"""VAJRA 2.0 — tests for the new behaviour (regime, MTM non-expiry band, feed sanity, last-entry
cutoff, current-ATM strike, risk stand-down, backend sounds, event hygiene, schema completeness)."""
import ast
import pytest
import math
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import vajra.orchestrator as orch_mod
from vajra import schema as S
from vajra.alerts import SoundBus, ApproachEpisode
from vajra.app import build_calendar
from vajra.config import load_config
from vajra.engine.trigger_engine import compute_band, compute_band_v2, TriggerBand
from vajra.execution import Executor
from vajra.feed_health import FeedHealth, SPOT_FROZEN, SPOT_IMPLAUSIBLE, SPOT_JUMP, RECOVERING
from vajra.marketdata import MarketSnapshot, StrikeQuote
from vajra.orchestrator import Orchestrator
from vajra.regime import resolve_regime, trading_days_between

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


class _W:
    def __init__(self):
        self.rows, self.events = {}, []
    def append(self, n, r): self.rows.setdefault(n, []).append(r)
    def event(self, t, p=None, ts_ms=None): self.events.append((t, p, ts_ms))
    def flush(self): pass
    def close(self): pass


def _cfg():
    return load_config(str(ROOT / "vajra_config.yaml"))


def _ms(d, h, m, s=0):
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST).timestamp() * 1000)


def _q(k, r, ltp, vwap=None):
    return StrikeQuote(k, r, ltp, ltp if vwap is None else vwap, "feed", 100, 100, ltp - 0.5, ltp + 0.5)


# ---------------------------------------------------------------- regime
def test_regime_trading_dte_and_expiry_day():
    cfg = _cfg()
    cal = build_calendar(cfg)
    # Oct 20 2026 (Tue) is Dussehra -> weekly expiry Mon Oct 19
    assert trading_days_between(cal, date(2026, 10, 14), date(2026, 10, 19)) == 3
    assert trading_days_between(cal, date(2026, 10, 16), date(2026, 10, 19)) == 1
    r = resolve_regime(cfg, cal.resolve(date(2026, 10, 15)), cal)
    assert r.name == "NON_EXPIRY" and r.dte_trading == 2 and r.band_method == "mtm"
    # operator-locked: all-in sizing, no trade cap, no daily loss stop — in EVERY regime
    assert not any(hasattr(r, a) for a in ("capital_fraction", "max_trades", "daily_loss_stop_pct"))
    e = resolve_regime(cfg, cal.resolve(date(2026, 10, 19)), cal)
    assert e.name == "EXPIRY" and e.dte_trading == 0 and e.band_method == "legacy"
    assert not any(hasattr(e, a) for a in ("capital_fraction", "max_trades", "daily_loss_stop_pct"))


# ---------------------------------------------------------------- band
def _golden(spot=24110, ce_atm=35, pe_atm=15, vce=75, vpe=95):
    s = {(24100, "CE"): _q(24100, "CE", ce_atm), (24100, "PE"): _q(24100, "PE", pe_atm),
         (24050, "CE"): _q(24050, "CE", 0, vce), (24150, "PE"): _q(24150, "PE", 0, vpe)}
    return MarketSnapshot(0, "NIFTY", spot, 14.0, None, s)


def test_legacy_method_is_bit_identical_to_1x_golden():
    b = compute_band_v2(_golden(), 24100, 10.0, "IMPLIED", 50, method="legacy")
    assert (b.upper_level, b.lower_level) == (24125, 24035)
    assert not b.narrowed and b.band_method == "legacy"


def test_pad_is_tunable_below_010():
    a = compute_band(_golden(), 24100, 10.0, "IMPLIED", 50, pad_floor=0.10)
    b = compute_band(_golden(), 24100, 10.0, "IMPLIED", 50, pad_floor=0.05)
    assert b.raw_upper < a.raw_upper                      # 1.x silently clamped pad to >= 0.10


def _nonexpiry_chain(spot=22950.0):
    """Off-expiry chain: ITM options carry ~100 pts extrinsic, so the 1.x band is ~±110 wide."""
    atm = 22950
    s = {}
    for k in range(atm - 300, atm + 301, 50):
        ce_int, pe_int = max(spot - k, 0), max(k - spot, 0)
        ext = 100 * math.exp(-((k - spot) / 220.0) ** 2)
        s[(k, "CE")] = _q(k, "CE", round(ce_int + ext, 2))
        s[(k, "PE")] = _q(k, "PE", round(pe_int + ext, 2))
    return MarketSnapshot(0, "NIFTY", spot, 13.0, None, s), atm


