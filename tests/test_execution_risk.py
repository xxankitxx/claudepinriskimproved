"""Execution tests — full-capital sizing (lot 65) + trailing-stop exit (15% min)."""

from pathlib import Path
from vajra.config import load_config
from vajra.execution import Executor, TRAIL_SL, SL_HIT
from vajra.engine.trigger_engine import TriggerBand
from vajra.marketdata import MarketSnapshot, StrikeQuote

ROOT = Path(__file__).resolve().parent.parent


def _cfg():
    return load_config(str(ROOT / "vajra_config.yaml"))


def _snap(spot, ce, pe, atm=24100):
    # bid=ask=ltp=premium so the test math is spread-free
    s = {(atm, "CE"): StrikeQuote(atm, "CE", ce, ce, "feed", 1, 1, ce, ce),
         (atm, "PE"): StrikeQuote(atm, "PE", pe, pe, "feed", 1, 1, pe, pe)}
    return MarketSnapshot(0, "NIFTY", spot, 14.0, None, s)


def _band(upper, lower, atm=24100):
    return TriggerBand("NIFTY", atm, atm - 50, 50.0, upper, upper, atm + 50, 50.0,
                       lower, lower, 0.0, "IMPLIED", 0.10, 1, 5.0, True, None)


def _enter(ex, spot, ce, band):
    return ex.enter(0, "UP", _snap(spot, ce, 5.0), band, conviction=1.0, threshold=0.25,
                    channels=["NIFTY"], is_expiry_day=True,
                    expiry_iso="2026-06-16", step=50)


# ---- council v4: force_flatten must NOT book a phantom 0.0 exit on a feed gap ----
def test_force_flatten_no_phantom_zero_on_missing_quote():
    ex = Executor(_cfg()); ex.capital = 100_000
    _enter(ex, 24120, 40.0, _band(24108, 24092))
    assert ex.position_active
    gap = MarketSnapshot(0, "NIFTY", 24120, 14.0, None, {})   # 15:30 feed gap: NO quote for the held strike
    rows = ex.force_flatten(0, gap)
    assert rows and rows[0]["action"] == "REJECT" and rows[0]["exit_reason"] == "NO_QUOTE_AT_FLATTEN"
    assert ex.position_active                                  # left OPEN, not booked at 0.0
    assert ex.capital == 100_000                               # no phantom P&L booked


# ---- lot 65 + full-capital sizing -----------------------------------------
def test_lot_size_is_65():
    assert Executor(_cfg()).lot_size == 65


def test_full_capital_sizing_no_cap():
    ex = Executor(_cfg()); ex.capital = 100_000
    # lots = floor(100000 / (40 * 65)) = floor(38.46) = 38 ; full capital, no upper cap
    assert ex.compute_lots(40.0) == 38
    ex.capital = 10_000_000                                  # big capital -> no 200-lot cap
    assert ex.compute_lots(40.0) == 10_000_000 // (40 * 65)


def test_entry_deploys_full_capital():
    ex = Executor(_cfg()); ex.capital = 100_000
    _enter(ex, 24120, 40.0, _band(24108, 24092))
    p = ex.position
    assert p.lots == 38 and p.qty_total == 38 * 65
    assert 0.9 * 100_000 <= p.cost <= 100_000        # ~all of the capital deployed


# ---- trailing arms at +10%, books >= 7% minimum, then trails under the peak ----
def test_trailing_arms_at_10pct_books_min_7pct():
    # operator rule: once the peak crosses +10%, trail with a +7% MINIMUM floor. Right at the arm
    # point the wide-tier give-back (12% of a peak barely above entry) is looser than the 7% minimum,
    # so the min-profit floor is what actually binds here — exercised explicitly by the wide/mid/tight
    # tier tests below.
    ex = Executor(_cfg()); ex.capital = 100_000
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)                    # entry premium 40
    assert ex.manage(10_000, _snap(24124, 44.0, 5.0)) == []   # +10% -> trail arms, held
    assert ex.position.trail_armed
    # fade to the +7% floor (42.8) -> TRAIL_SL books the >=7% minimum
    rows = ex.manage(20_000, _snap(24118, 42.8, 5.0))
    assert rows and rows[0]["exit_reason"] == TRAIL_SL
    assert rows[0]["pnl_realized"] > 0
    assert (rows[0]["fill_price"] - 40.0) / 40.0 >= 0.069


def test_trailing_wide_tier_gives_more_room_right_after_arming():
    # operator rule (adaptive give-back): right after arming, give-back widens to 12% so a fresh
    # breach-driven move can breathe past its first wobble instead of stopping out on the old flat 6%.
    ex = Executor(_cfg()); ex.capital = 100_000
    ex.trail_give_pct_wide, ex.trail_give_pct_mid, ex.trail_give_pct = 0.12, 0.09, 0.06
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)                             # entry 40
    assert ex.manage(10_000, _snap(24140, 47.0, 5.0)) == []   # peak +17.5% -> wide tier (12% give)
    assert ex.position.trail_armed
    # a price the OLD flat-6% floor (47*0.94=44.18) would have stopped out at -> now HELD
    # (new floor = max(min-profit 42.8, 47*0.88=41.36) = 42.8)
    assert ex.manage(20_000, _snap(24135, 44.0, 5.0)) == []
    assert ex.position_active
    # but a genuine fade below the new (wider) floor still stops it out
    rows = ex.manage(30_000, _snap(24130, 42.5, 5.0))
    assert rows and rows[0]["exit_reason"] == TRAIL_SL


