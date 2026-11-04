"""Coverage for the REAL entry path (the council flagged it as untested):
the anticipatory _approach_entry trigger, the premium floor, and insufficient-capital reject."""
from datetime import date, datetime, timezone, timedelta
from types import SimpleNamespace
from pathlib import Path

import vajra.orchestrator as orch_mod
from vajra.config import load_config
from vajra.app import build_calendar
from vajra.orchestrator import Orchestrator
from vajra.execution import Executor
from vajra.engine.trigger_engine import TriggerBand
from vajra.marketdata import MarketSnapshot, StrikeQuote

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


class _NullWriter:
    def __getattr__(self, n):
        return lambda *a, **k: None


def _cfg():
    return load_config(str(ROOT / "vajra_config.yaml"))


def _orch():
    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 6, 23))
    return Orchestrator(cfg, _NullWriter(), res)


def _band(upper, lower, atm=24100):
    return TriggerBand("NIFTY", atm, atm - 50, 50.0, upper, upper, atm + 50, 50.0,
                       lower, lower, 0.0, "IMPLIED", 0.10, 1, 5.0, True, None)


def _snap(spot, atm=24100, ce=120.0, pe=120.0):
    s = {(atm, "CE"): StrikeQuote(atm, "CE", ce, ce, "feed", 1, 1, ce, ce),
         (atm, "PE"): StrikeQuote(atm, "PE", pe, pe, "feed", 1, 1, pe, pe)}
    return MarketSnapshot(0, "NIFTY", spot, 14.0, None, s)


DR = SimpleNamespace(bias_ewma=0.0)


class _RecordingWriter(_NullWriter):
    def __init__(self):
        self.appended = []
    def append(self, tape_name, row):
        self.appended.append((tape_name, row))


def test_seed_from_prev_close_never_fabricates_a_zero_level():
    """council finding: seed_from_prev_close was the ONE band-write site with no band.ok guard, so a
    failed pre-open seed (missing ITM CE/PE quote) silently wrote upper_level=0.0/lower_level=0.0 to
    level_tape via `band.upper_level or 0.0` — indistinguishable from a real computed level. Must
    instead skip the write entirely and emit a loud FEED_GAP, exactly like the other two band sites."""
    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 6, 23))
    w = _RecordingWriter()
    o = Orchestrator(cfg, w, res)
    seen = []
    _orig = o._emit_event
    o._emit_event = lambda t, p=None, ts_ms=None: (seen.append((t, (p or {}).get("reason"))), _orig(t, p, ts_ms=ts_ms))[1]
    # _snap() only has the ATM strike quote -> ITM (atm-50/atm+50) CE/PE is missing -> band.ok=False
    o.seed_from_prev_close(_snap(24100), 0)
    assert o.states["NIFTY"].band is None, "a failed seed must NOT arm a band"
    assert not any(name == "level_tape" for name, _ in w.appended), "a failed seed wrote a fabricated level_tape row"
    assert ("FEED_GAP", "MISSING_VWAP") in seen, "a failed seed must be surfaced LOUDLY, not silently dropped"


# ---- anticipatory trigger (within 7 pts) AND breach-through -----------------
def test_approach_fires_up_within_proximity():
    o = _orch()
    e = o._approach_entry(_band(24108, 24092), _snap(24104), {}, DR)   # 4 pts below upper
    assert e and e["direction"] == "UP" and not e["breached"]


def test_breach_through_upper_still_fires():
    o = _orch()
    e = o._approach_entry(_band(24108, 24092), _snap(24115), {}, DR)   # gapped ABOVE upper
    assert e and e["direction"] == "UP" and e["breached"]


def test_approach_fires_down_near_lower():
    o = _orch()
    e = o._approach_entry(_band(24108, 24092), _snap(24095), {}, DR)   # 3 pts above lower
    assert e and e["direction"] == "DOWN"


def test_no_signal_when_mid_band():
    o = _orch()
    assert o._approach_entry(_band(24108, 24092), _snap(24100), {}, DR) is None


def test_sister_levels_reanchor_faster_than_nifty():
    """operator rule: NIFTY re-anchors on the 20-min rule (its level drives trades); sisters/stocks
    re-anchor faster for the confluence/lean. 2.0: 3 min -> 10 min (each 3-min re-anchor re-created
    breach flicker, the source of the old sister-breach 'ting ting'; team findings 06/28)."""
    o = _orch()
    assert o.states["NIFTY"].freshness.hold_min_secs == 1200        # 20 min
    for inst in ("BANKNIFTY", "FINNIFTY", "HDFCBANK", "SBIN", "RELIANCE"):
        assert o.states[inst].freshness.hold_min_secs == 180        # 3 min (operator-locked)