def test_mtm_narrows_nonexpiry_band_and_never_widens():
    snap, atm = _nonexpiry_chain()
    leg = compute_band_v2(snap, atm, 0.0, "IMPLIED", 50, method="legacy")
    mtm = compute_band_v2(snap, atm, 0.0, "IMPLIED", 50, method="mtm", mtm_pad=0.20, mtm_floor_pts=25)
    assert (leg.upper_level - leg.lower_level) / 2 > 90                 # the 1.x problem
    assert mtm.upper_level <= leg.upper_level and mtm.lower_level >= leg.lower_level   # narrow only
    assert mtm.narrowed and mtm.band_method == "mtm"
    assert mtm.upper_level - snap.spot >= 25 - 2.5 and snap.spot - mtm.lower_level >= 25 - 2.5   # floor
    assert (mtm.legacy_upper, mtm.legacy_lower) == (leg.upper_level, leg.lower_level)


def test_mtm_reduces_to_legacy_when_time_value_is_zero():
    """Expiry-like chain (no extrinsic, delta 1): MTM must not narrow at all vs the 1.x formula."""
    spot = 24110.0
    s = {}
    for k in range(23900, 24351, 50):
        s[(k, "CE")] = _q(k, "CE", max(spot - k, 0.05))
        s[(k, "PE")] = _q(k, "PE", max(k - spot, 0.05))
    snap = MarketSnapshot(0, "NIFTY", spot, 14.0, None, s)
    leg = compute_band_v2(snap, 24100, 0.0, "IMPLIED", 50, method="legacy")
    mtm = compute_band_v2(snap, 24100, 0.0, "IMPLIED", 50, method="mtm", mtm_floor_pts=0)
    assert abs(mtm.upper_level - leg.upper_level) <= 5 and abs(mtm.lower_level - leg.lower_level) <= 5


def test_optional_hard_cap():
    snap, atm = _nonexpiry_chain()
    b = compute_band_v2(snap, atm, 0.0, "IMPLIED", 50, method="legacy", max_half_width_pts=40)
    assert b.upper_level - snap.spot <= 40 + 2.5 and snap.spot - b.lower_level <= 40 + 2.5


# ---------------------------------------------------------------- feed health
def test_feed_health_frozen_implausible_jump_and_recovery():
    fh = FeedHealth(frozen_secs=30, max_abs_basis=45, max_jump_pts=40, recovery_secs=30)
    t = 1_000_000
    assert fh.update(t, 23000.0, -5).ok
    starts = 0
    for k in range(1, 125):                                 # unchanged for 31 s at 4 rounds/s
        st = fh.update(t + k * 250, 23000.0, -5)
        starts += st.started
    assert not st.ok and st.reason == SPOT_FROZEN and starts == 1
    st = fh.update(t + 32_000, 23240.0, -240)               # the 15:20 garbage tick
    assert not st.ok and st.reason in (SPOT_IMPLAUSIBLE, SPOT_JUMP)
    st = fh.update(t + 33_000, 23001.0, -4)                 # snap-back is itself a jump -> still closed
    assert not st.ok
    st = fh.update(t + 34_000, 23001.5, -4)                 # clean -> recovering, not yet open
    assert not st.ok and st.reason == RECOVERING
    for k in range(1, 40):
        st = fh.update(t + 34_000 + k * 1000, 23001.5 + k * 0.5, -4)
    assert st.ok
    fh2 = FeedHealth(max_abs_basis=500)
    fh2.update(0, 23000.0, 0)
    assert fh2.update(250, 23060.0, -60).reason == SPOT_JUMP      # spot jumped, implied future did not
    fh3 = FeedHealth()
    fh3.update(0, 23000.0, 5)
    assert fh3.update(250, 23060.0, 5).ok                         # a REAL 60-pt move (future followed) is not garbage


def test_feed_health_long_recovery_after_garbage_even_if_it_ends_with_a_jump():
    fh = FeedHealth(recovery_secs=60, jump_recovery_secs=2)
    t = 0
    for k in range(20):
        fh.update(t + k * 250, 23000.0 + (k % 3) * 0.5, 5)
    t = 10_000
    assert fh.update(t, 23250.0, -245).reason in (SPOT_IMPLAUSIBLE, SPOT_JUMP)
    fh.update(t + 250, 23001.0, 6)                                 # snap-back jump
    st = None
    for k in range(2, 40):                                         # 10 s of clean data: still closed
        st = fh.update(t + k * 250, 23001.0 + (k % 2) * 0.5, 6)
    assert not st.ok


def test_feed_health_relative_basis_catches_garbage_under_the_absolute_limit():
    fh = FeedHealth()
    spot = 23000.0
    for i in range(4 * 180):                                       # 3 min clean, basis ~ +8
        spot += 0.25 if i % 2 else -0.2
        assert fh.update(i * 250, round(spot, 2), 8.0).ok
    t = 4 * 180 * 250
    st = None
    for k in range(12):                                            # basis jumps to +38 (< 40) for 3 s
        st = fh.update(t + k * 250, round(spot + 30 + (k % 2) * 0.5, 2), 38.0)
    assert not st.ok and st.reason == SPOT_IMPLAUSIBLE


