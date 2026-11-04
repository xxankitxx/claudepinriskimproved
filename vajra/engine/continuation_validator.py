"""§5.7 Breach + Continuation Validator — the touch is not the signal.

  * BREACHED: spot closes beyond UPPER/LOWER by ``break_buffer`` (default 0.03%
    of spot) on the working timeframe.
  * CONTINUATION CONFIRMED: after breach, price extends in-direction for
    ``confirm_secs`` (default 120s) OR by ``confirm_points`` (default 0.1% of
    spot), WITH volume >= ``confirm_vol_mult`` * recent average (default 1.2x).
    On a slow-grind day, confirmation is persistence not speed — tune
    confirm_secs up rather than requiring velocity.
  * FALSE_BREAK: a breach that snaps back inside the band before confirmation
    -> no signal, logged FALSE_BREAK.

This stateful, per-instrument machine also emits the directional state vector
(state / dir / strength) that the §5.6 decision-maker consumes for every
instrument in the universe.
"""

from __future__ import annotations

from dataclasses import dataclass

# State alphabet (spec §5.6).
NEUTRAL = "NEUTRAL"
APPROACH_UP = "APPROACH_UP"
APPROACH_DOWN = "APPROACH_DOWN"
BREACH_UP = "BREACH_UP"
BREACH_DOWN = "BREACH_DOWN"
CONFIRM_UP = "CONFIRM_UP"
CONFIRM_DOWN = "CONFIRM_DOWN"


@dataclass(frozen=True)
class ContinuationSignal:
    state: str
    dir: int                       # +1 / 0 / -1
    strength: float                # [0,1]
    breached: bool
    confirmed: bool
    false_break: bool              # True only on the tick a false break is detected
    confirm_secs_elapsed: float
    level_crossed: float | None    # the level value spot crossed (for signal_tape)
    extension: float               # max in-direction extension beyond the level


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


class ContinuationValidator:
    def __init__(
        self,
        break_buffer_pct: float = 0.03,
        confirm_secs: float = 120.0,
        confirm_points_pct: float = 0.10,
        confirm_vol_mult: float = 1.2,
        linger_band_pct: float = 0.05,
    ):
        self.break_buffer_pct = float(break_buffer_pct)
        self.confirm_secs = float(confirm_secs)
        self.confirm_points_pct = float(confirm_points_pct)
        self.confirm_vol_mult = float(confirm_vol_mult)
        self.linger_band_pct = float(linger_band_pct)

        # active breach episode
        self._dir = 0
        self._breach_ts: int | None = None
        self._breach_level: float | None = None
        self._max_ext = 0.0
        self._confirmed = False
        self._vol_latch = False        # "the break had volume behind it" (latched per episode)

    def _reset_episode(self) -> None:
        self._dir = 0
        self._breach_ts = None
        self._breach_level = None
        self._max_ext = 0.0
        self._confirmed = False
        self._vol_latch = False

    def update(
        self,
        ts_ms: int,
        spot: float,
        upper_level: float | None,
        lower_level: float | None,
        vol_ratio: float = 1.0,
    ) -> ContinuationSignal:
        break_buf = (self.break_buffer_pct / 100.0) * spot
        confirm_pts = (self.confirm_points_pct / 100.0) * spot
        linger_pts = (self.linger_band_pct / 100.0) * spot

        if upper_level is None or lower_level is None:
            # Band invalid (feed gap) — hold no episode, report NEUTRAL.
            false_break = self._breach_ts is not None and not self._confirmed
            self._reset_episode()
            return ContinuationSignal(NEUTRAL, 0, 0.0, False, False, false_break, 0.0, None, 0.0)

        breached_up = spot >= upper_level + break_buf
        breached_down = spot <= lower_level - break_buf
        cur_dir = 1 if breached_up else (-1 if breached_down else 0)

        false_break = False

        if cur_dir == 0:
            # Not beyond either level by the buffer.
            if self._breach_ts is not None and not self._confirmed:
                false_break = True            # snapped back before confirmation
            self._reset_episode()

            # Approach detection (within linger band of a level, not breached).
            near_up = (upper_level - spot) >= 0 and (upper_level - spot) <= linger_pts
            near_dn = (spot - lower_level) >= 0 and (spot - lower_level) <= linger_pts
            if near_up and (not near_dn or (upper_level - spot) <= (spot - lower_level)):
                close = 1.0 - _clamp((upper_level - spot) / max(linger_pts, 1e-9), 0.0, 1.0)
                return ContinuationSignal(APPROACH_UP, 1, 0.10 + 0.20 * close,
                                          False, False, false_break, 0.0, upper_level, 0.0)
            if near_dn:
                close = 1.0 - _clamp((spot - lower_level) / max(linger_pts, 1e-9), 0.0, 1.0)
                return ContinuationSignal(APPROACH_DOWN, -1, 0.10 + 0.20 * close,
                                          False, False, false_break, 0.0, lower_level, 0.0)
            return ContinuationSignal(NEUTRAL, 0, 0.0, False, False, false_break, 0.0, None, 0.0)

        # We are beyond a level by the buffer -> in a breach episode.
        level = upper_level if cur_dir == 1 else lower_level
        ext = (spot - upper_level) if cur_dir == 1 else (lower_level - spot)

        if self._dir != cur_dir or self._breach_ts is None:
            # New episode (or direction flip). Flag prior unconfirmed as false break.
            if self._breach_ts is not None and not self._confirmed and self._dir != cur_dir:
                false_break = True
            self._dir = cur_dir
            self._breach_ts = int(ts_ms)
            self._breach_level = level
            self._max_ext = ext
            self._confirmed = False
            self._vol_latch = False
        else:
            self._max_ext = max(self._max_ext, ext)

        # Volume condition is about the break HAVING volume behind it; latch it
        # for the episode so a later normalised tick can't un-confirm a real break
        # (continuation itself is then about persistence, not speed — §5.7).
        if vol_ratio >= self.confirm_vol_mult:
            self._vol_latch = True

        elapsed = (ts_ms - self._breach_ts) / 1000.0
        persistence_ok = (elapsed >= self.confirm_secs) or (self._max_ext >= confirm_pts)

        if persistence_ok and self._vol_latch:
            self._confirmed = True

        r_ext = _clamp(self._max_ext / max(confirm_pts, 1e-9), 0.0, 1.0)
        r_time = _clamp(elapsed / max(self.confirm_secs, 1e-9), 0.0, 1.0)

        if self._confirmed:
            strength = _clamp(0.80 + 0.20 * max(r_ext, r_time), 0.80, 1.0)
            state = CONFIRM_UP if cur_dir == 1 else CONFIRM_DOWN
            return ContinuationSignal(state, cur_dir, strength, True, True, false_break,
                                      elapsed, level, self._max_ext)

        strength = _clamp(0.45 + 0.30 * max(r_ext, r_time), 0.45, 0.78)
        state = BREACH_UP if cur_dir == 1 else BREACH_DOWN
        return ContinuationSignal(state, cur_dir, strength, True, False, false_break,
                                  elapsed, level, self._max_ext)


__all__ = [
    "ContinuationValidator", "ContinuationSignal",
    "NEUTRAL", "APPROACH_UP", "APPROACH_DOWN", "BREACH_UP", "BREACH_DOWN",
    "CONFIRM_UP", "CONFIRM_DOWN",
]