# ---- confluence: absolute_net_pull is direction-INDEPENDENT (lean-panel fix) ----
def test_confluence_absolute_net_pull_not_inverted():
    """Council HIGH: the NIFTY-LEAN panel read net_pull, which is measured RELATIVE to our trade
    direction, so a DOWN setup with a DOWN bloc gave a POSITIVE net_pull -> panel showed 'BULLISH'
    while the market was bearish. absolute_net_pull (signed by each instrument's OWN breach dir)
    must be negative for a down-leaning bloc, regardless of our trade direction."""
    from vajra.engine.continuation_validator import ContinuationSignal as CS

    def sig(d, s):
        st = "BREACH_UP" if d > 0 else "BREACH_DOWN"
        return CS(st, d, s, True, False, False, 0.0, None, 0.0)

    o = _orch()
    signals = {"NIFTY": sig(-1, 0.5),
               "LT": sig(-1, 0.8), "SBIN": sig(-1, 0.7), "BAJAJFINSV": sig(-1, 0.6),
               "SBILIFE": sig(-1, 0.6), "AUBANK": sig(-1, 0.5), "SHRIRAMFIN": sig(-1, 0.5),
               "IDFCFIRSTB": sig(1, 0.4)}
    conf = o._confluence(signals, "DOWN", SimpleNamespace(bias_ewma=-0.318))
    assert conf["net_pull"] > 0           # the inversion trap (relative to the DOWN trade)
    assert conf["absolute_net_pull"] < 0  # the correct, direction-independent bearish lean
    # and a mirror-image UP bloc must read positive
    up = {k: sig(-v.dir, v.strength) for k, v in signals.items()}
    assert o._confluence(up, "UP", SimpleNamespace(bias_ewma=0.318))["absolute_net_pull"] > 0


# ---- council v4: reversal gate must compare DIRECTION (not just "a bias exists") ----
def test_is_reversal_requires_against_bias_direction():
    R = Orchestrator._is_reversal
    # abrupt move AGAINST the bias (down-abrupt while bias is up) WITH weight -> reversal
    assert R(True, -1, +0.8, 1.0, 0.5) is True
    # abrupt move WITH the bias (up-abrupt while bias is up) -> NOT a reversal (continuation)
    assert R(True, +1, +0.8, 1.0, 0.5) is False
    # no established bias -> nothing to reverse
    assert R(True, -1, 0.0, 1.0, 0.5) is False
    # not abrupt / below the weight floor -> no reversal
    assert R(False, -1, +0.8, 1.0, 0.5) is False
    assert R(True, -1, +0.8, 0.2, 0.5) is False


# ---- council v4 CRITICAL: one bad instrument must NOT crash the whole round ----
def _round_ms(h, m):
    return int(datetime(2026, 6, 23, h, m, tzinfo=IST).timestamp() * 1000)


def test_nan_spot_instrument_does_not_crash_the_round():
    o = _orch()
    seen = []
    _orig = o._emit_event
    o._emit_event = lambda t, p=None, ts_ms=None: (seen.append((t, (p or {}).get("reason"))), _orig(t, p, ts_ms=ts_ms))[1]
    bad = MarketSnapshot(0, "BANKNIFTY", float("nan"), 14.0, None, {})   # NaN spot crashed int(round_to(nan))
    state = o.process_round(_round_ms(10, 0), {"NIFTY": _snap(24100), "BANKNIFTY": bad})   # must NOT raise
    assert isinstance(state, dict) and "BANKNIFTY" in state["instruments"]   # degraded, not vanished
    assert state["instruments"]["BANKNIFTY"].get("stale") is True
    assert ("FEED_GAP", "NONFINITE_SPOT") in seen


def test_instrument_exception_is_isolated_not_fatal(monkeypatch):
    o = _orch()
    def _boom(*a, **k):
        raise ValueError("simulated bad tick")
    monkeypatch.setattr(orch_mod, "compute_basis", _boom)   # every instrument throws
    seen = []
    _orig = o._emit_event
    o._emit_event = lambda t, p=None, ts_ms=None: (seen.append((t, (p or {}).get("kind"))), _orig(t, p, ts_ms=ts_ms))[1]
    state = o.process_round(_round_ms(10, 0), {"NIFTY": _snap(24100), "BANKNIFTY": _snap(52000, atm=52000)})
    assert isinstance(state, dict)                          # the round survived the throw
    assert ("ANOMALY", "INSTRUMENT_ERROR") in seen          # and flagged it instead of dying