def test_feed_health_quiet_on_normal_ticks():
    fh = FeedHealth()
    spot = 23000.0
    for i in range(4 * 600):                                # 10 min of normal 4 Hz ticks
        spot += (0.35 if (i // 7) % 2 == 0 else -0.3)
        assert fh.update(i * 250, round(spot, 2), -6 + (i % 5)).ok


# ---------------------------------------------------------------- execution
def test_entry_buys_current_atm_and_sizes_all_in():
    ex = Executor(_cfg())
    ex.capital = 100_000
    s = {(22850, "CE"): _q(22850, "CE", 120.0), (22750, "CE"): _q(22750, "CE", 190.0)}
    snap = MarketSnapshot(0, "NIFTY", 22855, 13.0, None, s)
    band = TriggerBand("NIFTY", 22750, 22700, 1.0, 1.0, 22862, 22800, 1.0, 1.0, 22617, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    rows = ex.enter(0, "UP", snap, band, 0, 0, [], False, "2026-10-19", 50, atm_strike=22850)
    assert rows[0]["action"] == "ENTRY" and rows[0]["strike"] == 22850     # not the anchor ATM 22750
    assert rows[0]["qty"] == 12 * 65                                        # all-in: floor(100,000 / (120.5*65)) = 12 lots


# ---------------------------------------------------------------- orchestrator integration
def _nonexpiry_orch():
    cfg = _cfg()
    cal = build_calendar(cfg)
    res = cal.resolve(date(2026, 10, 15))
    w = _W()
    return Orchestrator(cfg, w, res), w, res


def _snap_at(spot, atm=22950):
    snap, _ = _nonexpiry_chain(spot)
    return snap


def test_no_entry_at_or_after_flatten_time(monkeypatch):
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)
    o.process_round(_ms(d, 15, 29), {"NIFTY": _snap_at(22955)})      # 2.0.4: last entry 15:29
    assert not o.executor.position_active
    assert w.rows["decision_tape"][-1]["reason_code"] == "AFTER_LAST_ENTRY"
    assert o.latest_state["idle"]["gate"] == "AFTER_LAST_ENTRY"


def test_entry_fires_inside_window_and_rings_once(monkeypatch):
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)
    o.process_round(_ms(d, 10, 0), {"NIFTY": _snap_at(22955)})
    assert o.executor.position_active
    kinds = [e["kind"] for e in o.latest_state["sound_events"] if e["kind"] != "armed"]
    assert kinds.count("entry") == 1 and kinds.count("approach") == 0   # the entry cue says it; no double ring


def test_approach_cue_rings_before_the_entry_not_with_it(monkeypatch):
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)
    o.process_round(_ms(d, 10, 0), {"NIFTY": _snap_at(22948)})       # 12 pts from 22960: warn, no trade
    kinds = [e["kind"] for e in o.latest_state["sound_events"] if e["kind"] != "armed"]
    assert kinds == ["approach"] and not o.executor.position_active
    o.process_round(_ms(d, 10, 0, 1), {"NIFTY": _snap_at(22955)})    # 5 pts: entry
    kinds = [e["kind"] for e in o.latest_state["sound_events"] if e["kind"] != "armed"]
    assert kinds == ["approach", "entry"]


def test_no_approach_cue_during_post_stop_cooldown(monkeypatch):
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)
    o._last_stop_ms = _ms(d, 9, 58)                                  # inside the 11-min stop cooldown
    o.process_round(_ms(d, 10, 0), {"NIFTY": _snap_at(22948)})
    assert [e["kind"] for e in o.latest_state["sound_events"] if e["kind"] != "armed"] == []


def test_no_entry_and_no_reanchor_on_garbage_feed(monkeypatch):
    o, w, res = _nonexpiry_orch()
    d = date(2026, 10, 15)
    t0 = _ms(d, 14, 0)
    o.process_round(t0, {"NIFTY": _snap_at(22950)})          # anchors a real band
    anchors = len(o._level_history)
    bad = _snap_at(22950)
    bad.spot = 23190.0                                       # spot jumps 240 pts, chain unchanged -> |basis| huge
    o.process_round(t0 + 250, {"NIFTY": bad})
    assert not o.feed_health.last.ok
    assert not o.executor.position_active
    assert o.latest_state["idle"]["gate"] == "FEED_UNHEALTHY"
    assert len(o._level_history) == anchors                  # nothing anchored on garbage
    assert any(e["kind"] == "feed_alarm" for e in o.latest_state["sound_events"])
    assert any(t == "FEED_GAP" and (p or {}).get("stage") == "feed_sanity" for t, p, _ in w.events)


