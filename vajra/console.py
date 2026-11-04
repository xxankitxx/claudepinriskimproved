"""Non-blocking console output for the engine thread (2.1).

A Windows console write can block the calling thread: QuickEdit text selection, a resized or
frozen terminal, a slow redirect. The engine thread used to print the heartbeat and feed warnings
directly, so a blocked console froze the round loop (no stops, no square-off). Every operator-facing
line now goes through a bounded queue drained by a daemon thread; the engine never waits on stdout.
"""

from __future__ import annotations

import queue
import sys
import threading

_q: "queue.Queue[tuple[str, bool]]" = queue.Queue(maxsize=4000)
_started = False
_lock = threading.Lock()
dropped = 0


def _drain() -> None:
    while True:
        msg, err = _q.get()
        stream = sys.stderr if err else sys.stdout
        try:
            print(msg, file=stream, flush=True)
        except UnicodeEncodeError:
            try:
                print(msg.encode("ascii", "replace").decode("ascii"), file=stream, flush=True)
            except Exception:
                pass
        except Exception:
            pass


def _ensure() -> None:
    global _started
    if _started:
        return
    with _lock:
        if not _started:
            threading.Thread(target=_drain, name="vajra-console", daemon=True).start()
            _started = True


def say(msg: str, err: bool = False) -> None:
    """Queue one line for the console. Never blocks; drops (and counts) if the queue is full."""
    global dropped
    _ensure()
    try:
        _q.put_nowait((str(msg), bool(err)))
    except queue.Full:
        dropped += 1


def warn(msg: str) -> None:
    say(msg, err=True)


__all__ = ["say", "warn"]
