"""§5.5 Basis-Change Monitor — operationalizes §2.4 (the rate of change is the signal).

    D_BASIS      = BASIS_t - BASIS_(t-dt)
    D_BASIS_EWMA = ewma(D_BASIS, halflife = basis_ewma_hl)        # default 5 min
    ABRUPT       = |D_BASIS - D_BASIS_EWMA| > abrupt_k * rolling_std(D_BASIS)

  * ABRUPT -> reversal-watch flag (a caution into §5.6, not a standalone entry).
  * Minor changes below ``noise_floor_pts`` are ignored entirely.
  * Time weighting: basis signals weighted high before 12:30, decaying to
    near-zero after 14:00 (convergence kills the edge) — basis_signal_weight(t).

Stateful per instrument; decayed on tape-ts deltas for deterministic replay.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..mathx import TimeEWMA, RollingStats
from ..timeutil import basis_signal_weight


@dataclass(frozen=True)
class BasisMonitorResult:
    d_basis: float
    d_basis_ewma: float
    abrupt_flag: bool
    abrupt_dir: int                # sign of the abrupt move (-1/0/+1); 0 if not abrupt
    signal_weight: float


class BasisMonitor:
    def __init__(
        self,
        ewma_halflife_min: float = 5.0,
        abrupt_k: float = 2.5,
        noise_floor_pts: float = 1.0,
        std_window: int = 30,
        full_until: str = "12:30",
        near_zero_after: str = "14:00",
    ):
        self.abrupt_k = float(abrupt_k)
        self.noise_floor_pts = float(noise_floor_pts)
        self.full_until = full_until
        self.near_zero_after = near_zero_after

        self._ewma = TimeEWMA(halflife_seconds=ewma_halflife_min * 60.0)
        self._std = RollingStats(window=std_window)
        self._last_basis: float | None = None

    def update(self, ts_ms: int, basis: float) -> BasisMonitorResult:
        weight = basis_signal_weight(ts_ms, self.full_until, self.near_zero_after)

        if self._last_basis is None:
            self._last_basis = float(basis)
            ewma_v = self._ewma.update(ts_ms / 1000.0, 0.0)
            self._std.push(0.0)
            return BasisMonitorResult(0.0, ewma_v, False, 0, weight)

        d_basis = float(basis) - self._last_basis
        self._last_basis = float(basis)

        # Noise floor: tiny wiggles are not signal (§5.5).
        if abs(d_basis) < self.noise_floor_pts:
            d_eff = 0.0
        else:
            d_eff = d_basis

        ewma_v = self._ewma.update(ts_ms / 1000.0, d_eff)
        self._std.push(d_eff)

        abrupt = False
        abrupt_dir = 0
        # 2.3: the rolling std is only needed when the move clears the noise floor (most rounds it
        # is 0 by construction) -- same result, without an O(window) pass per instrument per round.
        std = self._std.std() if abs(d_eff) >= self.noise_floor_pts else 0.0
        if std > 0 and abs(d_eff) >= self.noise_floor_pts:
            if abs(d_eff - ewma_v) > self.abrupt_k * std:
                abrupt = True
                abrupt_dir = 1 if d_eff > ewma_v else -1

        return BasisMonitorResult(d_basis, ewma_v, abrupt, abrupt_dir, weight)


__all__ = ["BasisMonitor", "BasisMonitorResult"]
