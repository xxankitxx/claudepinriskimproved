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

    ce_strikes = snapshot.strikes_for("CE")
    pe_strikes = snapshot.strikes_for("PE")

    # Highest-OI strike within the tracked window (ties -> nearest spot, then lowest strike).
    def _max_oi(quotes):
        if not quotes:
            return None
        best = max(quotes, key=lambda q: (q.oi, -abs(q.strike - snapshot.spot), -q.strike))
        return best.strike

    max_oi_ce = _max_oi(ce_strikes)
    max_oi_pe = _max_oi(pe_strikes)

    return PinResult(
        instrument=snapshot.instrument,
        atm_by_future=atm_by_future,
        max_oi_ce=max_oi_ce,
        max_oi_pe=max_oi_pe,
        dist_spot_to_max_oi_ce=(abs(snapshot.spot - max_oi_ce) if max_oi_ce is not None else None),
        dist_spot_to_max_oi_pe=(abs(snapshot.spot - max_oi_pe) if max_oi_pe is not None else None),
    )


__all__ = ["PinResult", "locate_pin"]
