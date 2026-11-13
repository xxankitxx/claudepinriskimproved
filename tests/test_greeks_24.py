"""2.4 option greeks: the numbers the gamma-aware entry rests on."""
import math
import random

import pytest

from vajra import greeks as G
from vajra.marketdata import MarketSnapshot, StrikeQuote


def test_black76_textbook_values_and_parity():
    c = G.black76(100.0, 100.0, 1.0, 0.2, "CE")
    p = G.black76(100.0, 100.0, 1.0, 0.2, "PE")
    assert c == pytest.approx(7.9656, abs=1e-3) and p == pytest.approx(c, abs=1e-9)   # ATM forward: C == P
    for f, k, t, s in ((25000, 25100, 4 / 365, 0.12), (25000, 24800, 0.5 / 365, 0.2), (58000, 58500, 20 / 365, 0.15)):
        assert G.black76(f, k, t, s, "CE") - G.black76(f, k, t, s, "PE") == pytest.approx(f - k, abs=1e-6)
    assert G.black76(100, 90, 0.0, 0.2, "CE") == 10.0 and G.black76(100, 90, 1.0, 0.0, "PE") == 0.0


def test_implied_vol_round_trips_across_the_chain_and_time():
    rnd = random.Random(4)
    for _ in range(400):
        f = rnd.uniform(22000, 26000)
        k = round((f + rnd.uniform(-400, 400)) / 50) * 50
        t = rnd.choice([10 / 525600, 2 / 24 / 365, 1 / 365, 4 / 365, 20 / 365])
        s = rnd.uniform(0.07, 0.6)
        r = rnd.choice(["CE", "PE"])
        px = G.black76(f, k, t, s, r)
        intrinsic = max(0.0, f - k) if r == "CE" else max(0.0, k - f)
        if px - intrinsic < 0.05:                 # no usable time value: nothing to back out
            continue
        iv = G.implied_vol(px, f, k, t, r)
        assert iv == pytest.approx(s, rel=2e-3, abs=2e-4), (f, k, t, s, r)


def test_implied_vol_refuses_prices_without_time_value_or_garbage():
    assert G.implied_vol(99.0, 25100, 25000, 1 / 365, "CE") is None      # below intrinsic (100)
    assert G.implied_vol(0.0, 25000, 25000, 1 / 365, "CE") is None
    assert G.implied_vol(float("nan"), 25000, 25000, 1 / 365, "CE") is None
    assert G.implied_vol(50.0, 25000, 0, 1 / 365, "CE") is None
    assert G.greeks(99.0, 25100, 25000, 1 / 365, "CE") is None


def test_greeks_match_finite_differences_of_the_price():
    f, k, t, s = 25000.0, 25050.0, 3 / 365, 0.13
    for r in ("CE", "PE"):
        px = G.black76(f, k, t, s, r)
        g = G.greeks(px, f, k, t, r)
        h = 1.0
        d_fd = (G.black76(f + h, k, t, s, r) - G.black76(f - h, k, t, s, r)) / (2 * h)
        g_fd = (G.black76(f + h, k, t, s, r) - 2 * px + G.black76(f - h, k, t, s, r)) / (h * h)
        dt = 1.0 / (365 * 1440)
        th_fd = G.black76(f, k, t - dt, s, r) - px
        assert g.delta == pytest.approx(d_fd, abs=2e-4) and g.gamma == pytest.approx(g_fd, rel=2e-2)
        assert g.theta_min == pytest.approx(th_fd, rel=3e-2) and g.theta_min < 0
        assert (g.delta > 0) == (r == "CE") and g.gamma > 0
    c, p = G.greeks(G.black76(f, k, t, s, "CE"), f, k, t, "CE"), G.greeks(G.black76(f, k, t, s, "PE"), f, k, t, "PE")
    assert c.gamma == pytest.approx(p.gamma, rel=1e-3) and c.delta - p.delta == pytest.approx(1.0, abs=1e-3)


def test_gamma_grows_as_expiry_nears_and_peaks_at_the_money():
    f, s = 25000.0, 0.12
    by_t = [G.greeks(G.black76(f, 25000, t, s, "CE"), f, 25000, t, "CE").gamma for t in (5 / 365, 1 / 365, 3 / 24 / 365)]
    assert by_t[0] < by_t[1] < by_t[2]
    t = 1 / 365
    by_k = {k: G.greeks(G.black76(f, k, t, s, "CE"), f, k, t, "CE") for k in (24850, 25000, 25150)}
    assert by_k[25000].gamma > by_k[24850].gamma and by_k[25000].gamma > by_k[25150].gamma
    assert by_k[25150].gamma_per_rupee > by_k[25000].gamma_per_rupee > by_k[24850].gamma_per_rupee   # convexity per rupee rises OTM


def test_entry_geometry_points_agree_with_repricing():
    f, k, s = 25000.0, 25000.0, 0.12
    for t in (4 / 365, 1 / 365, 2 / 24 / 365):
        px = G.black76(f, k, t, s, "CE")
        g = G.greeks(px, f, k, t, "CE")
        geo = G.entry_geometry(g, 0.10, 0.12)
        up = G.black76(f + geo["arm_pts"], k, t, s, "CE")
        dn = G.black76(f - geo["stop_pts"], k, t, s, "CE")
        assert up / px - 1 == pytest.approx(0.10, abs=0.012)      # second-order estimate vs full reprice
        assert dn / px - 1 == pytest.approx(-0.12, abs=0.015)
        assert geo["arm_pts"] < geo["stop_pts"] and geo["convexity"] > 1.0
    far = G.entry_geometry(G.greeks(G.black76(f, k, 4 / 365, s, "CE"), f, k, 4 / 365, "CE"), 0.10, 0.12)
    near = G.entry_geometry(G.greeks(G.black76(f, k, 2 / 24 / 365, s, "CE"), f, k, 2 / 24 / 365, "CE"), 0.10, 0.12)
    assert near["arm_pts"] < far["arm_pts"] / 3 and near["arm_sigmas"] < far["arm_sigmas"]
    assert near["convexity"] > far["convexity"]


def test_points_for_loss_is_infinite_when_the_option_cannot_lose_that_much():
    g = G.Greeks(25000, "CE", 10.0, 0.2, 0.05, 0.004, -0.01, 25000.0, 1e-4)
    assert math.isinf(G.points_for_loss(g, 5.0)) and G.points_for_gain(g, 0.0) == 0.0


def test_forward_from_chain_is_the_parity_median_and_survives_a_bad_leg():
    def q(k, r, px, bid=None, ask=None):
        return StrikeQuote(k, r, px, px, "feed", 1, 1, bid if bid is not None else px - 0.05, ask if ask is not None else px + 0.05)
    f = 25012.0
    strikes = {}
    for k in (24950, 25000, 25050):
        c = max(0.0, f - k) + 80.0
        strikes[(k, "CE")] = q(k, "CE", c)
        strikes[(k, "PE")] = q(k, "PE", c - (f - k))
    snap = MarketSnapshot(0, "NIFTY", 25005.0, 14.0, None, strikes)
    assert G.forward_from_chain(snap, 25000, 50) == pytest.approx(f, abs=0.01)
    strikes[(25000, "PE")] = q(25000, "PE", 40.0)                 # one stale leg at the ATM
    assert G.forward_from_chain(MarketSnapshot(0, "NIFTY", 25005.0, 14.0, None, strikes), 25000, 50) == pytest.approx(f, abs=0.01)
    assert G.forward_from_chain(MarketSnapshot(0, "NIFTY", 25005.0, 14.0, None, {}), 25000, 50) is None
