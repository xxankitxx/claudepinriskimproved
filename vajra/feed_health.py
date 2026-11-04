"""VAJRA 2.0 — NIFTY feed-health / plausibility gate.

Born from the 2026-10-14/15/16 tapes: every day the Breeze CASH feed froze from
15:15 to ~15:20 (spot unchanged ~290 s while options kept ticking) and then resumed
with garbage (spot jumped +185/+240/-134 pts in one tick while the option-implied
future moved < 10 pts; |basis| up to 283). VAJRA 1.x traded and re-anchored on it.

Clean-session evidence used for the thresholds (team findings 03/04/15/17/29):
  * longest benign unchanged-spot run: 16 s       -> frozen at >= 30 s
  * clean |basis| never above ~30 pts             -> implausible above 45 pts
  * clean one-round spot jump never above ~25 pts -> jump above 40 pts
After any fault, the gate stays closed until `recovery_secs` of clean rounds.

The gate blocks NEW ENTRIES and RE-ANCHORS only. Open positions are still managed
(their exits are priced from the option quotes, which kept ticking).
Pure function of (ts, spot, basis) — deterministic in replay.
"""

from __future__ import annotations

from dataclasses import dataclass

SPOT_FROZEN = "SPOT_FROZEN"
SPOT_IMPLAUSIBLE = "SPOT_IMPLAUSIBLE"        # |spot - implied future| too large
SPOT_JUMP = "SPOT_JUMP"                      # one-round spot jump too large
RECOVERING = "RECOVERING"                    # fault cleared, waiting recovery_secs


@dataclass(frozen=True)
class HealthState:
    ok: bool
    reason: str | None          # None when ok
    spot_unchanged_secs: float
    basis: float | None
    started: bool               # True on the round a fault episode STARTS (edge)
    ended: bool                 # True on the round the gate re-opens (edge)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason,
                "spot_unchanged_secs": round(self.spot_unchanged_secs, 1),
                "basis": None if self.basis is None else round(self.basis, 2)}


