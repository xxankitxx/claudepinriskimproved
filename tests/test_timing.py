"""Coverage for the new timing rules (council-flagged gap):
fixed-interval freshness re-anchor (mechanism tested generically via FreshnessEngine — see
vajra_config.yaml for the actual configured interval), prev-close seed -> 09:20 re-anchor, and the
trade gate (see vajra_config.yaml expiry.trade_start for the actual configured value)."""
from datetime import datetime, timezone, timedelta

from vajra.engine.freshness_engine import FreshnessEngine
from vajra.timeutil import hhmm_reached

IST = timezone(timedelta(hours=5, minutes=30))


def _ms(h, m, day=23):
    return int(datetime(2026, 6, day, h, m, tzinfo=IST).timestamp() * 1000)


def test_freshness_first_calc_then_30min_interval():
    fe = FreshnessEngine(hold_min_secs=1800)
    d0 = fe.update(_ms(9, 20), 24000.0)
    assert d0.need_recompute and d0.recalc_trigger == "open"      # first intraday calc
    fe.mark_recomputed(_ms(9, 20))
    assert not fe.update(_ms(9, 40), 24000.0).need_recompute       # 20 min < 30 -> held
    assert not fe.update(_ms(9, 49), 24000.0).need_recompute       # 29 min -> held
    d2 = fe.update(_ms(9, 51), 24000.0)
    assert d2.need_recompute and d2.recalc_trigger == "interval"   # 31 min -> re-anchor


def test_prev_close_seed_forces_0920_reanchor():
    fe = FreshnessEngine(hold_min_secs=1800)
    fe.mark_recomputed(_ms(15, 30, day=22))                        # seed = previous day's close
    d = fe.update(_ms(9, 20), 24000.0)                             # ~18h old >= 30 min
    assert d.need_recompute and d.recalc_trigger == "interval"


def test_failed_reanchor_does_not_phantom_refire():
    """Council CRITICAL: mark_recomputed only ran on SUCCESS, so a failed re-anchor left the age
    growing -> need_recompute every single round (phantom recalcs + multi-day 'ages'). A failed
    attempt must reset the re-trigger clock (short retry cadence) while the AGE keeps reflecting the
    last real success."""
    fe = FreshnessEngine(hold_min_secs=1800, retry_secs=30)
    fe.mark_recomputed(_ms(15, 30, day=22))                        # seed = prev close
    d = fe.update(_ms(9, 20))
    assert d.need_recompute and d.recalc_trigger == "interval"     # 09:20 wants a re-anchor
    fe.mark_attempt(_ms(9, 20))                                    # ...but the band FAILED (feed gap)
    # next ~10s of rounds at 4/sec must NOT re-fire (was: re-fired every round)
    assert not any(fe.update(_ms(9, 20)).need_recompute for _ in range(40))
    # age is still the stale seed age (not reset by a failed try) -> truthfully stale
    assert fe.update(_ms(9, 20)).freshness_age_secs > 1800
    # after retry_secs it retries; on success the age resets and the next re-anchor is a full 30 min
    assert fe.update(_ms(9, 21)).need_recompute                    # ~60s > retry_secs
    fe.mark_recomputed(_ms(9, 21))
    assert fe.update(_ms(9, 25)).freshness_age_secs < 300
    assert not fe.update(_ms(9, 40)).need_recompute                # 19 min held
    assert fe.update(_ms(9, 52)).need_recompute                    # 31 min -> normal interval


def test_trade_gate_0930_and_calc_gate_0920():
    assert not hhmm_reached(_ms(9, 19), "09:20")                   # before calc start
    assert hhmm_reached(_ms(9, 20), "09:20")                       # calc starts 09:20
    assert not hhmm_reached(_ms(9, 25), "09:30")                   # calc-only window, no trade
    assert hhmm_reached(_ms(9, 30), "09:30")                       # trading opens 09:30
