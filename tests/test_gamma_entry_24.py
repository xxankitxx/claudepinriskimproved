"""2.4 gamma-aware entry.

13 Nov 2026 (a Friday, four days from expiry): four entries, two full stops. The options had almost no
gamma (they needed 26-32 NIFTY points beyond the entry just to arm the trail), the engine bought 7 points
BEFORE levels that then held, and one of them was a second attempt on the same side eight minutes after
the first had fizzled. The engine now prices each candidate entry and
  * with low gamma waits for the level to break instead of anticipating it,
  * after a failed same-side attempt also waits for the break,
  * can refuse an option whose stop is only a few index points away,
  * can pick the strike with the most gamma per rupee.
Every rule is a config switch; all off must be the 2.3 behaviour.
"""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from vajra import greeks as G
from vajra.app import build_calendar
from vajra.config import load_config
from vajra.execution import Executor
from vajra.engine.trigger_engine import TriggerBand
from vajra.marketdata import MarketSnapshot, StrikeQuote
from vajra.orchestrator import Orchestrator
from vajra.schema import Reason

ROOT = Path(__file__).resolve().parent.parent
OFF = dict(gamma_anticipate_max_arm_pts=0, gamma_min_arm_pts=0, retry_confirm_secs=0,
           momentum_guard_secs=0, rebuy_guard="off", max_breach_pts=0)


class _NullWriter:
    def __getattr__(self, n):
        return lambda *a, **k: None


def _orch(day=date(2026, 11, 13), execution=None, **entry):
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    cfg.raw.setdefault("entry", {}).update({**OFF, **entry})
    cfg.raw["execution"].update({"strike_select": "atm", "strike_select_expiry": "atm", **(execution or {})})
    return Orchestrator(cfg, _NullWriter(), build_calendar(cfg).resolve(day))


def _chain(o, hours_to_expiry, fwd=25000.0, iv=0.13, spot=None):
    """A NIFTY snapshot whose option prices are exact Black-76 values `hours_to_expiry` before expiry."""
    ts = o._expiry_ms - int(hours_to_expiry * 3_600_000)
    t = G.years_to_expiry(ts, o._expiry_ms)
    strikes = {}
    for k in range(int(fwd) - 300, int(fwd) + 301, 50):
        for r in ("CE", "PE"):
            px = round(G.black76(fwd, k, t, iv, r), 2)
            if px > 0.05:
                strikes[(k, r)] = StrikeQuote(k, r, px, px, "feed", 1000, 1000, round(px - 0.05, 2), round(px + 0.05, 2))
    return ts, MarketSnapshot(ts, "NIFTY", spot if spot is not None else fwd, 14.0, None, strikes)


def _entry(direction="UP", dist=5.0, level=25005.0):
    return {"direction": direction, "side": "UPPER" if direction == "UP" else "LOWER", "dist": dist,
            "level": level, "breached": dist < 0}


def test_plan_prices_the_atm_option_and_gamma_decides_the_mode():
    o = _orch(gamma_anticipate_max_arm_pts=18)
    ts, snap = _chain(o, hours_to_expiry=96)                 # four days out, like 13 Nov
    far = o._plan_entry(ts, "UP", snap, 25000)
    assert far["strike"] == 25000 and far["right"] == "CE" and far["strike_rule"] == "atm"
    assert far["delta"] == pytest.approx(0.5, abs=0.03) and far["iv"] == pytest.approx(0.13, abs=0.004)
    assert 22 < far["arm_pts"] < 40 and far["stop_pts"] > far["arm_pts"] and far["convexity"] < 1.06
    assert far["mode"] == "confirm"                          # low gamma: wait for the break
    ts2, snap2 = _chain(o, hours_to_expiry=3)                # expiry-day afternoon
    near = o._plan_entry(ts2, "DOWN", snap2, 25000)
    assert near["right"] == "PE" and near["delta"] < 0 and near["arm_pts"] < 12 and near["convexity"] > 1.15
    assert near["gamma"] > 4 * far["gamma"] and near["mode"] == "anticipate"
    assert o._plan_entry(ts, "UP", MarketSnapshot(ts, "NIFTY", 25000.0, 14.0, None, {}), 25000) is None


def test_low_gamma_waits_for_the_level_to_break_high_gamma_anticipates():
    o = _orch(gamma_anticipate_max_arm_pts=18, gamma_confirm_breach_pts=2)
    ts, snap = _chain(o, 96)
    o._entry_plan = o._plan_entry(ts, "UP", snap, 25000)
    assert o._entry_guards(ts, _entry("UP", 6.9), snap) == Reason.GAMMA_CONFIRM_WAIT      # 7 pts before: wait
    assert o._entry_guards(ts, _entry("UP", -1.0), snap) == Reason.GAMMA_CONFIRM_WAIT     # 1 pt through: not yet
    assert o._entry_guards(ts, _entry("UP", -2.0), snap) is None                          # 2 pts through: go
    ts2, snap2 = _chain(o, 3)
    o._entry_plan = o._plan_entry(ts2, "UP", snap2, 25000)
    assert o._entry_guards(ts2, _entry("UP", 6.9), snap2) is None                         # high gamma: anticipate


