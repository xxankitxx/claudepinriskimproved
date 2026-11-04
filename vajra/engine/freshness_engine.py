"""§5.4 Freshness / Recalc Engine — FIXED-INTERVAL pin-risk levels (operator rule).

  * Pre-open: levels are SEEDED from the previous day's close (done in the orchestrator).
  * First intraday recompute at 09:20 (the first round past calc_start).
  * Then re-anchor every `hold_min_secs` — held in between, never refreshed tick-by-tick. The
    interval is configurable per instrument (NIFTY 20 min, sisters/stocks 3 min — see orchestrator).
  * Each EXIT forces its own recompute (handled in the orchestrator).

This supersedes the earlier consolidation-anchored model. Pure function of (ts), so replay
reproduces it exactly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FreshnessDecision:
    need_recompute: bool
    recalc_trigger: str | None        # "open" | "interval" | None
    freshness_age_secs: float         # age of the levels currently held


class FreshnessEngine:
    def __init__(self, hold_min_secs: float = 1800.0, retry_secs: float = 30.0, **_ignored):
        # _ignored absorbs legacy flat_window_secs/flat_range_pct/bootstrap_secs (the model is
        # now a fixed interval, not consolidation-anchored — operator rule).
        self.hold_min_secs = float(hold_min_secs)
        self.retry_secs = float(retry_secs)        # while a re-anchor keeps FAILING, retry on this short cadence
        # Two clocks (kept separate so a FAILED re-anchor cannot corrupt the timer):
        #  * _last_success_ms drives the held-levels AGE (only a real recompute resets it).
        #  * _last_attempt_ms drives the re-TRIGGER (a failed attempt resets it too, so we don't
        #    re-fire every single round — the old bug: age grew unbounded -> need_recompute every
        #    round -> phantom recalcs + multi-day "ages").
        self._last_success_ms: int | None = None
        self._last_attempt_ms: int | None = None

    def mark_recomputed(self, ts_ms: int) -> None:
        """A SUCCESSFUL recompute — resets both the age clock and the re-trigger clock."""
        self._last_success_ms = int(ts_ms)
        self._last_attempt_ms = int(ts_ms)

    def mark_attempt(self, ts_ms: int) -> None:
        """A recompute was TRIED but the band failed (feed gap) — record the attempt so we retry
        on the short `retry_secs` cadence instead of hammering every round, without claiming the
        held levels were refreshed (the age keeps reflecting the last real success)."""
        self._last_attempt_ms = int(ts_ms)

    def age_secs(self, ts_ms: int) -> float:
        if self._last_success_ms is None:
            return 0.0
        return max(0.0, (ts_ms - self._last_success_ms) / 1000.0)

    def update(self, ts_ms: int, spot: float | None = None) -> FreshnessDecision:
        """Fixed-interval recompute (operator rule): compute at the first intraday round
        (09:20), then re-anchor every `hold_min_secs` (30 min). A prev-close SEED set before
        the open counts as the last compute, so the first intraday round re-anchors at 09:20.
        Exits force their own recompute outside this engine."""
        age = self.age_secs(ts_ms)
        if self._last_attempt_ms is None:
            return FreshnessDecision(True, "open", 0.0)          # first intraday calc (09:20)
        since_attempt = max(0.0, (ts_ms - self._last_attempt_ms) / 1000.0)
        # If the last anchor never SUCCEEDED (held levels are stale), retry on the short cadence;
        # otherwise the normal fixed 30-min interval. Gating on the ATTEMPT clock — not the age —
        # is what stops a persistently-failing re-anchor from re-firing every round.
        holding_stale = (self._last_success_ms is None
                         or self._last_success_ms < self._last_attempt_ms)
        threshold = self.retry_secs if holding_stale else self.hold_min_secs
        if since_attempt >= threshold:
            return FreshnessDecision(True, "interval", age)
        return FreshnessDecision(False, None, age)


__all__ = ["FreshnessEngine", "FreshnessDecision"]