# ---- council v4 BROKEN fix #6: in-trade confluence reads the HELD position's direction ----
def test_in_trade_confluence_uses_position_direction_not_nearest_level():
    from vajra.engine.continuation_validator import ContinuationSignal as CS
    from vajra.execution import Position
    o = _orch()
    # spot sits near the UPPER level -> band geometry "direction" = UP
    band = _band(24108, 24092)
    snap = _snap(24106)
    # sisters are breaking DOWN; we hold a DOWN PE position
    sig = lambda d: CS("BREACH_DOWN" if d < 0 else "BREACH_UP", d, 0.8, True, False, False, 0.0, None, 0.0)
    signals = {"NIFTY": sig(-1), "SBIN": sig(-1), "LT": sig(-1)}
    o.executor.position = Position(direction="DOWN", right="PE", strike=24050, entry_ts=0, entry_fill=100.0,
                                   entry_spot=24100, lots=1, lot_size=65, qty_total=65, qty_remaining=65,
                                   cost=6500.0, peak_premium=100.0, last_premium=100.0)
    alarm = o._compute_alarm(_round_ms(10, 0), band, snap, signals, SimpleNamespace(bias_ewma=-0.3))
    # confluence must be read relative to the DOWN trade: the DOWN sisters are SAME-side (supporting),
    # not opposite. (Before the fix it used the UP band geometry and flipped them to opp_side.)
    assert "SBIN" in alarm["confluence"]["same_side"] and "LT" in alarm["confluence"]["same_side"]
    assert alarm["confluence"]["opp_side"] == []


# ---- premium floor: 45 non-expiry / 30 expiry ------------------------------
def _enter(ex, ce, is_expiry):
    return ex.enter(0, "UP", _snap(24120, ce=ce), _band(24108, 24092), conviction=1.0,
                    threshold=0.25, channels=["NIFTY"], is_expiry_day=is_expiry,
                    expiry_iso="2026-06-23", step=50)


def test_premium_floor_nonexpiry_45():
    ex = Executor(_cfg()); ex.capital = 100_000
    assert _enter(ex, 30.0, False)[0]["exit_reason"] == "PREMIUM_TOO_THIN"   # 30 < 45
    assert _enter(ex, 50.0, False)[0]["action"] == "ENTRY"                   # 50 >= 45


def test_premium_floor_expiry_30():
    ex = Executor(_cfg()); ex.capital = 100_000
    assert _enter(ex, 25.0, True)[0]["exit_reason"] == "PREMIUM_TOO_THIN"    # 25 < 30
    assert _enter(ex, 35.0, True)[0]["action"] == "ENTRY"                    # 35 >= 30


# ---- insufficient capital -> no trade, never negative ----------------------
def test_insufficient_capital_rejects():
    ex = Executor(_cfg()); ex.capital = 1_000                                # can't afford one lot (40*65=2600)
    rows = _enter(ex, 40.0, True)
    assert rows[0]["action"] == "REJECT" and rows[0]["exit_reason"] == "INSUFFICIENT_CAPITAL"
    assert ex.position is None


# ---- expiry-day ITM fallback when the ATM premium is below the floor --------
def test_expiry_itm_fallback_when_atm_below_floor():
    """Expiry day: ATM premium below the 30 floor -> step IN to the nearest ITM that clears it."""
    ex = Executor(_cfg()); ex.capital = 100_000
    atm = 24100
    s = {(atm, "CE"): StrikeQuote(atm, "CE", 20.0, 20.0, "feed", 1, 1, 20.0, 20.0),       # ATM 20 < 30
         (24050, "CE"): StrikeQuote(24050, "CE", 40.0, 40.0, "feed", 1, 1, 40.0, 40.0)}   # 1-ITM 40 >= 30
    snap = MarketSnapshot(0, "NIFTY", 24120, 14.0, None, s)
    rows = ex.enter(0, "UP", snap, _band(24108, 24092, atm=atm), conviction=1.0, threshold=0.25,
                    channels=["NIFTY"], is_expiry_day=True, expiry_iso="2026-06-23", step=50)
    assert rows and rows[0]["action"] == "ENTRY"
    assert rows[0]["strike"] == 24050 and ex.position.strike == 24050    # stepped IN to the ITM strike


