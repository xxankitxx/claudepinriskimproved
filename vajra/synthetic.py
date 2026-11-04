"""DEV-ONLY market SIMULATOR (NON-BINDING) — Black-Scholes, expiry-day regime.

The operator does not treat synthetic data as proof — only recorded live tape is
binding.  This simulator exists so the dashboard can be watched on something that
behaves like a real expiry session and so the gates can run.

It is calibrated to the REAL recorded MYTHOS tape (vajra/data/real_nifty_day.csv,
extracted from a real 2026-06-24 session: NIFTY ~24000, ATM IV ~0.12, a trending
day):

  * Options are priced by Black-Scholes at a SMALL time-to-expiry, so the deep-ITM
    extrinsic is thin (tight gamma-trigger band, per §5.3) and ATM gamma is sharp
    (small spot moves -> large % option moves, the expiry-day regime VAJRA lives in).
  * NIFTY follows a scripted path that coils, breaks a pin-risk level and HOLDS it
    long enough to confirm (ATM-stable), then RUNS — so the position rides a real
    move and the §7 trailing stop books it.  The financial bloc mirrors NIFTY;
    other constituents burst on their own staggered schedules.

Deterministic: no RNG, no wall-clock — pure functions of the round index, so a
recorded session replays byte-identically.
"""

from __future__ import annotations

import csv
import math
from datetime import date, datetime, time as dtime
from pathlib import Path

from .marketdata import MarketSnapshot, StrikeQuote
from .mathx import round_to
from .timeutil import IST

_DATA = Path(__file__).resolve().parent / "data" / "real_nifty_day.csv"

_BASE = {
    "NIFTY": (24100.0, 50.0), "BANKNIFTY": (52000.0, 100.0), "FINNIFTY": (23500.0, 50.0),
    "HDFCBANK": (1650.0, 5.0), "ICICIBANK": (1180.0, 5.0), "RELIANCE": (1400.0, 5.0),
    "BHARTIARTL": (1650.0, 5.0), "LT": (3600.0, 10.0), "INFY": (1550.0, 5.0),
    "SBIN": (820.0, 5.0), "AXISBANK": (1120.0, 5.0), "KOTAKBANK": (1780.0, 5.0),
    "ITC": (415.0, 1.0), "FEDERALBNK": (210.0, 1.0), "INDUSINDBK": (980.0, 5.0),
    "AUBANK": (750.0, 5.0), "BANKBARODA": (250.0, 1.0), "IDFCFIRSTB": (75.0, 1.0),
    "BAJFINANCE": (920.0, 5.0), "BSE": (2600.0, 10.0), "SHRIRAMFIN": (640.0, 5.0),
    "BAJAJFINSV": (2000.0, 10.0), "SBILIFE": (1550.0, 5.0),
}
# Betas vs NIFTY for the correlated co-movement of constituents.
_BETA = {"BANKNIFTY": 1.13, "FINNIFTY": 1.18, "HDFCBANK": 1.0, "ICICIBANK": 1.05,
         "SBIN": 1.2, "AXISBANK": 1.15, "KOTAKBANK": 0.95}
_BLOC = {"BANKNIFTY", "FINNIFTY"}
_LEANERS = {"HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"}

# Real ATM IV (from the recorded tape) and a representative India VIX.
_IV = 0.12


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float, right: str) -> float:
    """Black-Scholes (r=0). T in years.  Returns option premium."""
    if T <= 1e-9 or sigma <= 1e-9 or S <= 0:
        return max(0.0, S - K) if right == "CE" else max(0.0, K - S)
    vt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / vt
    d2 = d1 - vt
    if right == "CE":
        return S * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - S * _ncdf(-d1)


# --- NIFTY scripted path (offsets from a fixed ATM=24100) --------------------
# Each breakout COILS, breaks a level and HOLDS it ATM-stable (|offset|<=24) long
# enough to confirm (>=120s), then RUNS so the position profits, then fades so the
# trailing stop books it.  Three trades: UP, DOWN, UP.
# Each trade is preceded by a >=180s FLAT coil (so the consolidation-anchored §5.4
# engine actually anchors the levels there), then a break of those HELD levels that
# holds long enough to confirm, then a run that the trailing stop books.  Coils are
# spaced > the 10-min hold so each one re-anchors.
_WAY = [
    (0, 0), (12, 0),                             # coil ~180s -> ANCHOR levels
    (13, 30), (30, 28),                          # break UP through the held level, HOLD -> confirm
    (42, 62), (52, 30), (58, 6),                 # RUN up, fade -> trail books            (trade 1 CE)
    (60, 2), (72, 2),                            # coil ~180s (past the 10-min hold) -> RE-ANCHOR
    (73, -30), (90, -28),                        # break DOWN through the held level, HOLD
    (102, -62), (112, -30), (118, -4),           # RUN down, recover -> trail books       (trade 2 PE)
    (120, 0), (132, 0),                          # coil -> RE-ANCHOR
    (133, 30), (142, 60), (149, 40),             # break UP, RUN                          (trade 3 CE)
]


