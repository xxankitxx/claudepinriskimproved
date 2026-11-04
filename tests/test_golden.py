"""§11.3 formula golden tests (unit-level mirror of the verify gate).

NOTE: per the operator, synthetic/unit tests are NOT the binding proof of the
engine — recorded live tape is.  These guard the deterministic FORMULAS only.
"""

from vajra.mathx import round_to
from vajra.marketdata import MarketSnapshot, StrikeQuote
from vajra.engine.basis_engine import compute_basis
from vajra.engine.trigger_engine import compute_band


def _snap(spot, ce_atm, pe_atm, vwap_ce_itm, vwap_pe_itm, atm=24100, step=50):
    s = {
        (atm, "CE"): StrikeQuote(atm, "CE", ce_atm, ce_atm, "feed", 1, 1, ce_atm - .5, ce_atm + .5),
        (atm, "PE"): StrikeQuote(atm, "PE", pe_atm, pe_atm, "feed", 1, 1, pe_atm - .5, pe_atm + .5),
        (atm - step, "CE"): StrikeQuote(atm - step, "CE", 0, vwap_ce_itm, "feed", 1, 1, 0, 0),
        (atm + step, "PE"): StrikeQuote(atm + step, "PE", 0, vwap_pe_itm, "feed", 1, 1, 0, 0),
    }
    return MarketSnapshot(0, "NIFTY", spot, 14.0, None, s)


def test_round_half_up_trap():
    assert round_to(82.5, 5) == 85.0
    assert round_to(104.5, 5) == 105.0
    assert round_to(24110, 50) == 24100.0


def test_implied_future_and_basis():
    br = compute_basis(_snap(24110, 35, 15, 75, 95), 50, "IMPLIED")
    assert br.atm_strike == 24100
    assert br.future == 24120
    assert br.basis == 10


def test_band_premium():
    snap = _snap(24110, 35, 15, 75, 95)
    band = compute_band(snap, 24100, 10.0, "IMPLIED", 50,
                        pad_floor=0.10, level_round_step=5, itm_depth_strikes=1)
    assert band.raw_upper == 24135
    assert band.upper_level == 24125
    assert band.raw_lower == 24045
    assert band.lower_level == 24035


def test_band_discount_mirror():
    snap = _snap(24110, 35, 15, 75, 95)
    band = compute_band(snap, 24100, -10.0, "IMPLIED", 50,
                        pad_floor=0.10, level_round_step=5, itm_depth_strikes=1)
    # LEVEL = RAW - BASIS; discount (<0) adds to both
    assert band.upper_level == 24145
    assert band.lower_level == 24055


def test_actual_mode_uses_traded_future():
    s = {(24100, "CE"): StrikeQuote(24100, "CE", 35, 35, "feed", 1, 1, 0, 0),
         (24100, "PE"): StrikeQuote(24100, "PE", 15, 15, "feed", 1, 1, 0, 0)}
    snap = MarketSnapshot(0, "NIFTY", 24110, 14.0, future_traded=24125.0, strikes=s)
    br = compute_basis(snap, 50, "ACTUAL")
    assert br.future == 24125.0
    assert br.basis == 15.0
    assert br.basis_mode == "ACTUAL"
