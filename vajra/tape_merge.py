"""Merge a day's leftover tape parts in a process of its own (2.2).

    python -m vajra.tape_merge tapes/2026-11-09 [more partitions ...]

Why: on 9 Nov 2026 the end-of-day merge ran inside the engine's console window; it takes minutes, the
window was closed half way and the day was left as 1,538 part files (the next start only repaired
*today's* folder). The engine now closes its parts, starts this module detached and exits at once;
the next live start also sweeps the recent day folders for parts a shutdown interrupted.

Two files in the day folder keep this process and a running engine apart (both live in vajra.tape):
  * ``.live``        written and refreshed by a TapeWriter while it has the folder open. A fresh marker
                     means "an engine is writing here": nothing is merged (a same-day restart while the
                     earlier session's merge is still running stops that merge at the next tape).
  * ``.merge.lock``  held by the one process that is merging; a TapeWriter that starts while it is held
                     leaves the earlier session's parts to that process instead of merging them itself.
Only parts that existed BEFORE the live-marker check are merged, so a file an engine is still writing
can never be picked up. Parts are deleted only after the merged file is complete (``merge_parts``
writes to a temp name and renames), so killing this process loses nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .tape import (LOCK_NAME, STALE_LOCK_SECS, live_marker_fresh, merge_lock_held)   # noqa: F401  (re-exported)

LAST_ERROR = [None]        # why the last merge_partition() left parts behind (for the log)
WAIT_SECS = 900            # how long main() waits for a live engine / another merge to let go of a folder
RETRY_SECS = 15


def has_parts(partition: Path) -> bool:
    try:
        return any(Path(partition).glob("*.part-*.parquet"))
    except OSError:
        return False


def needs_work(partition: Path) -> bool:
    """Parts to merge, or a closing session's sidecar manifest still to apply."""
    return has_parts(partition) or (Path(partition) / ".sidecars.json").exists()


def _lower_priority() -> None:
    try:
        if os.name == "nt":
            from .power import _set_priority_class
            _set_priority_class(0x00004000)                               # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


def merge_partition(partition: Path) -> list[str]:
    """Merge every tape's parts in ``partition`` under the folder lock. Returns the tapes merged
    ([] when there was nothing to do, an engine is writing there, or another merge holds the lock)."""
    from . import schema as S
    from .tape import merge_parts, part_paths
    partition = Path(partition)
    if not partition.is_dir():
        return []
    if not has_parts(partition):
        if (partition / ".sidecars.json").exists() and not live_marker_fresh(partition):
            from .tape import drop_manifest_sidecars          # a merge that finished before the manifest
            return [f"dropped {n}.jsonl" for n in drop_manifest_sidecars(partition)]
        return []
    names = list(S.PARQUET_TAPES) + ["raw_ticks"]
    snapshot = {n: part_paths(partition, n) for n in names}       # BEFORE the marker check (see module doc)
    if live_marker_fresh(partition):
        return []
    lock = partition / LOCK_NAME
    try:
        if lock.exists() and not merge_lock_held(partition):
            lock.unlink()                                          # a merge that died long ago
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as e:                                           # held by another merge (or unwritable folder)
        LAST_ERROR[0] = f"lock: {e!r}"[:200]
        return []
    done = []
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        for name in names:
            if not snapshot[name]:
                continue
            if live_marker_fresh(partition):                       # an engine (re)started here: stop, it owns the folder
                break
            try:
                if merge_parts(partition, name, S.TAPE_SCHEMAS.get(name), parts=snapshot[name]) is not None:
                    done.append(name)
            except Exception as e:                                 # that tape's parts stay; the others still merge
                LAST_ERROR[0] = f"{name}: {e!r}"[:200]
            try:
                os.utime(lock, None)                               # still alive: keep the lock fresh
            except OSError:
                pass
        if not has_parts(partition):                               # every tape is whole: the closing session's
            from .tape import drop_manifest_sidecars               # verified jsonl copies can go
            dropped = drop_manifest_sidecars(partition)
            if dropped:
                done = done + [f"dropped {n}.jsonl" for n in dropped]
        return done
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def spawn(partitions, root: Path | None = None, low_priority: bool = True) -> bool:
    """Start the merge of ``partitions`` in a detached, low-priority process that outlives this one
    (and the console window). Returns False when nothing needed merging or the spawn failed -- the
    parts then simply wait for the next start."""
    parts = [str(Path(p)) for p in partitions if needs_work(Path(p))]
    if not parts:
        return False
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    # low_priority (the sweep at a live START) keeps the merge out of the engine's way. At the CLOSE it
    # must not be: on 13 Nov a below-normal merge on a laptop at 100% CPU did not even start for 12 min.
    cmd = [sys.executable, "-m", "vajra.tape_merge", *(["--low"] if low_priority else []), *parts]
    log = None
    try:
        (root / "logs").mkdir(exist_ok=True)
        log = open(root / "logs" / "tape_merge.log", "ab")
    except OSError:
        log = None
    out = log if log is not None else subprocess.DEVNULL
    kw = dict(cwd=str(root), stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True)
    try:
        if os.name == "nt":
            base = 0x08000000 | 0x00000200 | (0x00004000 if low_priority else 0)   # NO_WINDOW | NEW_PROCESS_GROUP [| BELOW_NORMAL]
            try:
                subprocess.Popen(cmd, creationflags=base | 0x01000000, **kw)   # + BREAKAWAY_FROM_JOB
            except OSError:
                subprocess.Popen(cmd, creationflags=base, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
        return True
    except Exception:
        return False
    finally:
        if log is not None:                                  # the child has its own handle
            try:
                log.close()
            except OSError:
                pass


def stale_partitions(tape_root: Path, exclude: Path | None = None, look_back: int = 8) -> list[Path]:
    """Recent day folders (other than ``exclude``) that still hold part files."""
    try:
        days = sorted(d for d in Path(tape_root).iterdir()
                      if d.is_dir() and len(d.name) == 10 and d.name[4] == "-")
    except OSError:
        return []
    ex = Path(exclude).resolve() if exclude is not None else None
    return [d for d in days[-look_back:] if d.resolve() != ex and needs_work(d)]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    low = "--low" in argv
    argv = [x for x in argv if x != "--low"]
    if not argv:
        print(__doc__)
        return 2
    if low:
        _lower_priority()
    for a in argv:
        part = Path(a)
        t0 = time.perf_counter()
        try:
            done = merge_partition(part)
            # 13 Nov: on a laptop at 100% CPU with 2 GB free a first attempt merged nothing and gave up
            # silently. Whatever the reason (an engine live there, another merge, memory, a file the
            # virus scanner holds), keep trying for WAIT_SECS and say why in the log.
            while has_parts(part) and time.perf_counter() - t0 < WAIT_SECS:
                time.sleep(RETRY_SECS)
                done = done + merge_partition(part)
            left = ""
            if has_parts(part):
                why = ("an engine is writing there" if live_marker_fresh(part) else
                       "another merge holds the lock" if merge_lock_held(part) else
                       (LAST_ERROR[0] or "unknown reason"))
                left = f" (PARTS REMAIN: {why}; they merge at the next start)"
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {a}: merged {done or 'nothing'}{left} "
                  f"({time.perf_counter() - t0:.1f} s)", flush=True)
        except Exception as e:                           # never die silently: the log is the only witness
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {a}: merge FAILED {e!r} (parts kept)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