def test_position_is_managed_even_when_band_fails(monkeypatch):
    """1.x skipped manage() (stops and the 15:25 flatten) on any round whose NIFTY basis failed."""
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)
    o.process_round(_ms(d, 10, 0), {"NIFTY": _snap_at(22955)})
    pos = o.executor.position
    assert pos is not None
    # next round: ATM quotes missing (basis fails) but the held option still quotes at -20%
    s = {(pos.strike, pos.right): _q(pos.strike, pos.right, round(pos.entry_fill * 0.8, 2))}
    o.process_round(_ms(d, 10, 1), {"NIFTY": MarketSnapshot(0, "NIFTY", 22900.0, 13.0, None, s)})
    assert not o.executor.position_active                    # the hard stop fired
    assert any(e["kind"] == "exit_loss" for e in o.latest_state["sound_events"])


def test_no_daily_limits_anywhere():
    """operator-locked: full capital + running P&L every trade; no trade cap, no daily loss stop."""
    for day in (date(2026, 10, 15), date(2026, 10, 19)):
        cfg = _cfg()
        o = Orchestrator(cfg, _W(), build_calendar(cfg).resolve(day))
        assert not hasattr(o.executor, "capital_fraction")
        assert not hasattr(o, "_check_standdown")
        assert (o.entry_cutoff, o.flatten_at) == ("15:29", "15:38")         # expiry day too (operator)


# ---------------------------------------------------------------- sounds
def test_soundbus_rate_limits_but_never_drops_trade_cues():
    sb = SoundBus({"approach_cooldown_secs": 120, "global_min_gap_secs": 3})
    assert sb.emit("approach", 0)
    assert not sb.emit("approach", 60_000)                    # per-kind cooldown
    assert sb.emit("entry", 60_500) and sb.emit("exit_profit", 60_600)   # trade cues bypass limits
    assert sb.emit("feed_alarm", 61_000)                       # 2.0.2: an alarm is never lost to the gap
    assert not sb.emit("approach", 62_000)                    # ...but approach still respects it
    assert sb.emit("approach", 130_000)
    assert [e["seq"] for e in sb.snapshot()] == [1, 2, 3, 4, 5]


def test_soundbus_kind_can_be_switched_off_and_unknown_kinds_ignored():
    sb = SoundBus({"approach": "off"})
    assert not sb.emit("approach", 0)
    assert not sb.emit("sister_breach", 0)                    # sister breaches never sound
    assert sb.emit("entry", 0)


def test_approach_episode_hysteresis():
    ep = ApproachEpisode(prox=7, rearm_pts=8)
    seq = [20, 10, 6.5, 5, 7.5, 6, 3, 9, 14, 16, 6]           # hovering at the boundary = ONE episode
    fired = [ep.update(d, 23100.0) for d in seq]
    assert fired.count(True) == 2 and fired[2] and fired[10]  # second only after backing off > 15 pts
    assert ep.update(5, 23150.0) is True                      # a new level is a new episode


# ---------------------------------------------------------------- hygiene / schema
def test_reversal_watch_needs_real_bias_and_is_rate_limited():
    o, w, res = _nonexpiry_orch()
    assert o.reversal_min_bias >= 0.1 and o.reversal_refractory_ms >= 60_000


