"""VAJRA 2.0 — backend-decided alert sounds (the cure for "ting, ting, ting").

VAJRA 1.x let the browser diff the state every 250 ms and ring a sound on every
edge. Simulated on the 10-14/15/16 tapes the sister-breach sound alone would have
rung 28 / 241 / 112 times a day (ITC / BHARTIARTL / BANKBARODA flicker), while the
NIFTY approach — the only cue that matters before a trade — rang 1 / 0 / 0 times.

2.0 policy: the ENGINE decides when a sound is due and appends it to a small ring
(`latest_state["sound_events"]`, monotonically increasing `seq`). The dashboard
only plays events with a seq it has not seen. Rules:
  * only five kinds exist: entry, exit_profit, exit_loss, approach, feed_alarm
    (sister/stock breaches never make a sound);
  * per-kind cooldown, plus a global minimum gap so two cues never overlap;
  * trade cues (entry / exits) bypass the global gap and per-kind cooldown — they
    are rare and must never be dropped;
  * a kind can be switched off in config (`sounds.<kind>: off`).
Deterministic (tape-timestamp driven).
"""

from __future__ import annotations

from collections import deque

KINDS = ("entry", "exit_profit", "exit_loss", "approach", "feed_alarm", "armed")
TRADE_KINDS = ("entry", "exit_profit", "exit_loss")
# 2.0.2 (sound council): priority used by the page's player -- a higher cue cuts a lower one short
PRIORITY = {"exit_loss": 5, "exit_profit": 5, "entry": 4, "feed_alarm": 3, "approach": 2, "armed": 1}

DEFAULT_COOLDOWN_SECS = {"entry": 0, "exit_profit": 0, "exit_loss": 0,
                         "approach": 120, "feed_alarm": 300, "armed": 0}


class SoundBus:
    def __init__(self, cfg_sounds: dict | None = None, ring: int = 30):
        c = dict(cfg_sounds or {})
        self.cfg = c
        self.enabled = {k: str(c.get(k, "on")).lower() not in ("off", "0", "no", "false") for k in KINDS}
        self.cooldown_ms = {k: int(float(c.get(f"{k}_cooldown_secs", DEFAULT_COOLDOWN_SECS[k])) * 1000)
                            for k in KINDS}
        self.global_gap_ms = int(float(c.get("global_min_gap_secs", 3)) * 1000)
        self._last_kind_ms: dict[str, int] = {}
        self.sink = None                               # tape.event-compatible callable
        self._last_any_ms: int | None = None
        self._seq = 0
        self.events: deque = deque(maxlen=ring)
        self.suppressed = 0

    def reset(self, kind: str) -> None:
        """Forget the last time `kind` rang (2.0.2: the approach cooldown restarts on every entry, exit
        and level change -- a warning for a NEW setup must never be muted by the previous one)."""
        self._last_kind_ms.pop(kind, None)

    def emit(self, kind: str, ts_ms: int, text: str = "", force: bool = False) -> bool:
        """Request a sound. Returns True if it was queued for the dashboard. `force` bypasses the
        per-kind cooldown and the global gap (entry-imminent warning, critical alarms)."""
        if kind not in KINDS or not self.enabled.get(kind, False):
            return False
        trade = kind in TRADE_KINDS
        if not trade and not force:
            last_k = self._last_kind_ms.get(kind)
            if last_k is not None and ts_ms - last_k < self.cooldown_ms[kind]:
                self.suppressed += 1
                return False
            if (kind != "feed_alarm" and self._last_any_ms is not None
                    and ts_ms - self._last_any_ms < self.global_gap_ms):   # an alarm is never lost to the gap
                self.suppressed += 1
                return False
        self._seq += 1
        if trade:                                      # a trade starts a new setup: the next warning must
            self._last_kind_ms.pop("approach", None)   # not be muted by the previous one (council 01/08)
        self._last_kind_ms[kind] = ts_ms
        if kind != "armed":                            # the daily sound check never blocks a real cue
            self._last_any_ms = ts_ms
        self.events.append({"seq": self._seq, "ts": int(ts_ms), "kind": kind, "text": text,
                            "prio": PRIORITY.get(kind, 1)})
        if self.sink is not None:                      # 2.0.1: sounds are taped (EOD: engine vs browser)
            try:
                self.sink("SOUND", {"seq": self._seq, "kind": kind, "text": text}, ts_ms=int(ts_ms))
            except Exception:
                pass
        return True

    def snapshot(self) -> list[dict]:
        return list(self.events)


class ApproachEpisode:
    """Hysteresis for the NIFTY approach cue: arm when spot comes within `prox` of a level,
    re-arm only after it has backed away beyond `prox + rearm_pts` (or the level moved)."""

    def __init__(self, prox: float, rearm_pts: float = 8.0):
        self.prox = float(prox)
        self.rearm_pts = float(rearm_pts)
        self._armed = True
        self._level = None

    def update(self, dist: float | None, level: float | None) -> bool:
        """Return True on the round a NEW approach episode starts."""
        if dist is None or level is None:
            return False
        if self._level is not None and abs(level - self._level) > 1e-6:
            self._armed = True                       # a re-anchored level is a fresh episode
        self._level = level
        if self._armed and dist <= self.prox:
            self._armed = False
            return True
        if not self._armed and dist > self.prox + self.rearm_pts:
            self._armed = True
        return False

    def rearm(self) -> None:
        """2.0.2: the episode started but its cue could not ring (cooldown, gate, gap) -- keep it
        armed so the warning rings as soon as it can, instead of being used up silently."""
        self._armed = True


__all__ = ["SoundBus", "ApproachEpisode", "KINDS", "PRIORITY"]
