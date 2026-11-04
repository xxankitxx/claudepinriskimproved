"""§5.1 Basis Engine — spot-to-future basis per instrument, in the correct mode.

    ATM_STRIKE = round(spot / step) * step
    MODE ACTUAL  (monthly-expiry session): FUTURE = traded near-month future
    MODE IMPLIED (other weeks):            FUTURE = ATM + (CE_ATM - PE_ATM)   # put-call parity
    BASIS = FUTURE - SPOT          (>0 premium, <0 discount)

Golden (IMPLIED): spot 24110 -> ATM 24100; CE 35, PE 15 -> FUTURE 24120; BASIS +10.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..mathx import round_to
from ..marketdata import MarketSnapshot
from ..schema import Reason


@dataclass(frozen=True)
class BasisResult:
    instrument: str
    spot: float
    atm_strike: int
    future: float | None
    basis: float | None
    basis_mode: str            # IMPLIED | ACTUAL
    ok: bool
    reason: str | None         # typed gap reason when ok is False (§0.4)


def compute_basis(
    snapshot: MarketSnapshot,
    step: float,
    basis_mode: str,
) -> BasisResult:
    spot = snapshot.spot
    atm = int(round_to(spot, step))

    if basis_mode == "ACTUAL":
        future = snapshot.future_traded
        if future is None:
            return BasisResult(snapshot.instrument, spot, atm, None, None,
                               basis_mode, False, Reason.NO_FUTURE_PRICE)
        basis = future - spot
        return BasisResult(snapshot.instrument, spot, atm, future, basis,
                           basis_mode, True, None)

    # IMPLIED — synthesize the future via put-call parity at the ATM strike.
    ce = snapshot.ce(atm)
    pe = snapshot.pe(atm)
    if ce is None or pe is None:
        return BasisResult(snapshot.instrument, spot, atm, None, None,
                           basis_mode, False, Reason.MISSING_ATM_QUOTE)
    future = atm + (ce.ltp - pe.ltp)
    basis = future - spot
    return BasisResult(snapshot.instrument, spot, atm, future, basis,
                       basis_mode, True, None)


__all__ = ["BasisResult", "compute_basis"]