def test_every_event_type_used_in_code_is_declared():
    used = set()
    for py in (ROOT / "vajra").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("_emit_event", "event", "_feed_event", "_emit_alert_event"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    used.add(node.args[0].value)
    missing = used - set(S.EVENT_TYPES)
    assert not missing, f"undeclared event types: {missing}"


def test_every_recalc_trigger_written_is_declared():
    src = (ROOT / "vajra" / "orchestrator.py").read_text(encoding="utf-8")
    for trig in ("open", "interval", "post_exit", "post_cooldown", "prev_close_seed", "held"):
        assert trig in S.RECALC_TRIGGERS
    assert 'trigger="post_cooldown"' in src


def test_code_defaults_match_operator_yaml():
    """1.x code fell back to superseded rules (09:30 / 180 s / 120 s) when a key was missing."""
    cfg = _cfg()
    for sect in ("expiry", "execution"):
        cfg.raw[sect] = {k: v for k, v in cfg.raw[sect].items()
                         if k not in ("trade_start", "post_stop_cooldown_secs", "post_profit_cooldown_secs", "last_entry")}
    o = Orchestrator(cfg, _W(), build_calendar(cfg).resolve(date(2026, 10, 15)))
    assert o.trade_start == "09:21" and o.post_stop_cooldown_ms == 660_000
    assert o.post_profit_cooldown_ms == 15_000 and o.entry_cutoff == "15:29"


def test_every_universe_stock_has_a_weight():
    cfg = _cfg()
    for st in cfg.stocks:
        assert cfg.weights.get(st) is not None and cfg.weights[st].decision_weight > 0, f"{st} has no weight"


def test_api_state_survives_nan():
    pytest.importorskip("httpx", reason="starlette's TestClient needs httpx (test-only dependency)")
    from fastapi.testclient import TestClient
    from vajra.dashboard import build_app
    fake = SimpleNamespace(latest_state={"round_ts": 1, "nifty": {"vix": float("nan")}, "x": [float("inf")]})
    r = TestClient(build_app(fake)).get("/api/state")
    assert r.status_code == 200 and r.json()["nifty"]["vix"] is None


# ---------------------------------------------------------------- REAL data regression (10-15 fixture)
def test_real_1015_fixture_v2_behaviour():
    """2 MB real fixture cut from the recorded 2026-10-15 tape (NIFTY/BANKNIFTY/FINNIFTY at 1 Hz,
    09:41-10:15 and 15:10-15:30). v1 took ONE morning trade and a garbage 15:20 trade here.
    v2 must trade the morning trend (narrowed non-expiry band). 2.0.4: from 15:15 the frozen /
    indicative index is replaced by forward - basis_ref, so the feed stays healthy and the engine's
    spot tracks the forward (never the +185 pt indicative close)."""
    from vajra.feed import ReplaySource
    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 10, 15))
    w = _W()
    o = Orchestrator(cfg, w, res)
    o.run(ReplaySource(ROOT / "tests" / "fixtures" / "2026-10-15"))
    entries = [r for r in w.rows.get("order_tape", []) if r["action"] == "ENTRY"]
    bad_from = _ms(date(2026, 10, 15), 15, 15)
    assert len(entries) >= 2, "v2 should trade the 10-15 morning trend"
    assert any(t == "ANOMALY" and (p or {}).get("kind") == "CLOSING_SESSION_SPOT"
               and p["basis_ref_source"] == "median_clean_basis" for t, p, _ in w.events)
    assert not any(t == "FEED_GAP" and (p or {}).get("stage") == "feed_sanity" and ts and ts >= bad_from
                   for t, p, ts in w.events), "the closing-session proxy must keep the feed healthy"
    late = [r for r in w.rows["basis_tape"] if r["instrument"] == "NIFTY" and r["ts"] >= bad_from]
    assert late and all(r["spot_source"] == "CLOSING_PROXY" and abs(r["spot"] - r["future"]) < 5 for r in late)
    assert max(r["index_spot"] - r["spot"] for r in late) > 100     # the indicative close it ignored