def _nifty_offset(r: float) -> float:
    pts = _WAY
    if r <= pts[0][0]:
        return pts[0][1]
    if r >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if r <= pts[i][0]:
            r0, v0 = pts[i - 1]
            r1, v1 = pts[i]
            frac = (r - r0) / (r1 - r0) if r1 != r0 else 0.0
            return v0 + (v1 - v0) * frac
    return pts[-1][1]


def _phase(inst: str) -> float:
    return (sum((i + 1) * ord(c) for i, c in enumerate(inst)) % 997) / 997.0


def _osc(inst: str, r: int, step: float) -> float:
    ph = _phase(inst)
    cyc = (r + ph * 44.0) % 44.0
    if cyc < 6.0:                                       # staggered independent burst
        d = 1 if (int((r + ph * 44.0) // 44.0) % 2 == 0) else -1
        return 0.55 * step * d
    return 0.20 * step * math.sin(2 * math.pi * (r / 9.0 + ph))


def _t_years(r: int, n_rounds: int) -> float:
    mins_left = (n_rounds - r) * 1.0 + 150.0            # ~150..270 min to expiry
    return mins_left / (60.0 * 24.0 * 365.0)


def _chain(inst, spot, atm, step, n, T, cum_vol) -> dict:
    strikes: dict[tuple[int, str], StrikeQuote] = {}
    for k in range(-n, n + 1):
        K = int(atm + k * step)
        ce = bs_price(spot, K, T, _IV, "CE")
        pe = bs_price(spot, K, T, _IV, "PE")
        # OI magnets: a put wall below and a call wall above (round levels), ATM ridge.
        wall = 4200 if (K == atm - 2 * step or K == atm + 2 * step) else 0
        oi = int(1000 * (n + 1 - abs(k)) + wall)
        vol = int(1500 * cum_vol * (n + 1 - abs(k)))
        strikes[(K, "CE")] = StrikeQuote(K, "CE", round(ce, 2), round(ce, 4), "feed",
                                         vol, oi, round(ce - 0.4, 2), round(ce + 0.4, 2))
        strikes[(K, "PE")] = StrikeQuote(K, "PE", round(pe, 2), round(pe, 4), "feed",
                                         vol, oi, round(pe - 0.4, 2), round(pe + 0.4, 2))
    return strikes


def generate_rounds(cfg, session_date: date, n_rounds: int = 150, dt_secs: int = 15,
                    start_hhmm: str = "13:00"):
    """Yield (round_ts_ms, {instrument: MarketSnapshot}) for a simulated expiry session."""
    universe = cfg.universe
    n = cfg.strikes_each_side
    h, m = [int(x) for x in start_hhmm.split(":")]
    start_ms = int(datetime.combine(session_date, dtime(h, m, 0), tzinfo=IST).timestamp() * 1000)

    cum: dict[str, float] = {inst: 0.0 for inst in universe}
    rounds = []
    for r in range(n_rounds):
        ts_ms = start_ms + r * dt_secs * 1000
        T = _t_years(r, n_rounds)
        nifty_off = _nifty_offset(r)
        nifty_ret = nifty_off / 24100.0                 # NIFTY fractional move (for betas)
        bucket: dict[str, MarketSnapshot] = {}
        for inst in universe:
            base, step = _BASE.get(inst, (1000.0, 5.0))
            if inst == "NIFTY":
                spot = base + nifty_off
            elif inst in _BLOC or inst in _LEANERS:
                beta = _BETA.get(inst, 1.0)
                wob = 0.05 * step * math.sin(2 * math.pi * (r / 9.0 + _phase(inst)))
                spot = base * (1.0 + beta * nifty_ret) + wob
            else:
                spot = base + _osc(inst, r, step)
            cum[inst] += 1.0 + (0.6 if abs(nifty_off) > 12 and inst in (_BLOC | _LEANERS | {"NIFTY"}) else 0.0)
            atm = int(round_to(spot, step))
            strikes = _chain(inst, spot, atm, step, n, T, cum[inst])
            bucket[inst] = MarketSnapshot(ts_ms=ts_ms, instrument=inst, spot=round(spot, 2),
                                          vix=round(13.0 + 2.0 * math.sin(r / 11.0), 2),
                                          future_traded=None, strikes=strikes)
        rounds.append((ts_ms, bucket))
    return rounds


__all__ = ["generate_rounds", "bs_price"]
