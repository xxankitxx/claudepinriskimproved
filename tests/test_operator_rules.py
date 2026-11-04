# Adopted from the independent teardown (T22): end-to-end checks of every operator-locked rule.
"""T22 — missing operator-rule tests (written by the reviewer; NOT part of the snapshot).
Each test pins one binding rule from BRIEF.md to the live yaml + the orchestrator path."""
import math
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from vajra.app import build_calendar
from vajra.config import load_config
from vajra.engine.trigger_engine import TriggerBand
from vajra.execution import Executor, SL_HIT, TRAIL_SL, EXPIRY_FLATTEN
from vajra.marketdata import MarketSnapshot, StrikeQuote
from vajra.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
D = date(2026, 10, 15)          # non-expiry
DX = date(2026, 10, 19)         # Monday expiry


class _W:
    def __init__(self): self.rows, self.events = {}, []
    def append(self, n, r): self.rows.setdefault(n, []).append(r)
    def event(self, t, p=None, ts_ms=None): self.events.append((t, p, ts_ms))
    def flush(self): pass
    def close(self): pass


def _cfg():
    return load_config(str(ROOT / "vajra_config.yaml"))


def _ms(h, m, s=0, d=D):
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST).timestamp() * 1000)


def _q(k, r, ltp):
    return StrikeQuote(k, r, ltp, ltp, "feed", 100, 100, ltp - 0.5, ltp + 0.5)


def _chain(spot, inst="NIFTY", step=50, ext_pts=100.0, width=220.0, n=6):
    atm = int(round(spot / step) * step)
    s = {}
    for k in range(atm - n * step, atm + n * step + 1, step):
        ext = ext_pts * math.exp(-((k - spot) / width) ** 2)
        s[(k, "CE")] = _q(k, "CE", round(max(spot - k, 0) + ext, 2))
        s[(k, "PE")] = _q(k, "PE", round(max(k - spot, 0) + ext, 2))
    return MarketSnapshot(0, inst, spot, 13.0, None, s)


BAND = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                   "IMPLIED", 0.1, 1, 5.0, True, None)


def _orch(monkeypatch, d=D):
    cfg = _cfg()
    w = _W()
    o = Orchestrator(cfg, w, build_calendar(cfg).resolve(d))
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: BAND)
    return o, w


def _with(snap, key, ltp):
    snap.strikes[key] = _q(key[0], key[1], ltp)
    return snap


def _orders(w, action=None):
    return [r for r in w.rows.get("order_tape", []) if action is None or r["action"] == action]


# ------------------------------------------------------------- 1. the live yaml IS the operator rule set
def test_yaml_carries_the_operator_rules():
    c = _cfg()
    ex = c.get("execution")
    assert ex["LIVE_ORDERS"] is False
    assert ex["lot_size"] == 65 and ex["starting_capital"] == 100000 and ex["deploy_full_capital"] is True
    assert ex["hard_sl_pct"] == 0.12 and ex["trail_activate_pct"] == 0.10 and ex["min_profit_pct"] == 0.07
    assert ex["trail_give_pct_wide"] == ex["trail_give_pct_mid"] == ex["trail_give_pct"] == 0.06
    assert ex["post_stop_cooldown_secs"] == 660 and ex["post_profit_cooldown_secs"] == 15
    assert ex["flatten_before"] == "15:38" and ex["last_entry"] == "15:29"
    cs = c.get("closing_session")          # 2.0.4 operator rule (21 Oct 2026): enter until 15:29, out by 15:40
    assert cs["last_entry"] == "15:29" and cs["flatten_before"] == "15:38"
    assert cs["expiry_day_last_entry"] == "15:29" and cs["expiry_day_flatten_before"] == "15:38"   # same on expiry
    assert c.get("expiry", "trade_start") == "09:21"
    assert c.get("entry", "proximity_pts") == 7
    assert c.get("freshness", "hold_min_secs") == 1200 and c.get("freshness", "sister_hold_min_secs") == 180
    assert c.get("trigger", "pad_floor") == 0.10


# ------------------------------------------------------------- 2. exits with the REAL yaml (no tier override)
def _ex_enter(prem=40.0):
    ex = Executor(_cfg()); ex.capital = 100_000
    s = {(24100, "CE"): StrikeQuote(24100, "CE", prem, prem, "feed", 1, 1, prem, prem)}
    ex.enter(0, "UP", MarketSnapshot(0, "NIFTY", 24104, 14, None, s), None, 1, 0, [], True, "x", 50,
             atm_strike=24100)
    return ex