class FeedHealth:
    """2.0.1 (teardown T03): (a) the jump test fires only when SPOT jumps but the option-implied
    future does NOT follow (every garbage jump moved the implied future <= 2.1 pts; a real breakout
    moves both) -- a genuine fast move no longer blocks entries; (b) a RELATIVE basis test catches
    garbage that sits just under the absolute limit (10-14: 29-45 pts, gate re-opened 3x); (c) the
    recovery wait is per fault kind: long after a frozen / implausible feed, short after a jump."""

    def __init__(self, frozen_secs=25.0, max_abs_basis=150.0, max_jump_pts=40.0,
                 recovery_secs=60.0, enabled=True, jump_divergence_pts=25.0,
                 rel_basis_pts=20.0, rel_basis_hold_secs=2.0, rel_basis_halflife_secs=300.0,
                 rel_basis_warmup_secs=120.0, jump_recovery_secs=2.0):
        self.frozen_secs = float(frozen_secs)
        self.max_abs_basis = float(max_abs_basis)
        self.max_jump_pts = float(max_jump_pts)
        self.recovery_secs = float(recovery_secs)
        self.jump_recovery_secs = float(jump_recovery_secs)
        self.jump_divergence_pts = float(jump_divergence_pts)
        self.rel_basis_pts = float(rel_basis_pts)
        self.rel_basis_hold_secs = float(rel_basis_hold_secs)
        self.rel_basis_halflife_secs = float(rel_basis_halflife_secs)
        self.rel_basis_warmup_secs = float(rel_basis_warmup_secs)
        self.enabled = bool(enabled)
        self._last_spot: float | None = None
        self._last_fut: float | None = None
        self._same_rounds = 0
        self._spot_since_ms: int | None = None
        self._fault: str | None = None          # current fault reason (incl. RECOVERING)
        self._fault_kind: str | None = None     # the last REAL fault (decides the recovery wait)
        self._clean_since_ms: int | None = None
        self._b_ewma: float | None = None       # normal basis level, learned on clean rounds only
        self._b_since_ms: int | None = None
        self._b_last_ms: int | None = None
        self._rel_off_since: int | None = None
        self.last = HealthState(True, None, 0.0, None, False, False)

    def _learn_basis(self, ts_ms, basis):
        import math
        if self._b_ewma is None:
            self._b_ewma, self._b_since_ms = basis, ts_ms
        else:
            dt = max(0.0, (ts_ms - (self._b_last_ms or ts_ms)) / 1000.0)
            a = 1.0 - math.exp(-math.log(2) * dt / max(1.0, self.rel_basis_halflife_secs))
            self._b_ewma += a * (basis - self._b_ewma)
        self._b_last_ms = ts_ms

    def update(self, ts_ms: int, spot: float, basis: float | None) -> HealthState:
        fut = (spot - basis) if basis is not None else None
        if self._last_spot is None or spot != self._last_spot:
            jump = abs(spot - self._last_spot) if self._last_spot is not None else 0.0
            fut_move = (abs(fut - self._last_fut) if (fut is not None and self._last_fut is not None)
                        else None)
            self._last_spot = spot
            self._spot_since_ms = ts_ms
            self._same_rounds = 0
        else:
            jump, fut_move = 0.0, None
            self._same_rounds += 1
        if fut is not None:
            self._last_fut = fut
        unchanged = (ts_ms - self._spot_since_ms) / 1000.0 if self._spot_since_ms is not None else 0.0

        raw = None
        if self.enabled:
            rel_bad = False
            if (basis is not None and self._b_ewma is not None and self._b_since_ms is not None
                    and (ts_ms - self._b_since_ms) / 1000.0 >= self.rel_basis_warmup_secs
                    and abs(basis - self._b_ewma) > self.rel_basis_pts):
                if self._rel_off_since is None:
                    self._rel_off_since = ts_ms
                rel_bad = (ts_ms - self._rel_off_since) / 1000.0 >= self.rel_basis_hold_secs
            else:
                self._rel_off_since = None
            # a spot print the option-implied future did not follow (clean rounds: <= 15.8 pts apart;
            # garbage one-tick prints of +-30/35 (T16) diverge by their full size)
            if fut_move is None:
                jump_bad = jump > self.max_jump_pts
            else:
                jump_bad = (abs(jump - fut_move) > self.jump_divergence_pts
                            and jump > min(self.max_jump_pts, 20.0))
            # frozen = unchanged for frozen_secs AND across >= 20 rounds (a coarse 30-s synthetic /
            # replay cadence must not read as a frozen feed; live is 4 rounds/s -> 25 s = 100 rounds)
            if unchanged >= self.frozen_secs and self._same_rounds >= 20:
                raw = SPOT_FROZEN
            elif jump_bad:                 # checked first: a single garbage print gets the SHORT recovery;
                raw = SPOT_JUMP            # if it persists, the next round (jump 0) trips IMPLAUSIBLE below
            elif basis is not None and abs(basis) > self.max_abs_basis:
                raw = SPOT_IMPLAUSIBLE
            elif rel_bad:
                raw = SPOT_IMPLAUSIBLE
            if (raw is None and self._fault is None and basis is not None
                    and self._rel_off_since is None):
                self._learn_basis(ts_ms, basis)      # learn "normal" only from clean, settled rounds

        prev = self._fault
        if raw is not None:
            # the recovery wait follows the WORST fault of the episode (a garbage episode that ends
            # with a snap-back jump must still wait the long recovery)
            if prev is None or raw != SPOT_JUMP:
                self._fault_kind = raw
            if raw == SPOT_JUMP and jump > self.max_jump_pts:
                self._fault_kind = SPOT_IMPLAUSIBLE     # a gross print (> 40 pts) earns the LONG recovery
            self._fault = raw
            self._clean_since_ms = None
        elif self._fault is not None:
            if self._clean_since_ms is None:
                self._clean_since_ms = ts_ms
            wait = self.jump_recovery_secs if self._fault_kind == SPOT_JUMP else self.recovery_secs
            if (ts_ms - self._clean_since_ms) / 1000.0 >= wait:
                self._fault = None
            else:
                self._fault = RECOVERING
        started = prev is None and self._fault is not None
        ended = prev is not None and self._fault is None
        self.last = HealthState(self._fault is None, self._fault, unchanged, basis, started, ended)
        return self.last


__all__ = ["FeedHealth", "HealthState", "SPOT_FROZEN", "SPOT_IMPLAUSIBLE", "SPOT_JUMP", "RECOVERING"]