# ---------------------------------------------------------------- 2.0.1 teardown fixes
def test_nan_quote_never_sizes_or_fills():
    """T07: a NaN ask used to pass the premium floor and size 30,769 lots at a NaN fill."""
    ex = Executor(_cfg())
    nanq = StrikeQuote(22850, "CE", float("nan"), 100.0, "feed", 100, 100, float("nan"), float("nan"))
    snap = MarketSnapshot(0, "NIFTY", 22855, 13.0, None, {(22850, "CE"): nanq})
    band = TriggerBand("NIFTY", 22850, 22800, 1.0, 1.0, 22862, 22900, 1.0, 1.0, 22817, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    rows = ex.enter(0, "UP", snap, band, 0, 0, [], False, "2026-10-19", 50, atm_strike=22850)
    assert rows[0]["action"] == "REJECT" and not ex.position_active
    assert ex.compute_lots(float("nan")) == 0 and ex.compute_lots(0.0) == 0
    zero_ask = StrikeQuote(22850, "CE", float("nan"), 100.0, "feed", 100, 100, 0.0, 0.0)
    snap2 = MarketSnapshot(0, "NIFTY", 22855, 13.0, None, {(22850, "CE"): zero_ask})
    assert ex.enter(0, "UP", snap2, band, 0, 0, [], False, "2026-10-19", 50, atm_strike=22850)[0]["action"] == "REJECT"
    assert math.isfinite(ex.capital)


def test_nan_bid_at_exit_does_not_poison_capital():
    ex = Executor(_cfg())
    good = {(22850, "CE"): _q(22850, "CE", 120.0)}
    band = TriggerBand("NIFTY", 22850, 22800, 1.0, 1.0, 22862, 22900, 1.0, 1.0, 22817, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    ex.enter(0, "UP", MarketSnapshot(0, "NIFTY", 22855, 13.0, None, good), band, 0, 0, [], False,
             "2026-10-19", 50, atm_strike=22850)
    assert ex.position_active
    bad = StrikeQuote(22850, "CE", 90.0, 100.0, "feed", 100, 100, float("nan"), float("nan"))
    rows = ex.manage(1000, MarketSnapshot(1000, "NIFTY", 22800, 13.0, None, {(22850, "CE"): bad}))
    assert rows and rows[0]["action"] == "EXIT" and rows[0]["fill_price"] == 90.0   # falls back to the finite LTP
    assert math.isfinite(ex.capital)


def test_exact_plus_10pct_peak_arms_the_trail():
    ex = Executor(_cfg())
    s = {(22850, "CE"): StrikeQuote(22850, "CE", 100.0, 100.0, "feed", 1, 1, 100.0, 100.0)}
    band = TriggerBand("NIFTY", 22850, 22800, 1.0, 1.0, 22862, 22900, 1.0, 1.0, 22817, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    ex.enter(0, "UP", MarketSnapshot(0, "NIFTY", 22855, 13.0, None, s), band, 0, 0, [], False,
             "2026-10-19", 50, atm_strike=22850)
    q = StrikeQuote(22850, "CE", 110.0, 100.0, "feed", 1, 1, 110.0, 110.0)
    ex.manage(1000, MarketSnapshot(1000, "NIFTY", 22900, 13.0, None, {(22850, "CE"): q}))
    assert ex.position.trail_armed


def test_live_orders_only_from_a_real_boolean_plus_opt_in():
    from vajra.config import live_orders_enabled
    for v in ("false", "no", "off", "0", "true", "yes", 1, "True", None):
        assert live_orders_enabled({"LIVE_ORDERS": v, "LIVE_ORDERS_CONFIRM": "I UNDERSTAND REAL MONEY"}) is False
    assert live_orders_enabled({"LIVE_ORDERS": True}) is False                       # no opt-in -> paper
    assert live_orders_enabled({"LIVE_ORDERS": True, "LIVE_ORDERS_CONFIRM": "I UNDERSTAND REAL MONEY"}) is True
    assert _cfg().live_orders is False


def test_token_file_written_by_powershell_or_notepad(tmp_path):
    from vajra.config import _read_text_any
    for enc, data in (("utf-16", "ABC123\r\n"), ("utf-8-sig", "ABC123\n"), ("utf-8", "ABC123")):
        f = tmp_path / f"t_{enc}"
        f.write_bytes(data.encode(enc))
        assert _read_text_any(f).strip() == "ABC123"


def test_same_day_restart_restores_capital_position_and_cooldowns(tmp_path):
    import json
    o, w, res = _nonexpiry_orch()
    d = date(2026, 10, 15)
    rows = [
        {"ts": _ms(d, 9, 40), "action": "ENTRY", "strike": 22850, "right": "CE", "qty": 780, "fill_price": 120.0,
         "pnl_realized": 0.0, "exit_reason": "", "order_id": "PAPER-1"},
        {"ts": _ms(d, 9, 45), "action": "EXIT", "strike": 22850, "right": "CE", "qty": 780, "fill_price": 135.0,
         "pnl_realized": 11700.0, "exit_reason": "TRAIL_SL", "order_id": "PAPER-2"},
        {"ts": _ms(d, 10, 5), "action": "ENTRY", "strike": 22900, "right": "PE", "qty": 845, "fill_price": 130.0,
         "pnl_realized": 0.0, "exit_reason": "", "order_id": "PAPER-3"},
    ]
    (tmp_path / "order_tape.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{torn", encoding="utf-8")
    info = o.restore_from_order_tape(tmp_path)
    assert info["capital"] == 111_700.0 and o.executor.capital == 111_700.0
    pos = o.executor.position
    assert pos.open and pos.right == "PE" and pos.strike == 22900 and pos.qty_remaining == 845
    assert o._last_profit_ms == _ms(d, 9, 45) and o._entries_today == 2
    assert any(t == "RESTORED" for t, _, _ in w.events)
    assert "RESTORED" in S.EVENT_TYPES


def test_one_bad_round_does_not_end_the_session(monkeypatch):
    """T01: an exception after the per-instrument loop ended the whole trading day."""
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)
    o.process_round(_ms(d, 10, 0), {"NIFTY": _snap_at(22955)})
    pos = o.executor.position
    assert pos is not None
    calls = {"n": 0}
    real = o._idle_diagnostics
    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("injected")
    monkeypatch.setattr(o, "_idle_diagnostics", boom)
    s = {(pos.strike, pos.right): _q(pos.strike, pos.right, round(pos.entry_fill * 0.8, 2))}

    class Src:
        def rounds(self):
            yield _ms(d, 10, 1), {"NIFTY": MarketSnapshot(0, "NIFTY", 22900.0, 13.0, None, s)}
            monkeypatch.setattr(o, "_idle_diagnostics", real)
            yield _ms(d, 10, 2), {"NIFTY": _snap_at(22900)}
    o.run(Src())
    assert calls["n"] >= 1
    assert not o.executor.position_active                       # the stop still fired on the bad round
    errs = [p for t, p, _ in w.events if t == "SESSION_ERROR" and (p or {}).get("stage") == "process_round"]
    assert errs and errs[0]["severity"] == "ROUND_SKIPPED"
    assert w.rows["decision_tape"][-1]["ts"] == _ms(d, 10, 2)  # the NEXT round was processed normally


def test_mtm_rounding_never_widens_past_legacy():
    snap, _ = _nonexpiry_chain()
    for spot in (22947.6, 22951.3, 22953.9, 22948.2):
        snap.spot = spot
        b = compute_band_v2(snap, 22950, 0.0, "IMPLIED", 50, method="mtm")
        if b.ok:
            assert b.upper_level <= b.legacy_upper + 1e-9 and b.lower_level >= b.legacy_lower - 1e-9


def test_nan_vwap_is_missing_not_a_nan_level():
    snap, _ = _nonexpiry_chain()
    k = next(iter(snap.strikes))
    q = snap.strikes[k]
    snap.strikes[k] = StrikeQuote(q.strike, q.right, q.ltp, float("nan"), "feed", 1, 1, q.bid, q.ask)
    for kk in list(snap.strikes):
        qq = snap.strikes[kk]
        snap.strikes[kk] = StrikeQuote(qq.strike, qq.right, qq.ltp, float("nan"), "feed", 1, 1, qq.bid, qq.ask)
    b = compute_band(snap, 22950, 0.0, "IMPLIED", 50)
    assert not b.ok and b.upper_level is None


def test_dashboard_has_session_info_before_0920():
    cfg = _cfg()
    o = Orchestrator(cfg, _W(), build_calendar(cfg).resolve(date(2026, 10, 19)))
    st = o.latest_state
    assert st["expiry"]["is_expiry_day"] is True and st["regime"]["name"] == "EXPIRY"
    assert st["session_date"] == "2026-10-19" and st["boot_id"]
    assert st["expiry"]["shifted_from_holiday"]                   # Tue 20 Oct holiday -> Mon


def test_regime_event_uses_round_time_not_wall_clock():
    o, w, res = _nonexpiry_orch()
    assert not any(t == "REGIME" for t, _, _ in w.events)
    t = _ms(date(2026, 10, 15), 9, 20)
    o.process_round(t, {"NIFTY": _snap_at(22950)})
    ev = [ts for typ, _, ts in w.events if typ == "REGIME"]
    assert ev == [t]


def test_alternating_alert_types_are_each_rate_limited():
    """T21: a shared last-key let APPROACH_ALERT / ENTRY_REJECTED both fire every round."""
    o, w, res = _nonexpiry_orch()
    for i in range(40):
        t = 1_000_000 + i * 250
        o._emit_alert_event("APPROACH_ALERT", "UP", {"direction": "UP"}, t)
        o._emit_alert_event("ENTRY_REJECTED", "PREMIUM_TOO_THIN", {"reason": "PREMIUM_TOO_THIN"}, t)
    n_a = sum(1 for t, _, _ in w.events if t == "APPROACH_ALERT")
    n_r = sum(1 for t, _, _ in w.events if t == "ENTRY_REJECTED")
    assert n_a == 1 and n_r == 1


def test_blocked_entry_is_not_shown_as_buy(monkeypatch):
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)
    o._last_stop_ms = _ms(d, 9, 58)
    o.process_round(_ms(d, 10, 0), {"NIFTY": _snap_at(22955)})
    a = o.latest_state["arming"]
    assert not o.executor.position_active
    assert a.get("target") is None and "NO TRADE" in a["message"]


def test_full_disk_does_not_kill_the_session(tmp_path, monkeypatch):
    """T15: a full disk ended the day mid-trade with no SHUTDOWN."""
    from vajra.tape import TapeWriter
    tw = TapeWriter(tmp_path, date(2026, 10, 15))
    class Bad:
        def write(self, *_): raise OSError(28, "No space left on device")
        def flush(self): raise OSError(28, "No space left on device")
        def close(self): pass
    tw._jsonl["order_tape"] = Bad(); tw._jsonl["event_tape"] = Bad()
    tw.append("order_tape", {"ts": 1, "action": "ENTRY", "instrument": "NIFTY", "strike": 1, "right": "CE",
                             "qty": 1, "intended_price": 1.0, "fill_price": 1.0, "slippage": 0.0,
                             "order_id": "x", "live_orders_flag": False, "exit_reason": "", "pnl_realized": 0.0})
    tw.event("SHUTDOWN", {"reason": "t"}, ts_ms=1)
    assert tw.disk_errors == 2


def test_no_flatten_at_a_stale_price_when_nifty_vanished_before_the_close(monkeypatch):
    o, w, res = _nonexpiry_orch()
    band = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: band)
    d = date(2026, 10, 15)

    class Src:
        def rounds(self):
            yield _ms(d, 15, 0), {"NIFTY": _snap_at(22955)}            # enters
            for k in range(1, 400):                                     # then NIFTY never returns
                yield _ms(d, 15, 0) + k * 250, {}
    o.run(Src())
    assert o.executor.position_active                                  # NOT booked at a 100-s-old price
    assert any(t == "SESSION_ERROR" and (p or {}).get("stage") == "finalize" for t, p, _ in w.events)


