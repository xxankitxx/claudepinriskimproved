"""Option greeks for the NIFTY chain (2.4) -- the engine exists to buy GAMMA, so it should see it.

Black-76 on the option-implied forward (no rate / dividend guess needed: the forward comes from put-call
parity at the ATM strike, exactly as the basis engine derives it). Everything is per ONE index point and
per ONE unit of the option:

    delta   premium change for +1 NIFTY point            (calls +, puts -)
    gamma   delta change for +1 NIFTY point              (always +)
    theta   premium decay per MINUTE of calendar time    (always <= 0)
    iv      annualised implied volatility (calendar time, 365 days)

``entry_geometry`` answers the question the entry really asks: with this option's delta and gamma, how
many NIFTY points must the index travel for the premium to gain X% (the trail arms) and how many against
us for it to lose Y% (the stop)? A high-gamma option needs few points to arm and gains speed as it goes;
a low-gamma one needs a long, sustained move and behaves like a plain directional bet.

Pure functions, no state, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)
YEAR_SECS = 365.0 * 86400.0
MIN_T = 60.0 / YEAR_SECS                 # never price with less than one minute to expiry


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def years_to_expiry(ts_ms: int, expiry_ts_ms: int) -> float:
    """Calendar time from ``ts_ms`` to ``expiry_ts_ms`` in years (floored at one minute)."""
    return max(MIN_T, (expiry_ts_ms - ts_ms) / 1000.0 / YEAR_SECS)


def black76(fwd: float, strike: float, t: float, sigma: float, right: str) -> float:
    """Undiscounted Black-76 price of a European option on the forward."""
    if sigma <= 0 or t <= 0:
        return max(0.0, fwd - strike) if right == "CE" else max(0.0, strike - fwd)
    v = sigma * math.sqrt(t)
    d1 = (math.log(fwd / strike) + 0.5 * v * v) / v
    d2 = d1 - v
    if right == "CE":
        return fwd * _cdf(d1) - strike * _cdf(d2)
    return strike * _cdf(-d2) - fwd * _cdf(-d1)


def implied_vol(price: float, fwd: float, strike: float, t: float, right: str) -> float | None:
    """Implied volatility by bisection (robust for the deep / near-expiry quotes Newton trips on).
    None when the price carries no time value (at or below intrinsic) or the inputs are unusable."""
    try:
        if not (price > 0 and fwd > 0 and strike > 0 and t > 0):
            return None
        intrinsic = max(0.0, fwd - strike) if right == "CE" else max(0.0, strike - fwd)
        if price <= intrinsic + 1e-6:
            return None
        lo, hi = 1e-4, 6.0
        if black76(fwd, strike, t, hi, right) < price:
            return None
        for _ in range(48):
            mid = 0.5 * (lo + hi)
            if black76(fwd, strike, t, mid, right) < price:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-6:
                break
        return 0.5 * (lo + hi)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


@dataclass(frozen=True)
class Greeks:
    strike: int
    right: str
    price: float
    iv: float
    delta: float           # per index point (calls +, puts -)
    gamma: float           # per index point, per point
    theta_min: float       # premium change per calendar minute (<= 0)
    fwd: float
    t_years: float

    @property
    def gamma_per_rupee(self) -> float:
        """Gamma bought per rupee of premium (x 10,000 for a readable number). With all-in sizing the
        position's gamma is capital x this, so it ranks strikes by convexity per unit of capital."""
        return 1e4 * self.gamma / self.price if self.price > 0 else 0.0

    def as_dict(self) -> dict:
        return {"strike": self.strike, "right": self.right, "price": round(self.price, 2),
                "iv": round(self.iv, 4), "delta": round(self.delta, 4), "gamma": round(self.gamma, 6),
                "theta_min": round(self.theta_min, 4), "gamma_per_rupee": round(self.gamma_per_rupee, 3)}


def greeks(price: float, fwd: float, strike: float, t: float, right: str) -> Greeks | None:
    """Greeks of one option from its market price. None when no implied vol can be backed out."""
    iv = implied_vol(price, fwd, strike, t, right)
    if iv is None:
        return None
    v = iv * math.sqrt(t)
    d1 = (math.log(fwd / strike) + 0.5 * v * v) / v
    pdf = _pdf(d1)
    delta = _cdf(d1) if right == "CE" else _cdf(d1) - 1.0
    gamma = pdf / (fwd * v)
    theta_year = -fwd * pdf * iv / (2.0 * math.sqrt(t))
    return Greeks(int(strike), right, float(price), iv, delta, gamma, theta_year / (365.0 * 1440.0),
                  float(fwd), float(t))