def test_trailing_mid_tier_tapers_give_back():
    # operator rule: as the move extends further (+20% to +35% peak), give-back tapers to 9% --
    # still wider than the old flat 6%, but tighter than the initial wide tier.
    ex = Executor(_cfg()); ex.capital = 100_000
    ex.trail_give_pct_wide, ex.trail_give_pct_mid, ex.trail_give_pct = 0.12, 0.09, 0.06
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)                             # entry 40
    assert ex.manage(10_000, _snap(24135, 50.0, 5.0)) == []   # peak +25% -> mid tier (9% give)
    assert ex.position.trail_armed
    # OLD flat-6% floor (50*0.94=47) would have stopped this out -> now HELD (new floor 50*0.91=45.5)
    assert ex.manage(20_000, _snap(24130, 46.0, 5.0)) == []
    assert ex.position_active
    rows = ex.manage(30_000, _snap(24128, 45.0, 5.0))          # breaches the new mid floor -> TRAIL_SL
    assert rows and rows[0]["exit_reason"] == TRAIL_SL
    assert (rows[0]["fill_price"] - 40.0) / 40.0 >= 0.12


def test_trailing_tight_tier_locks_in_hard_on_proven_move():
    # operator rule: once the move is clearly extended (+35% or more peak), give-back tightens BACK
    # to the original 6% -- a proven big move still gets locked in hard, matching the pre-adaptive rule.
    ex = Executor(_cfg()); ex.capital = 100_000
    ex.trail_give_pct_wide, ex.trail_give_pct_mid, ex.trail_give_pct = 0.12, 0.09, 0.06
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)                             # entry 40
    assert ex.manage(10_000, _snap(24160, 56.0, 5.0)) == []   # peak +40% -> tight tier (6% give)
    assert ex.position.trail_armed
    rows = ex.manage(20_000, _snap(24150, 52.0, 5.0))          # below the tight floor (56*0.94=52.64) -> TRAIL_SL
    assert rows and rows[0]["exit_reason"] == TRAIL_SL
    assert (rows[0]["fill_price"] - 40.0) / 40.0 >= 0.29


def test_trail_tier_only_tightens_never_loosens_back_up():
    # peak_premium never falls, so once the give-back tier tightens it can never loosen back up even
    # if price then pulls back within the SAME peak level (no re-widening exploit).
    ex = Executor(_cfg()); ex.capital = 100_000
    ex.trail_give_pct_wide, ex.trail_give_pct_mid, ex.trail_give_pct = 0.12, 0.09, 0.06
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)                             # entry 40
    ex.manage(10_000, _snap(24135, 50.0, 5.0))                # peak +25% -> mid tier, floor 45.5
    floor_after_mid = ex.position.trail_floor
    ex.manage(20_000, _snap(24132, 47.0, 5.0))                # price pulls back, peak UNCHANGED at 50
    assert ex.position.trail_floor == floor_after_mid          # floor must not loosen back to a wide-tier value
    assert ex.position_active


def test_winner_below_arm_is_held():
    # in profit but below the +10% arm -> HELD (only the hard stop acts below the arm)
    ex = Executor(_cfg()); ex.capital = 100_000
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)
    rows = ex.manage(10_000, _snap(24123, 43.2, 5.0))   # +8% -> below +10% arm
    assert rows == [] and ex.position_active
    assert not ex.position.trail_armed


def test_no_breakeven_exit():
    # operator removed breakeven: a trade that was green but never armed (+10%) and falls back
    # to entry is HELD, NOT booked at breakeven. Only the -12% hard stop closes it.
    # 2.2: the operator switched the protective stop ON (10 Nov 2026); this keeps the OFF path honest
    # (execution.breakeven_arm_pct: 0). The ON behaviour is in tests/test_protective_stop_22.py.
    cfg = _cfg(); cfg.raw["execution"]["breakeven_arm_pct"] = 0
    ex = Executor(cfg); ex.capital = 100_000
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)                       # entry 40
    assert ex.manage(10_000, _snap(24124, 43.0, 5.0)) == []   # +7.5%, trail NOT armed
    assert not ex.position.trail_armed
    assert ex.manage(15_000, _snap(24120, 40.0, 5.0)) == []   # back to entry -> HELD (no breakeven)
    assert ex.position_active
    rows = ex.manage(20_000, _snap(24112, 35.0, 5.0))         # -12.5% -> hard stop
    assert rows and rows[0]["exit_reason"] == SL_HIT


def test_hard_stop_on_loss():
    ex = Executor(_cfg()); ex.capital = 100_000
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)
    rows = ex.manage(10_000, _snap(24112, 35.0, 5.0))
    assert rows and rows[0]["exit_reason"] == SL_HIT and rows[0]["pnl_realized"] < 0


def test_capital_updates_on_close():
    ex = Executor(_cfg()); ex.capital = 100_000
    band = _band(24108, 24092)
    _enter(ex, 24120, 40.0, band)
    ex.manage(10_000, _snap(24135, 50.0, 5.0))   # peak +25% -> mid tier, floor 45.5
    ex.manage(20_000, _snap(24128, 45.0, 5.0))   # breaches the mid floor -> trail exit
    assert ex.capital > 100_000                       # booked profit added to capital