def test_strike_from_anchor_atm_restores_1x_strike(monkeypatch):
    ex = Executor(_cfg())
    s = {(22850, "CE"): _q(22850, "CE", 120.0), (22750, "CE"): _q(22750, "CE", 190.0)}
    snap = MarketSnapshot(0, "NIFTY", 22855, 13.0, None, s)
    band = TriggerBand("NIFTY", 22750, 22700, 1.0, 1.0, 22862, 22800, 1.0, 1.0, 22617, 0.0,
                       "IMPLIED", 0.1, 1, 5.0, True, None)
    rows = ex.enter(0, "UP", snap, band, 0, 0, [], False, "2026-10-19", 50, atm_strike=None)
    assert rows[0]["strike"] == 22750                       # anchor ATM, as 1.0
    cfg = _cfg(); cfg.raw["execution"]["strike_from"] = "anchor_atm"
    o = Orchestrator(cfg, _W(), build_calendar(cfg).resolve(date(2026, 10, 15)))
    assert o.strike_from == "anchor_atm"


def test_one_tick_35pt_garbage_print_is_caught_and_clears_in_2s():
    """T16: 7 of 26 stress trades were entered on a one-tick +-30/35 print the gate let through."""
    fh = FeedHealth()
    spot = 23000.0
    for i in range(400):
        spot += 0.25 if i % 2 else -0.2
        assert fh.update(i * 250, round(spot, 2), 6.0).ok
    t = 400 * 250
    st = fh.update(t, spot + 35.0, 6.0 + 35.0)          # spot +35, implied future unchanged (basis +35)
    assert not st.ok and st.reason == SPOT_JUMP
    st = fh.update(t + 250, spot + 0.1, 6.0)            # snap back: future unchanged -> diverged too
    assert not st.ok
    for k in range(2, 12):
        st = fh.update(t + k * 250, spot + 0.1 + k * 0.05, 6.0)
    assert st.ok                                        # pure jump fault: open again after ~2 s
    fh2 = FeedHealth()
    fh2.update(0, 23000.0, 5.0)
    assert fh2.update(250, 23035.0, 5.0).ok             # a REAL 35-pt move (future followed) is fine


