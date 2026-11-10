"""2.2 protective stop (execution.breakeven_arm_pct / breakeven_lock_pct).

15-day tape study: 41% of the trades that ended at the -12% hard stop had first been +5% or better.
The rule is OFF in the shipped config (the operator rule was "no breakeven"); when switched on it
must only ever act between the arm point and the trail's own arm point, and it must not start the
11-minute post-stop cooldown.
"""
from pathlib import Path

from vajra.config import load_config
from vajra.engine.trigger_engine import TriggerBand
from vajra.execution import Executor, TRAIL_SL, SL_HIT, BE_STOP
from vajra.marketdata import MarketSnapshot, StrikeQuote

ROOT = Path(__file__).resolve().parent.parent


def _cfg(**ex):
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    cfg.raw["execution"].update(ex)
    return cfg


def _snap(ce, atm=24100):
    s = {(atm, "CE"): StrikeQuote(atm, "CE", ce, ce, "feed", 1, 1, ce, ce),
         (atm, "PE"): StrikeQuote(atm, "PE", 5.0, 5.0, "feed", 1, 1, 5.0, 5.0)}
    return MarketSnapshot(0, "NIFTY", 24120, 14.0, None, s)


def _band():
    return TriggerBand("NIFTY", 24100, 24050, 50.0, 24108, 24108, 24150, 50.0,
                       24092, 24092, 0.0, "IMPLIED", 0.10, 1, 5.0, True, None)


def _open(ex, prem=100.0):
    ex.capital = 100_000
    rows = ex.enter(0, "UP", _snap(prem), _band(), conviction=1.0, threshold=0.25, channels=["NIFTY"],
                    is_expiry_day=True, expiry_iso="2026-06-16", step=50)
    assert rows[0]["action"] == "ENTRY"


def _walk(ex, prices):
    for i, p in enumerate(prices):
        rows = ex.manage(1000 * (i + 1), _snap(p))
        if rows:
            return rows[0]
    return None


def test_shipped_config_has_the_rule_on_and_off_behaves_as_before():
    on = Executor(_cfg())
    assert on.breakeven_arm_pct == 0.06 and on.breakeven_lock_pct == 0.01   # operator, 10 Nov 2026
    ex = Executor(_cfg(breakeven_arm_pct=0))
    assert ex.breakeven_arm_pct == 0
    _open(ex)
    r = _walk(ex, [106.5, 101.0, 95.0, 89.0, 87.9])           # +6.5% then all the way down
    assert r["exit_reason"] == SL_HIT and r["fill_price"] == 87.9


def test_protective_stop_exits_near_entry_after_the_trade_was_up():
    ex = Executor(_cfg(breakeven_arm_pct=0.06, breakeven_lock_pct=0.01))
    _open(ex)
    assert _walk(ex, [103.0, 99.0, 95.0]) is None            # never reached +6%: only the hard stop applies
    assert not ex.position.be_armed
    r = _walk(ex, [106.2, 104.0, 102.0, 101.0])              # reached +6%, came back to +1%
    assert r["exit_reason"] == BE_STOP and r["fill_price"] == 101.0 and r["pnl_realized"] > 0
    assert not ex.position_active


def test_protective_stop_hands_over_to_the_trail_once_it_arms():
    ex = Executor(_cfg(breakeven_arm_pct=0.06, breakeven_lock_pct=0.01))
    _open(ex)
    r = _walk(ex, [106.5, 110.5, 108.0, 107.0])              # +10% armed the trail: floor = +7%
    assert r["exit_reason"] == TRAIL_SL and r["fill_price"] == 107.0
    assert ex.position.trail_armed


def test_hard_stop_still_applies_before_the_arm_point():
    ex = Executor(_cfg(breakeven_arm_pct=0.06, breakeven_lock_pct=0.01))
    _open(ex)
    r = _walk(ex, [104.0, 96.0, 88.0])
    assert r["exit_reason"] == SL_HIT


def test_a_gap_through_the_lock_still_exits_as_a_protective_stop():
    ex = Executor(_cfg(breakeven_arm_pct=0.06, breakeven_lock_pct=0.01))
    _open(ex)
    r = _walk(ex, [107.0, 97.0])                             # jumped past +1% to -3% in one round
    assert r["exit_reason"] == BE_STOP and r["pnl_realized"] < 0


def test_trough_is_tracked_for_the_journal():
    ex = Executor(_cfg(breakeven_arm_pct=0))
    _open(ex)
    _walk(ex, [103.0, 96.5, 99.0])
    assert ex.position.trough_premium == 96.5 and ex.position.peak_premium == 103.0