def _m(ex, p):
    s = {(24100, "CE"): StrikeQuote(24100, "CE", p, p, "feed", 1, 1, p, p)}
    return ex.manage(_ms(11, 0), MarketSnapshot(0, "NIFTY", 24100, 14, None, s))


def test_trail_floor_is_flat_peak_x094_with_real_yaml():
    ex = _ex_enter(40.0)                     # peak 50 (+25%): floor = max(42.8, 47.0) = 47.0
    assert _m(ex, 50.0) == []
    assert _m(ex, 47.1) == [] and ex.position_active
    rows = _m(ex, 46.9)
    assert rows and rows[0]["exit_reason"] == TRAIL_SL


def test_trail_floor_just_after_arming_is_the_7pct_minimum():
    ex = _ex_enter(40.0)                     # peak 44 (+10%): floor = max(42.8, 41.36) = 42.8
    assert _m(ex, 44.0) == [] and ex.position.trail_armed
    assert _m(ex, 42.9) == []
    assert _m(ex, 42.8)[0]["exit_reason"] == TRAIL_SL


def test_hard_stop_boundary_minus_12pct():
    ex = _ex_enter(40.0)
    assert _m(ex, 35.3) == []               # -11.75% -> held
    assert _m(ex, 35.2)[0]["exit_reason"] == SL_HIT   # exactly -12%


def test_no_hard_stop_once_trail_armed():
    ex = _ex_enter(40.0)
    _m(ex, 44.0)
    rows = _m(ex, 30.0)                      # gap far below entry after arming -> TRAIL_SL, not SL_HIT
    assert rows[0]["exit_reason"] == TRAIL_SL


