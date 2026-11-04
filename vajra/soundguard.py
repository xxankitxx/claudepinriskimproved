"""VAJRA 2.0.2 — engine-side sound WATCH (sound council 03/07/10). It never plays a sound.

Only the dashboard plays sounds, and only real MP3 files (operator's rule, 20 Oct 2026). But a locked,
closed or crashed page looks exactly like a quiet market. The page reports its sound health in its
beacon (POST /api/client every 10 s, and at once when it locks/unlocks or cannot play a cue). This
watch prints one loud console line when no dashboard has been able to play sound for ``stale_secs``,
and lists any trade cue that was missed, so it is visible in the console and in the EOD tapes.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

TRADE = ("entry", "exit_profit", "exit_loss", "feed_alarm")


class SoundGuard:
    def __init__(self, sounds_dir: str = "", enabled: bool = True, stale_secs: float = 30.0,
                 log: Callable[[str], None] = print, **_ignored):
        self.enabled = bool(enabled)
        self.stale_ms = int(stale_secs * 1000)
        self.log = log
        self.last_beacon_ms: int | None = None
        self.last_ok = False
        self.last_reason = "no dashboard has reported yet"
        self._cursor: int | None = None
        self._boot = None
        self._bad_since: int | None = None
        self._warned = False
        self.missed: list[tuple[int, str]] = []

    def beacon(self, body: dict, now_ms: int | None = None) -> None:
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        snd = body.get("snd") if isinstance(body, dict) else None
        if not isinstance(snd, dict) or not snd.get("leader", True):
            return
        muted = bool(snd.get("muted"))
        playing = bool(snd.get("on")) and not snd.get("locked")
        self.last_beacon_ms = now_ms
        self.last_ok = muted or playing
        self.last_reason = ("sound turned OFF by operator" if muted else
                            ("ok" if playing else "dashboard sound LOCKED - click the page"))

    def page_ok(self, now_ms: int) -> bool:
        return (self.last_beacon_ms is not None and self.last_ok
                and now_ms - self.last_beacon_ms <= self.stale_ms)

    def tick(self, events: list[dict], boot_id, now_ms: int | None = None) -> list[str]:
        """Returns the trade cues that were due while no page could play them (and logs them)."""
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        if not self.enabled:
            return []
        if boot_id != self._boot:
            self._boot, self._cursor = boot_id, None
        max_seq = events[-1]["seq"] if events else 0
        if self._cursor is None:
            self._cursor = max_seq
            return []
        ok = self.page_ok(now_ms)
        if ok:
            self._bad_since, self._warned = None, False
        elif self._bad_since is None:
            self._bad_since = now_ms
        out = []
        if not ok:
            for e in events:
                if e["seq"] > self._cursor and e["kind"] in TRADE:
                    out.append(e["kind"])
                    self.missed.append((e["ts"], e["kind"]))
                    self.log(f"  !! SOUND NOT PLAYED: {e['kind'].upper()} {e.get('text', '')} -- {self.last_reason}")
            if not self._warned and now_ms - self._bad_since >= self.stale_ms:
                self._warned = True
                self.log(f"  !! NO DASHBOARD IS PLAYING SOUND ({self.last_reason}) -- open/click the VAJRA window")
        self._cursor = max(self._cursor, max_seq)
        return out

    def played_seqs(self) -> list[int]:     # kept for the page contract: nothing is ever played here
        return []


def run_guard(orch, guard: SoundGuard, period: float = 0.5) -> threading.Thread:
    def loop():
        while True:
            try:
                st = orch.latest_state or {}
                guard.tick(st.get("sound_events") or [], st.get("boot_id"))
            except Exception:
                pass
            time.sleep(period)
    t = threading.Thread(target=loop, name="vajra-soundwatch", daemon=True)
    t.start()
    return t


__all__ = ["SoundGuard", "run_guard"]