def test_nonexpiry_keeps_strict_atm_floor():
    """Non-expiry day: NO ITM fallback -> a thin ATM is still rejected (operator scoped this to expiry)."""
    ex = Executor(_cfg()); ex.capital = 100_000
    atm = 24100
    s = {(atm, "CE"): StrikeQuote(atm, "CE", 40.0, 40.0, "feed", 1, 1, 40.0, 40.0),       # ATM 40 < 45
         (24050, "CE"): StrikeQuote(24050, "CE", 60.0, 60.0, "feed", 1, 1, 60.0, 60.0)}
    snap = MarketSnapshot(0, "NIFTY", 24120, 14.0, None, s)
    rows = ex.enter(0, "UP", snap, _band(24108, 24092, atm=atm), conviction=1.0, threshold=0.25,
                    channels=["NIFTY"], is_expiry_day=False, expiry_iso="2026-06-23", step=50)
    assert rows and rows[0]["exit_reason"] == "PREMIUM_TOO_THIN"


# ---- decision_tape records the REAL outcome, not an unconditional FIRE ------
class _CapWriter:
    """Captures every tape append/event/flush so a test can read the binding tapes."""
    def __init__(self):
        self.rows: dict[str, list] = {}
        self.events: list = []
        self.flushed = 0
    def append(self, name, row):
        self.rows.setdefault(name, []).append(row)
    def event(self, event_type, payload=None, ts_ms=None):
        self.events.append({"type": event_type, "payload": payload, "ts": ts_ms})
    def flush(self):
        self.flushed += 1
    def close(self):
        pass


def _ms(h, m, day=23):
    return int(datetime(2026, 6, day, h, m, tzinfo=IST).timestamp() * 1000)


def test_decision_tape_records_reject_reason_not_fire(monkeypatch):
    """A rejected anticipatory entry must leave decision_tape showing HOLD + the reject
    reason (not FIRE_UP/APPROACH_ENTRY) so it never contradicts order_tape's REJECT."""
    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 6, 23))
    w = _CapWriter()
    o = Orchestrator(cfg, w, res)
    o.executor.capital = 1_000                                   # can't afford even one lot

    band = _band(24108, 24092, atm=24100)
    monkeypatch.setattr(orch_mod, "compute_band", lambda *a, **k: band)
    monkeypatch.setattr(o, "_expiry_gate_ok", lambda: True)
    monkeypatch.setattr(o, "_approach_entry", lambda *a, **k: {
        "direction": "UP", "side": "UPPER", "dist": 4.0, "level": 24108.0, "breached": False,
        "confluence": {"verdict": "LONE", "same_side": [], "opp_side": [], "confidence": 0.0,
                       "conflicted": False, "same_pull": 0.0, "opp_pull": 0.0, "net_pull": 0.0}})

    o.process_round(_ms(10, 0), {"NIFTY": _snap(24104, ce=60.0, pe=60.0)})   # 10:00, in session

    dec = w.rows.get("decision_tape", [])
    assert dec, "no decision_tape row written"
    assert dec[-1]["decision"] == "HOLD"                         # NOT FIRE_UP
    assert dec[-1]["reason_code"] == "INSUFFICIENT_CAPITAL"      # carries the real reject reason
    rej = [r for r in w.rows.get("order_tape", []) if r["action"] == "REJECT"]
    assert rej and rej[-1]["exit_reason"] == "INSUFFICIENT_CAPITAL"   # the two tapes now agree


# ---- session lifecycle: open position at the close MUST be booked -----------
def _fire_open(o, monkeypatch, ts):
    """Open a real NIFTY position via the anticipatory path (forced band + approach)."""
    band = _band(24108, 24092, atm=24100)
    monkeypatch.setattr(orch_mod, "compute_band", lambda *a, **k: band)
    monkeypatch.setattr(o, "_expiry_gate_ok", lambda: True)
    monkeypatch.setattr(o, "_approach_entry", lambda *a, **k: {
        "direction": "UP", "side": "UPPER", "dist": 4.0, "level": 24108.0, "breached": False,
        "confluence": {"verdict": "LONE", "same_side": [], "opp_side": [], "confidence": 0.0,
                       "conflicted": False, "same_pull": 0.0, "opp_pull": 0.0, "net_pull": 0.0}})
    o.process_round(ts, {"NIFTY": _snap(24104, ce=60.0, pe=60.0)})