# ------------------------------------------------------------- 3. all-in compounding across trades
def test_all_in_compounds_through_the_day(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(10, 0), {"NIFTY": _chain(22955)})
    e1 = _orders(w, "ENTRY")[-1]
    pos = o.executor.position
    o.process_round(_ms(10, 0, 1), {"NIFTY": _with(_chain(22954), (pos.strike, pos.right), round(pos.entry_fill * 0.8, 2))})
    x1 = _orders(w, "EXIT")[-1]
    assert x1["exit_reason"] == SL_HIT
    cap = 100_000 + x1["pnl_realized"]
    assert abs(o.executor.capital - cap) < 1e-6
    t2 = _ms(10, 0, 1) + 660_000 + 1000
    o.process_round(t2, {"NIFTY": _chain(22956)})
    e2 = _orders(w, "ENTRY")[-1]
    assert e2 is not e1
    assert e2["qty"] == int(cap // (e2["fill_price"] * 65)) * 65    # lots = floor(capital/(prem*65))


# ------------------------------------------------------------- 4. the 09:21 - 15:25 window, end to end
def test_no_entry_before_0921_entry_at_0921(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(9, 20, 59), {"NIFTY": _chain(22955)})
    assert not o.executor.position_active
    assert w.rows["decision_tape"][-1]["reason_code"] == "BEFORE_TRADE_START"
    o.process_round(_ms(9, 21, 0), {"NIFTY": _chain(22955.5)})
    assert o.executor.position_active


def test_entry_allowed_at_1528_59(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(15, 10), {"NIFTY": _chain(22930)})      # clean pre-15:15 round (no entry: 30 pts away)
    assert not o.executor.position_active
    o.process_round(_ms(15, 28, 59), {"NIFTY": _chain(22955)})
    assert o.executor.position_active


def test_no_entry_at_1529(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(15, 10), {"NIFTY": _chain(22930)})
    o.process_round(_ms(15, 29), {"NIFTY": _chain(22955)})
    assert not o.executor.position_active
    assert w.rows["decision_tape"][-1]["reason_code"] == "AFTER_LAST_ENTRY"


def test_1538_flatten_inside_process_round(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(14, 0), {"NIFTY": _chain(22955)})
    pos = o.executor.position
    assert pos is not None
    o.process_round(_ms(15, 37, 59), {"NIFTY": _with(_chain(22955.5), (pos.strike, pos.right), pos.entry_fill)})
    assert o.executor.position_active
    o.process_round(_ms(15, 38, 0), {"NIFTY": _with(_chain(22956), (pos.strike, pos.right), pos.entry_fill)})
    assert not o.executor.position_active
    assert _orders(w, "EXIT")[-1]["exit_reason"] == EXPIRY_FLATTEN


def test_expiry_day_same_times_as_normal_days(monkeypatch):
    o, w = _orch(monkeypatch, DX)
    assert (o.entry_cutoff, o.flatten_at) == ("15:29", "15:38")
    o.process_round(_ms(15, 10, d=DX), {"NIFTY": _chain(22930)})      # clean pre-15:15 round, no entry
    o.process_round(_ms(15, 28, 59, d=DX), {"NIFTY": _chain(22955)})
    pos = o.executor.position
    assert pos is not None
    o.process_round(_ms(15, 37, 59, d=DX), {"NIFTY": _with(_chain(22955.5), (pos.strike, pos.right), pos.entry_fill)})
    assert o.executor.position_active
    o.process_round(_ms(15, 38, 0, d=DX), {"NIFTY": _with(_chain(22956), (pos.strike, pos.right), pos.entry_fill)})
    assert not o.executor.position_active
    assert _orders(w, "EXIT")[-1]["exit_reason"] == EXPIRY_FLATTEN


# ------------------------------------------------------------- 5. cooldowns wired from the REAL exits
def test_sl_hit_starts_exactly_11min_cooldown(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(10, 0), {"NIFTY": _chain(22955)})
    pos = o.executor.position
    t_sl = _ms(10, 0, 1)
    o.process_round(t_sl, {"NIFTY": _with(_chain(22954), (pos.strike, pos.right), round(pos.entry_fill * 0.8, 2))})
    assert _orders(w, "EXIT")[-1]["exit_reason"] == SL_HIT
    o.process_round(t_sl + 659_000, {"NIFTY": _chain(22955.5)})
    assert not o.executor.position_active
    assert w.rows["decision_tape"][-1]["reason_code"] == "POST_STOP_COOLDOWN"
    o.process_round(t_sl + 660_000, {"NIFTY": _chain(22955)})
    assert o.executor.position_active


def test_profit_exit_starts_exactly_15s_cooldown(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(10, 0), {"NIFTY": _chain(22955)})
    pos = o.executor.position
    k = (pos.strike, pos.right)
    o.process_round(_ms(10, 0, 1), {"NIFTY": _with(_chain(22955.5), k, round(pos.entry_fill * 1.25, 2))})
    t_x = _ms(10, 0, 2)
    o.process_round(t_x, {"NIFTY": _with(_chain(22956), k, round(pos.entry_fill * 1.16, 2))})
    x = _orders(w, "EXIT")[-1]
    assert x["exit_reason"] == TRAIL_SL and x["pnl_realized"] > 0
    o.process_round(t_x + 14_000, {"NIFTY": _chain(22955.5)})
    assert not o.executor.position_active
    o.process_round(t_x + 15_000, {"NIFTY": _chain(22955)})
    assert o.executor.position_active


# ------------------------------------------------------------- 6. no cap / no loss stop / no confluence veto
def test_no_trade_cap_and_no_loss_stop_after_many_losers(monkeypatch):
    o, w = _orch(monkeypatch)
    t = _ms(9, 30)
    for i in range(6):                                       # six hard stops in a row
        o.process_round(t, {"NIFTY": _chain(22955 + (i % 2) * 0.5)})
        assert o.executor.position_active, f"entry {i+1} blocked (cap/loss-stop?)"
        pos = o.executor.position
        t += 1000
        o.process_round(t, {"NIFTY": _with(_chain(22954.5), (pos.strike, pos.right), round(pos.entry_fill * 0.85, 2))})
        assert not o.executor.position_active
        t += 661_000
    assert o.executor.capital < 0.5 * 100_000                # deep drawdown, still trading


def test_opposite_confluence_never_vetoes(monkeypatch):
    from vajra.engine.continuation_validator import ContinuationSignal as CS
    o, w = _orch(monkeypatch)
    real = o._approach_entry
    def with_opp(band, snap, signals, dr):
        sig = {k: CS("BREACH_DOWN", -1, 0.9, True, True, False, 0.0, None, 0.0)
               for k in ("BANKNIFTY", "FINNIFTY", "HDFCBANK", "ICICIBANK", "RELIANCE")}
        return real(band, snap, sig, dr)
    monkeypatch.setattr(o, "_approach_entry", with_opp)
    o.process_round(_ms(10, 0), {"NIFTY": _chain(22955)})
    assert o.executor.position_active


def test_proximity_boundary_7pts(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(10, 0), {"NIFTY": _chain(22952.9)})   # 7.1 pts from 22960
    assert not o.executor.position_active
    o.process_round(_ms(10, 0, 1), {"NIFTY": _chain(22953.0)})   # 7.0 pts
    assert o.executor.position_active


# ------------------------------------------------------------- 7. re-anchor cadences through the orchestrator
def _anchors(w, inst):
    return [(r["ts"], r["recalc_trigger"]) for r in w.rows.get("level_tape", [])
            if r["instrument"] == inst and r["recalc_trigger"] != "held"]


def test_nifty_reanchors_every_20min_when_flat():
    cfg = _cfg(); w = _W()
    o = Orchestrator(cfg, w, build_calendar(cfg).resolve(D))
    spot = 22950.0
    for t in (_ms(9, 20), _ms(9, 39, 59), _ms(9, 40, 0), _ms(9, 59, 59), _ms(10, 0, 0)):
        spot += 0.5
        o.process_round(t, {"NIFTY": _chain(spot)})
    a = _anchors(w, "NIFTY")
    assert [ts for ts, _ in a] == [_ms(9, 20), _ms(9, 40), _ms(10, 0)], a


def test_sisters_reanchor_every_3min():
    cfg = _cfg(); w = _W()
    o = Orchestrator(cfg, w, build_calendar(cfg).resolve(D))
    bstep = int(cfg.strike_step("BANKNIFTY"))
    spot, bspot = 22950.0, 51000.0
    for t in (_ms(9, 20), _ms(9, 22, 59), _ms(9, 23, 0), _ms(9, 26, 0)):
        spot += 0.5; bspot += 1
        o.process_round(t, {"NIFTY": _chain(spot), "BANKNIFTY": _chain(bspot, "BANKNIFTY", bstep, 250, 500)})
    a = _anchors(w, "BANKNIFTY")
    assert [ts for ts, _ in a] == [_ms(9, 20), _ms(9, 23), _ms(9, 26)], a


def test_post_exit_reanchor_is_written(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(10, 0), {"NIFTY": _chain(22955)})
    pos = o.executor.position
    o.process_round(_ms(10, 0, 1), {"NIFTY": _with(_chain(22954), (pos.strike, pos.right), round(pos.entry_fill * 0.8, 2))})
    assert any(tr == "post_exit" for _, tr in _anchors(w, "NIFTY"))


def test_profit_cooldown_end_reanchors(monkeypatch):
    o, w = _orch(monkeypatch)
    o.process_round(_ms(10, 0), {"NIFTY": _chain(22955)})
    pos = o.executor.position; k = (pos.strike, pos.right)
    o.process_round(_ms(10, 0, 1), {"NIFTY": _with(_chain(22955.5), k, round(pos.entry_fill * 1.25, 2))})
    t_x = _ms(10, 0, 2)
    o.process_round(t_x, {"NIFTY": _with(_chain(22956), k, round(pos.entry_fill * 1.16, 2))})
    o.process_round(t_x + 5_000, {"NIFTY": _chain(22931)})        # a round inside the window (live: 4 Hz)
    o.process_round(t_x + 15_000, {"NIFTY": _chain(22930)})       # far from levels: no entry, but re-anchor
    assert any(tr == "post_cooldown" and ts == t_x + 15_000 for ts, tr in _anchors(w, "NIFTY"))


# ------------------------------------------------------------- 8. Monday 19 Oct expiry uses the locked formula
def test_expiry_day_1019_uses_locked_legacy_formula():
    cfg = _cfg(); w = _W()
    o = Orchestrator(cfg, w, build_calendar(cfg).resolve(DX))
    assert o.expiry.is_expiry_day and o.regime.name == "EXPIRY"
    o.process_round(_ms(9, 20, d=DX), {"NIFTY": _chain(24110, ext_pts=8.0, width=60.0)})
    rows = [r for r in w.rows["level_tape"] if r["instrument"] == "NIFTY"]
    r = rows[-1]
    assert r["band_method"] == "legacy" and r["narrowed"] is False
    # K +/- round(1.1*VWAP(1-ITM)) - basis, rounded to 5
    from vajra.mathx import round_to
    assert r["upper_level"] == r["k_ce"] + round_to(r["vwap_ce"] * 1.1, 5) - r["basis"]
    assert r["lower_level"] == r["k_pe"] - round_to(r["vwap_pe"] * 1.1, 5) - r["basis"]