def test_a_thin_option_whose_stop_is_inside_the_noise_is_refused():
    o = _orch(gamma_min_arm_pts=7)
    ts, snap = _chain(o, 0.5, iv=0.11)                       # 30 minutes to expiry
    o._entry_plan = o._plan_entry(ts, "UP", snap, 25000)
    assert o._entry_plan["arm_pts"] < 7
    assert o._entry_guards(ts, _entry("UP", 5.0), snap) == Reason.GAMMA_TOO_TIGHT
    ts2, snap2 = _chain(o, 30)                               # the day before expiry: plenty of room
    o._entry_plan = o._plan_entry(ts2, "UP", snap2, 25000)
    assert o._entry_plan["arm_pts"] > 12
    assert o._entry_guards(ts2, _entry("UP", 5.0), snap2) is None


def test_after_a_failed_attempt_the_same_side_needs_the_break_for_a_while():
    o = _orch(retry_confirm_secs=1800, gamma_confirm_breach_pts=2)
    ts, snap = _chain(o, 3)
    o._entry_plan = None
    o._fail = {"dir": "UP", "ts": ts - 480_000}              # an UP attempt failed 8 minutes ago (13 Nov, 09:57)
    assert o._entry_guards(ts, _entry("UP", 6.9), snap) == Reason.RETRY_NEEDS_BREAK
    assert o._entry_guards(ts, _entry("DOWN", 6.9), snap) is None          # the other side is free
    assert o._entry_guards(ts, _entry("UP", -2.5), snap) is None           # the level broke: go
    assert o._entry_guards(ts + 1_400_000, _entry("UP", 6.9), snap) is None and o._fail is None   # 31 min later


def test_all_switches_off_changes_nothing():
    o = _orch()
    ts, snap = _chain(o, 96)
    o._entry_plan = o._plan_entry(ts, "UP", snap, 25000)
    o._fail = {"dir": "UP", "ts": ts - 1000}
    assert o._entry_plan["mode"] == "anticipate"
    assert o._entry_guards(ts, _entry("UP", 6.9), snap) is None


def test_gamma_strike_selection_takes_the_most_convex_tradable_strike():
    o = _orch(execution={"strike_select": "gamma", "gamma_strike_min_delta": 0.35, "gamma_strike_max_delta": 0.60,
                         "gamma_strike_max_otm_steps": 2})
    ts, snap = _chain(o, 96)
    plan = o._plan_entry(ts, "UP", snap, 25000)
    atm = G.greeks(snap.quote(25000, "CE").ltp, 25000.0, 25000, G.years_to_expiry(ts, o._expiry_ms), "CE")
    assert plan["strike_rule"] == "gamma" and plan["strike"] in (25050, 25100)
    assert plan["gamma_per_rupee"] > atm.gamma_per_rupee and 0.35 <= plan["delta"] <= 0.60
    down = o._plan_entry(ts, "DOWN", snap, 25000)
    assert down["strike"] in (24950, 24900) and down["right"] == "PE"
    # expiry day keeps the ATM unless strike_select_expiry says otherwise
    o2 = _orch(day=date(2026, 11, 17), execution={"strike_select": "gamma"})
    ts2, snap2 = _chain(o2, 3)
    assert o2.expiry.is_expiry_day and o2._plan_entry(ts2, "UP", snap2, 25000)["strike_rule"] == "atm"
    # nothing tradable in the delta band -> the ATM
    o3 = _orch(execution={"strike_select": "gamma", "gamma_strike_min_delta": 0.9, "gamma_strike_max_delta": 0.95})
    ts3, snap3 = _chain(o3, 96)
    p3 = o3._plan_entry(ts3, "UP", snap3, 25000)
    assert p3["strike"] == 25000 and p3["strike_rule"] == "atm"


def test_executor_buys_the_overridden_strike():
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    ex = Executor(cfg)
    ex.capital = 100_000
    s = {(24100, "CE"): StrikeQuote(24100, "CE", 120.0, 120.0, "feed", 1, 1, 120.0, 120.0),
         (24150, "CE"): StrikeQuote(24150, "CE", 95.0, 95.0, "feed", 1, 1, 95.0, 95.0)}
    snap = MarketSnapshot(0, "NIFTY", 24104.0, 14.0, None, s)
    band = TriggerBand("NIFTY", 24100, 24050, 50.0, 24108, 24108, 24150, 50.0, 24092, 24092, 0.0, "IMPLIED",
                       0.10, 1, 5.0, True, None)
    rows = ex.enter(0, "UP", snap, band, conviction=1.0, threshold=0.25, channels=[], is_expiry_day=False,
                    expiry_iso="2026-11-17", step=50, atm_strike=24100, strike_override=24150)
    assert rows[0]["action"] == "ENTRY" and rows[0]["strike"] == 24150 and rows[0]["fill_price"] == 95.0


def test_entry_context_carries_the_greeks_to_the_tape():
    o = _orch(gamma_anticipate_max_arm_pts=18)
    ts, snap = _chain(o, 96)
    o._entry_plan = o._plan_entry(ts, "UP", snap, 25000)
    o._fail = {"dir": "UP", "ts": ts - 5000}
    c = o._entry_context(ts, _entry("UP", -2.0))
    g = c["greeks"]
    assert g["strike"] == 25000 and g["mode"] == "confirm" and g["arm_pts"] > 20 and c["retry_after_fail"] is True
    o._entry_plan = None
    assert o._entry_context(ts, _entry("UP", 5.0))["greeks"] is None