def test_eod_forensics_tapes_are_written(tmp_path):
    """2.0.1: round_tape (per round), SOUND events, client beacons and raw ticks exist for EOD audit."""
    import json
    from vajra.tape import TapeWriter
    from vajra.rawtape import RawTickRecorder
    cfg = _cfg()
    tw = TapeWriter(tmp_path, date(2026, 10, 15))
    o = Orchestrator(cfg, tw, build_calendar(cfg).resolve(date(2026, 10, 15)))
    d = date(2026, 10, 15)
    o.process_round(_ms(d, 10, 0), {"NIFTY": _snap_at(22950)})
    o.sounds.emit("entry", _ms(d, 10, 0), "test")
    tw.client({"paints": 4, "rounds": 4, "sounds": [[1, "entry"]]})
    tw.close()
    rt = [json.loads(l) for l in open(tmp_path / "2026-10-15" / "round_tape.jsonl")]
    assert rt and {"ts", "wall_ms", "proc_ms", "health", "gate", "spot"} <= set(rt[0])
    ev = [json.loads(l) for l in open(tmp_path / "2026-10-15" / "event_tape.jsonl")]
    assert any(e["event_type"] == "SOUND" and e["payload"]["kind"] == "entry" for e in ev)
    cl = [json.loads(l) for l in open(tmp_path / "2026-10-15" / "client_tape.jsonl")]
    assert cl[0]["paints"] == 4 and "recv_ms" in cl[0]
    rr = RawTickRecorder(tmp_path / "2026-10-15")
    for i in range(5):
        rr.record({"_key": "NIFTY|cash|None|None", "stock_code": "NIFTY", "kind": "cash", "ltp": 23000.0 + i}, {"ltt": "10:00:0%d" % i}, 1000 + i)
    rr.close()
    import pyarrow.parquet as pq
    t = pq.read_table(tmp_path / "2026-10-15" / "raw_ticks.parquet")
    assert t.num_rows == 5 and t.column("ltt").to_pylist()[0] == "10:00:00"


def test_client_beacon_endpoint(tmp_path):
    pytest.importorskip("httpx", reason="starlette's TestClient needs httpx (test-only dependency)")
    from fastapi.testclient import TestClient
    from vajra.dashboard import build_app
    from vajra.tape import TapeWriter
    cfg = _cfg()
    tw = TapeWriter(tmp_path, date(2026, 10, 15))
    o = Orchestrator(cfg, tw, build_calendar(cfg).resolve(date(2026, 10, 15)))
    c = TestClient(build_app(o))
    assert c.post("/api/client", json={"paints": 1, "rounds": 1, "sounds": []}).status_code == 200
    assert c.post("/api/client", content=b"garbage", headers={"Content-Type": "application/json"}).status_code == 400
    tw.close()
    assert (tmp_path / "2026-10-15" / "client_tape.jsonl").exists()