def test_open_position_at_close_is_force_flattened_into_the_tape(monkeypatch):
    """The determinism harness never asserts this: an open position at the 15:25 close MUST be
    force-booked into the binding order_tape (a held trade must never leave P&L uncaptured)."""
    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 6, 23))
    w = _CapWriter()
    o = Orchestrator(cfg, w, res)
    o.executor.capital = 100_000

    _fire_open(o, monkeypatch, _ms(10, 0))                       # opens NIFTY 24100 CE
    assert o.executor.position_active, "setup failed: no position opened"
    held = o.executor.position.strike

    # End the session with the position STILL OPEN -> finalize must force-flatten it.
    o.finalize(_ms(15, 25), {"NIFTY": _snap(24130, ce=80.0, pe=20.0)})

    exits = [r for r in w.rows.get("order_tape", []) if r["action"] == "EXIT"]
    assert exits, "no EXIT row written for the held position at the close"
    assert exits[-1]["strike"] == held and exits[-1]["exit_reason"] == "END_OF_SESSION"
    assert not o.executor.position_active                        # flat afterwards
    closed = [t for t in o.journal if t["status"] == "CLOSED"]
    assert closed and closed[-1]["exit_ts"] is not None          # round-trip journal closed it
    assert any(e["type"] == "SHUTDOWN" for e in w.events)        # session end is on the tape
    assert w.flushed >= 1                                        # tape flushed at finalize


def test_recompute_refuses_stale_inputs(monkeypatch):
    """Post-exit re-anchor must NOT compute against prior-round inputs (NIFTY fed-gapped this
    round): it flags levels_stale + emits FEED_GAP(STALE_INPUTS) instead of anchoring stale data."""
    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 6, 23))
    w = _CapWriter()
    o = Orchestrator(cfg, w, res)
    o._levels_stale = False
    # inputs stamped at an EARLIER round than the exit round we pass in
    o._nifty_band_inputs = (_ms(10, 0), _snap(24104), 24100, 10.0, "IMPLIED", 50)

    def _boom(*a, **k):
        raise AssertionError("must NOT recompute on stale inputs")
    monkeypatch.setattr(orch_mod, "compute_band", _boom)

    o._recompute_nifty_band(_ms(10, 1), None)                   # different ts -> stale

    assert o._levels_stale is True
    fg = [e for e in w.events if e["type"] == "FEED_GAP" and e["payload"].get("reason") == "STALE_INPUTS"]
    assert fg, "expected a FEED_GAP(STALE_INPUTS) event"
    assert not any(r["recalc_trigger"] == "post_exit" for r in w.rows.get("level_tape", []))   # nothing anchored


def test_event_strip_collapses_consecutive_repeats_but_tape_keeps_all():
    """2026-07-06: 1,047 REVERSAL_WATCH episodes drowned every FEED_GAP out of the 40-slot cockpit
    strip. Consecutive identical events must collapse to ONE strip row with a running count, while
    the binding event_tape still records every single one."""
    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 6, 23))
    w = _RecordingWriter()
    taped = []
    w.event = lambda t, p, ts_ms=None: taped.append((t, p))
    o = Orchestrator(cfg, w, res)
    for i in range(50):
        o._emit_event("REVERSAL_WATCH", {"instrument": "NIFTY", "d_basis": 1.0, "dir": 1}, ts_ms=1000 + i)
    o._emit_event("FEED_GAP", {"instrument": "LT", "reason": "MISSING_ATM_QUOTE", "stage": "basis"}, ts_ms=2000)
    o._emit_event("REVERSAL_WATCH", {"instrument": "NIFTY", "d_basis": 1.0, "dir": 1}, ts_ms=3000)
    strip = list(o._recent_events)
    assert len(strip) == 3, f"expected 3 collapsed strip rows, got {len(strip)}"
    assert strip[0]["type"] == "REVERSAL_WATCH" and strip[0]["count"] == 50 and strip[0]["ts"] == 1049
    assert strip[1]["type"] == "FEED_GAP"                       # NOT evicted by the flood
    assert strip[2]["type"] == "REVERSAL_WATCH" and strip[2]["count"] == 1
    mine = [t for t, p in taped if t in ("REVERSAL_WATCH", "FEED_GAP")]
    assert len(mine) == 52, "the binding tape must keep EVERY event (50+1+1)"