def forward_from_chain(snapshot, atm_strike: int, step: float) -> float | None:
    """The option-implied forward by put-call parity, averaged over the strikes around the ATM that
    have both legs quoted (one stale leg at the ATM no longer moves the forward)."""
    vals = []
    for k in (atm_strike, atm_strike - step, atm_strike + step):
        ce, pe = snapshot.quote(int(k), "CE"), snapshot.quote(int(k), "PE")
        if ce is None or pe is None:
            continue
        c, p = _mid(ce), _mid(pe)
        if c is None or p is None:
            continue
        vals.append(k + c - p)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _mid(q) -> float | None:
    """Mid of a sane bid/ask, else the last price."""
    try:
        ltp = float(q.ltp)
        b, a = float(q.bid), float(q.ask)
    except (TypeError, ValueError):
        return None
    if b > 0 and a > 0 and a >= b and (ltp <= 0 or (0.5 * ltp <= b and a <= 1.5 * ltp)):
        return 0.5 * (a + b)
    return ltp if ltp > 0 and math.isfinite(ltp) else None


def points_for_gain(g: Greeks, gain: float) -> float:
    """NIFTY points IN THE TRADE'S FAVOUR needed for the premium to gain ``gain`` rupees
    (second order: delta*x + gamma*x^2/2 = gain; theta ignored -- entries are judged on the move)."""
    d, gm = abs(g.delta), g.gamma
    if gain <= 0:
        return 0.0
    if gm <= 1e-12:
        return gain / d if d > 0 else math.inf
    return (-d + math.sqrt(d * d + 2.0 * gm * gain)) / gm


def points_for_loss(g: Greeks, loss: float) -> float:
    """NIFTY points AGAINST the trade for the premium to lose ``loss`` rupees (delta shrinks as it
    goes: delta*x - gamma*x^2/2 = loss). Infinite when the option cannot lose that much to second order."""
    d, gm = abs(g.delta), g.gamma
    if loss <= 0:
        return 0.0
    if gm <= 1e-12:
        return loss / d if d > 0 else math.inf
    disc = d * d - 2.0 * gm * loss
    if disc < 0:
        return math.inf
    return (d - math.sqrt(disc)) / gm


def entry_geometry(g: Greeks, arm_pct: float, stop_pct: float, horizon_min: float = 10.0) -> dict:
    """What this option asks of the index. ``arm_pts`` / ``stop_pts``: NIFTY points to the trail-arm gain
    and to the hard-stop loss. ``sigma_pts``: the one-sigma index move the option's own implied vol
    prices for ``horizon_min`` minutes. ``arm_sigmas`` = arm_pts / sigma_pts: how unusual a move the
    trade needs just to arm the trail -- the lower, the more gamma is working for the trade.
    ``convexity``: gain from a favourable one-sigma move divided by the loss from an adverse one."""
    arm = points_for_gain(g, arm_pct * g.price)
    stop = points_for_loss(g, stop_pct * g.price)
    sig = g.fwd * g.iv * math.sqrt(horizon_min / (365.0 * 1440.0))
    d, gm = abs(g.delta), g.gamma
    up = d * sig + 0.5 * gm * sig * sig
    dn = d * sig - 0.5 * gm * sig * sig
    return {"arm_pts": round(arm, 1), "stop_pts": (round(stop, 1) if math.isfinite(stop) else None),
            "sigma_pts": round(sig, 1), "arm_sigmas": round(arm / sig, 2) if sig > 0 else None,
            "convexity": round(up / dn, 3) if dn > 0 else None,
            "theta_pct_10min": round(100.0 * g.theta_min * 10.0 / g.price, 2) if g.price > 0 else None}


__all__ = ["Greeks", "greeks", "implied_vol", "black76", "years_to_expiry", "forward_from_chain",
           "points_for_gain", "points_for_loss", "entry_geometry", "YEAR_SECS"]
