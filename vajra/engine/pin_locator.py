"""§5.2 Pin-Strike / ATM Locator.

  * ATM / pin candidate (max-gamma strike) = strike nearest the FUTURE
    (not raw spot — premiums live in forward space).
  * Surface the highest-OI CE strike and highest-OI PE strike in the tracked
    window (the literal OI magnets, §2.1/§2.2).
  * Distance(spot -> max-OI strike) is a context field.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..mathx import round_to
from ..marketdata import MarketSnapshot


@dataclass(frozen=True)
class PinResult:
    instrument: str
    atm_by_future: int                 # max-gamma / pin candidate
    max_oi_ce: int | None
    max_oi_pe: int | None
    dist_spot_to_max_oi_ce: float | None
    dist_spot_to_max_oi_pe: float | None


def locate_pin(snapshot: MarketSnapshot, future: float, step: float) -> PinResult:
    atm_by_future = int(round_to(future, step))

    # Highest-OI strike per side within the tracked window (ties -> nearest spot, then lowest strike).
    # 2.2: one pass over the chain (it used to sort both sides every round: 22% of the round time).
    spot = snapshot.spot
    best_ce = best_pe = None
    oi_ce = oi_pe = d_ce = d_pe = 0
    # order: higher OI, then nearer the spot, then the lower strike (2.3: no tuple per quote)
    for q in snapshot.strikes.values():
        oi, k = q.oi, q.strike
        if q.right == "CE":
            if best_ce is None or oi > oi_ce:
                best_ce, oi_ce, d_ce = k, oi, abs(k - spot)
            elif oi == oi_ce:
                d = abs(k - spot)
                if d < d_ce or (d == d_ce and k < best_ce):
                    best_ce, d_ce = k, d
        elif q.right == "PE":
            if best_pe is None or oi > oi_pe:
                best_pe, oi_pe, d_pe = k, oi, abs(k - spot)
            elif oi == oi_pe:
                d = abs(k - spot)
                if d < d_pe or (d == d_pe and k < best_pe):
                    best_pe, d_pe = k, d
    max_oi_ce, max_oi_pe = best_ce, best_pe

    return PinResult(
        instrument=snapshot.instrument,
        atm_by_future=atm_by_future,
        max_oi_ce=max_oi_ce,
        max_oi_pe=max_oi_pe,
        dist_spot_to_max_oi_ce=(abs(snapshot.spot - max_oi_ce) if max_oi_ce is not None else None),
        dist_spot_to_max_oi_pe=(abs(snapshot.spot - max_oi_pe) if max_oi_pe is not None else None),
    )


__all__ = ["PinResult", "locate_pin"]
