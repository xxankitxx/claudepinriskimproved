"""The tape (spec §9) — append-only, ms-timestamped, partitioned by trade date.

Design (2.1):
  * The columnar parquet is written as a sequence of small, self-contained PART files
    (``<tape>.part-00001.parquet`` ...), each closed (footer written) every ``rotate_secs``.
    A hard kill therefore loses at most ``rotate_secs`` of rows -- 2.0 kept ONE ParquetWriter
    open all day, so every tape of a killed session was unreadable (no footer; 2 Nov 2026).
  * At a clean close the parts are merged into the canonical ``<tape>.parquet``; leftover parts
    from a crashed session are merged the next time the partition is opened (writer OR reader).
  * The small tapes (decision / order / signal) keep a crash-safe ``.jsonl`` sidecar flushed once
    per round. The big ones (tick / level / basis, ~9 GB/day of jsonl in 2.0) rely on the parts
    (``tape.sidecar_jsonl: all`` restores the 2.0 behaviour).
  * event_tape (§9.7) is jsonl-only. Every row carries ``schema_version`` and an int64 ms ``ts``.

Layout:  <root>/<YYYY-MM-DD>/<tape_name>.parquet  (+ .jsonl sidecar for the small tapes)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from . import SCHEMA_VERSION
from . import schema as S


def now_ms() -> int:
    return int(time.time_ns() // 1_000_000)


SMALL_TAPES = ("decision_tape", "signal_tape", "order_tape")
BIG_TAPES = ("tick_tape", "level_tape", "basis_tape")


# =============================================================================
# Part-file helpers (shared by the writer, the raw-tick recorder and every reader)
# =============================================================================
def part_paths(partition: Path, name: str) -> list[Path]:
    return sorted(partition.glob(f"{name}.part-*.parquet"))


def _readable(p: Path) -> bool:
    """True when the file has a valid footer. The handle is closed at once (Windows cannot
    rename / delete a file somebody still holds open)."""
    try:
        with pq.ParquetFile(str(p)):
            return True
    except Exception:
        return False


# 2.1.1: parts a LIVE writer currently has open (never merged, never renamed .corrupt by a reader that
# runs while the session is on), and one lock per (partition, tape) so two threads cannot merge the
# same tape at once (startup repair thread vs. a reader on the main thread -> duplicated rows).
OPEN_PARTS: set = set()
_MERGE_LOCKS: dict = {}
_MERGE_LOCKS_GUARD = threading.Lock()


# 2.2: two small files in a day folder keep a detached merge (vajra.tape_merge) and an engine apart.
LIVE_NAME = ".live"            # a TapeWriter has this folder open (refreshed while it runs)
LOCK_NAME = ".merge.lock"      # one process is merging this folder (holds its pid)
LIVE_FRESH_SECS = 300          # a marker older than this belongs to an engine that died
LIVE_TOUCH_SECS = 30
STALE_LOCK_SECS = 2700         # a merge refreshes its lock after every tape; older = it died


def _age_secs(path: Path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def live_marker_fresh(partition: Path) -> bool:
    """True while an engine is (or was, within LIVE_FRESH_SECS) writing parts in ``partition``."""
    age = _age_secs(Path(partition) / LIVE_NAME)
    return age is not None and age < LIVE_FRESH_SECS


def merge_lock_held(partition: Path) -> bool:
    age = _age_secs(Path(partition) / LOCK_NAME)
    return age is not None and age < STALE_LOCK_SECS


def _foreign_merge(partition: Path) -> bool:
    """Another PROCESS is merging this folder right now (its fresh lock holds a pid that is not ours)."""
    if not merge_lock_held(partition):
        return False
    try:
        return (Path(partition) / LOCK_NAME).read_text().strip() != str(os.getpid())
    except OSError:
        return True


def _merge_lock(partition: Path, name: str) -> threading.Lock:
    key = (str(Path(partition).resolve()), name)
    with _MERGE_LOCKS_GUARD:
        lk = _MERGE_LOCKS.get(key)
        if lk is None:
            lk = _MERGE_LOCKS[key] = threading.Lock()
        return lk


def merge_parts(partition: Path, name: str, schema: pa.Schema | None = None,
                compression: str = "zstd", parts: list | None = None) -> Path | None:
    """Merge ``<name>.part-*.parquet`` into ``<name>.parquet`` (one read per part; Arrow releases the
    GIL while it works). An existing ``<name>.parquet`` (an EARLIER session of the same day) is kept
    as ``.bak``. Written to a temp name and renamed, so a kill during the merge leaves the parts intact.
    ``parts`` restricts the merge to an explicit list (the startup repair snapshots the leftover parts
    so it never swallows the new session's files). Returns the merged path, or None if nothing to do."""
    partition = Path(partition)
    if _foreign_merge(partition):                       # 2.2: a detached merge owns these parts -- merging
        return None                                     # them here too would write every row twice
    with _merge_lock(partition, name):
        cands = list(parts) if parts is not None else part_paths(partition, name)
        cands = [p for p in cands if p.exists() and str(p.resolve()) not in OPEN_PARTS]
        if not cands:
            return None
        readable = []
        for p in cands:
            if not _readable(p):                        # a part cut off mid-write (a killed session)
                try:
                    p.rename(p.with_name(p.name + ".corrupt"))
                except OSError:
                    pass
                continue
            readable.append(p)
        if not readable:
            return None
        main = partition / f"{name}.parquet"
        if main.exists():
            _backup(main)
        tmp = partition / f"{name}.parquet.merging"
        if tmp.exists():
            tmp.unlink()
        if schema is None:
            with pq.ParquetFile(str(readable[0])) as pf0:
                schema = pf0.schema_arrow
        w = pq.ParquetWriter(str(tmp), schema, compression=compression)
        try:
            for p in readable:
                with pq.ParquetFile(str(p)) as pf:
                    tbl = pf.read()
                if not tbl.schema.equals(schema):
                    tbl = tbl.cast(schema)
                if tbl.num_rows:
                    w.write_table(tbl, row_group_size=max(4096, tbl.num_rows))
        finally:
            w.close()
        if main.exists():                               # (a backup rename failed) -> never clobber silently
            _backup(main)
        tmp.rename(main)
        for p in readable:
            try:
                p.unlink()
            except OSError:
                pass
        return main


def _backup(path: Path) -> Path:
    """Rename a prior-session file out of the way, collision-safe (Windows rename never overwrites)."""
    stamp = now_ms()
    tag = "" if _readable(path) else ".unreadable"
    bak = path.with_name(f"{path.name}.{stamp}{tag}.bak")
    n = 1
    while bak.exists():
        bak = path.with_name(f"{path.name}.{stamp}.{n}{tag}.bak")
        n += 1
    path.rename(bak)
    return bak


def recover_partition(partition: Path, names=None) -> list[str]:
    """Merge any leftover parts (a crashed / killed session) into the canonical files.
    Cheap when there is nothing to do. Returns the tapes that were recovered."""
    partition = Path(partition)
    out = []
    for name in list(names or S.PARQUET_TAPES) + ["raw_ticks"]:
        try:
            if merge_parts(partition, name, S.TAPE_SCHEMAS.get(name)) is not None:
                out.append(name)
        except Exception:
            continue
    return out


def _first_ts(path: Path) -> int:
    try:
        with pq.ParquetFile(str(path)) as pf:
            for gi in range(pf.num_row_groups):
                col = pf.read_row_group(gi, columns=["ts"]).column("ts")
                if len(col):
                    return int(col[0].as_py())
    except Exception:
        pass
    return 0


def parquet_sources(partition: Path, name: str) -> list[Path]:
    """Every READABLE parquet file holding rows of ``name`` for the day, in time order: a clean day
    is just ``<name>.parquet``; a restarted day adds the earlier session's ``.bak``; leftover parts
    are merged first. An unreadable (footer-less, pre-2.1) file is skipped."""
    partition = Path(partition)
    try:
        recover_partition(partition, [name])
    except Exception:
        pass
    cands = [partition / f"{name}.parquet"] + sorted(partition.glob(f"{name}.parquet.*.bak"))
    found = []
    for p in cands:
        if not p.exists() or p.name.endswith(".unreadable.bak"):
            continue
        if not _readable(p):
            continue
        found.append((_first_ts(p), p))
    found.sort(key=lambda x: x[0])
    return [p for _t, p in found]


def iter_parquet_rows(partition: Path, name: str, batch_size: int = 20000, columns=None):
    """Stream the day's rows of a tape in time order across every readable file."""
    for p in parquet_sources(partition, name):
        with pq.ParquetFile(str(p)) as pf:
            for batch in pf.iter_batches(batch_size=batch_size, columns=columns):
                yield from batch.to_pylist()


def rebuild_from_jsonl(partition: Path, name: str, compression: str = "zstd",
                       batch_rows: int = 5000) -> int:
    """2.0 left footer-less parquet files beside complete jsonl sidecars. Rebuild ``<name>.parquet``
    from ``<name>.jsonl`` (streaming). Returns the number of rows written (0 if nothing to do)."""
    partition = Path(partition)
    j = partition / f"{name}.jsonl"
    p = partition / f"{name}.parquet"
    schema = S.TAPE_SCHEMAS.get(name)
    if not j.exists() or schema is None:
        return 0
    if p.exists():
        if _readable(p):
            return 0                                    # already fine
        _backup(p)
    tmp = partition / f"{name}.parquet.rebuilding"
    w = pq.ParquetWriter(str(tmp), schema, compression=compression)
    n = 0
    buf: list[dict] = []
    try:
        with open(j, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    buf.append(json.loads(line))
                except ValueError:
                    continue                            # a torn last line
                if len(buf) >= batch_rows:
                    w.write_table(pa.Table.from_pylist(buf, schema=schema)); n += len(buf); buf = []
        if buf:
            w.write_table(pa.Table.from_pylist(buf, schema=schema)); n += len(buf)
    finally:
        w.close()
    tmp.rename(p)
    return n


SIDECAR_MANIFEST = ".sidecars.json"   # 2.2: {tape: rows} a closing session hands to the detached merge


def drop_sidecars(partition: Path, todo: dict) -> list[str]:
    """Delete ``<tape>.jsonl`` for each {tape: rows} whose merged parquet reads back exactly ``rows``
    rows and has no earlier-session .bak beside it. Anything that does not check out is kept."""
    removed = []
    partition = Path(partition)
    for name, n in (todo or {}).items():
        j = partition / f"{name}.jsonl"
        p = partition / f"{name}.parquet"
        try:
            if not (isinstance(n, int) and n > 0 and j.exists() and p.exists()):
                continue
            if any(partition.glob(f"{name}.parquet.*.bak")) or part_paths(partition, name):
                continue
            with pq.ParquetFile(str(p)) as pf:
                if pf.metadata.num_rows != n:
                    continue
            j.unlink()
            removed.append(name)
        except Exception:
            continue
    return removed


def drop_manifest_sidecars(partition: Path) -> list[str]:
    """Apply (and then remove) the sidecar manifest a closing session left in ``partition``."""
    m = Path(partition) / SIDECAR_MANIFEST
    try:
        todo = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    removed = drop_sidecars(partition, todo if isinstance(todo, dict) else {})
    try:
        m.unlink()
    except OSError:
        pass
    return removed


# =============================================================================
# The writer
# =============================================================================
class TapeWriter:
    MAX_BACKLOG = 120          # parquet batches (~a few minutes on a stalled disk)
    BIG_SIDECARS = ("tick_tape", "level_tape", "basis_tape", "decision_tape")

    def __init__(
        self,
        root_dir: str | Path,
        trade_date: date,
        schema_version: str = SCHEMA_VERSION,
        flush_threshold: int = 256,
        rotate_secs: float = 60.0,
        sidecar_jsonl: str = "small",
        compression: str = "zstd",
    ):
        self.schema_version = schema_version
        self.partition = Path(root_dir) / trade_date.isoformat()
        self.partition.mkdir(parents=True, exist_ok=True)
        self.flush_threshold = flush_threshold
        self.rotate_secs = float(rotate_secs)
        self.compression = compression
        mode = str(sidecar_jsonl or "small").lower()
        self._jsonl_for = {t: (mode == "all" or (mode == "small" and t in SMALL_TAPES))
                           for t in S.PARQUET_TAPES}

        self._buffers: dict[str, list[dict[str, Any]]] = {t: [] for t in S.PARQUET_TAPES}
        self._buf_since: dict[str, float] = {}
        self._writers: dict[str, pq.ParquetWriter] = {}
        self._part_idx: dict[str, int] = {}
        self._part_opened: dict[str, float] = {}
        self._jsonl: dict[str, Any] = {}
        self._closed = False
        self._start_ms = now_ms()
        self._open_paths: dict[str, str] = {}
        # leftover parts of a killed earlier session today: 2.1.1 -- merged on a BACKGROUND thread from a
        # snapshot of the part list taken now (the 3 Nov restart spent ~2 min of a live session on this
        # merge before the first round). The new session never collides with those names (see _writer).
        self.recovered: list[str] = []
        self._recovery_thread: threading.Thread | None = None
        snapshot = {name: part_paths(self.partition, name) for name in list(S.PARQUET_TAPES) + ["raw_ticks"]}
        snapshot = {n: ps for n, ps in snapshot.items() if ps}
        # 2.2: say "an engine is writing here" BEFORE the first part of this session exists, so a
        # detached merge (vajra.tape_merge) never picks up a file we still have open. If such a merge
        # is already working on the earlier session's parts, they are its job, not ours.
        self._live_path = self.partition / LIVE_NAME
        self._live_touched = 0.0
        self._live_stop = threading.Event()
        self._touch_live()

        def _keep_live():                              # its own timer: before 09:20 no round runs, but the
            while not self._live_stop.wait(LIVE_TOUCH_SECS):     # raw tick recorder already writes parts here
                self._touch_live(force=True)
        threading.Thread(target=_keep_live, name="vajra-tape-live", daemon=True).start()
        self.merge_in_progress = bool(snapshot) and _foreign_merge(self.partition)
        if self.merge_in_progress:
            snapshot = {}
        self.recovering = sorted(snapshot)
        if snapshot:
            def _recover():
                for name, ps in snapshot.items():
                    try:
                        if merge_parts(self.partition, name, S.TAPE_SCHEMAS.get(name), compression, parts=ps):
                            self.recovered.append(name)
                    except Exception:
                        continue
            self._recovery_thread = threading.Thread(target=_recover, name="vajra-tape-recover", daemon=True)
            self._recovery_thread.start()
        # 2.0: parquet row-groups are converted + written on a BACKGROUND thread (1.x did it on the
        # engine thread, stalling rounds by 13-100+ ms), and the jsonl sidecars are flushed once per
        # round (1.x flushed the disk after every single row, ~50 times per round).
        self._q: "queue.Queue" = queue.Queue(maxsize=self.MAX_BACKLOG)
        self.parquet_rows_dropped = 0
        self.disk_errors = 0
        self.parquet_errors = 0
        self._last_disk_log = 0.0
        self._last_drop_log = 0.0
        self._dirty: set = set()
        self._last_jflush = time.monotonic()
        # 2.0.3 (21 Oct: ~9 GB of jsonl per day beside the parquet): at a CLEAN close, a large jsonl
        # sidecar whose every row is verifiably in its parquet is removed. Off unless app.py turns it on.
        self.drop_verified_jsonl = False
        self._jrows: dict[str, int] = {}
        self._prows: dict[str, int] = {}
        self._jfresh: dict[str, bool] = {}
        self._closed_after_event_warned = False
        self._worker = threading.Thread(target=self._work, name="vajra-tape", daemon=True)
        self._worker.start()

    # ---- background parquet thread --------------------------------------------
    ROWGROUP_ROWS = 2048            # 2.1.1: rows accumulated per row group (was one 256-row group per batch:
    ROWGROUP_SECS = 20.0            # ~8,000 groups a day made the close merge take minutes)

    def _work(self):
        import logging
        pending: dict[str, list] = {}
        since: dict[str, float] = {}

        def _write(name, rows):
            if not rows:
                return
            for i in range(0, len(rows), 1024):         # bounded GIL hold per conversion
                chunk = rows[i:i + 1024]
                table = pa.Table.from_pylist(chunk, schema=S.TAPE_SCHEMAS[name])
                self._writer(name).write_table(table, row_group_size=len(chunk))
            self._prows[name] = self._prows.get(name, 0) + len(rows)

        while True:
            item = self._q.get()
            try:
                if item is None:
                    for name, rows in pending.items():
                        try:
                            _write(name, rows)
                        except Exception as e:
                            self.parquet_errors += 1
                            logging.getLogger("vajra.tape").error("parquet write failed for %s: %r", name, e)
                    return
                name, rows = item
                if name == "__flush__":                 # flush(): everything buffered goes to disk now
                    for nm, buf in list(pending.items()):
                        try:
                            pending[nm] = []
                            _write(nm, buf)
                        except Exception as e:
                            self.parquet_errors += 1
                            logging.getLogger("vajra.tape").error("parquet write failed for %s: %r", nm, e)
                    continue
                try:
                    buf = pending.setdefault(name, [])
                    if not buf:
                        since[name] = time.monotonic()
                    buf.extend(rows)
                    due = len(buf) >= self.ROWGROUP_ROWS or time.monotonic() - since[name] >= self.ROWGROUP_SECS
                    # a part about to rotate must carry everything buffered for it (the footer closes it)
                    opened = self._part_opened.get(name)
                    if opened is not None and time.monotonic() - opened >= self.rotate_secs:
                        due = True
                    if due:
                        pending[name] = []
                        _write(name, buf)
                        self._maybe_rotate(name)
                except Exception as e:                # one exotic row must NOT abort a live session
                    self.parquet_errors += 1
                    pending[name] = []
                    logging.getLogger("vajra.tape").error(
                        "parquet write failed for %s (%d rows): %r", name, len(rows), e)
            finally:
                self._q.task_done()

    def _writer(self, tape_name: str) -> pq.ParquetWriter:
        """The CURRENT part writer for a tape (opened on first use / after a rotation)."""
        w = self._writers.get(tape_name)
        if w is None:
            idx = self._part_idx.get(tape_name, 0) + 1
            self._part_idx[tape_name] = idx
            path = self.partition / f"{tape_name}.part-{idx:05d}.parquet"
            while path.exists():                        # never clobber (a part of a recovered session)
                idx += 1
                self._part_idx[tape_name] = idx
                path = self.partition / f"{tape_name}.part-{idx:05d}.parquet"
            w = pq.ParquetWriter(str(path), S.TAPE_SCHEMAS[tape_name], compression=self.compression)
            self._writers[tape_name] = w
            self._part_opened[tape_name] = time.monotonic()
            self._open_paths[tape_name] = str(path.resolve())
            OPEN_PARTS.add(self._open_paths[tape_name])  # a reader merging today must skip this file
        return w

    def _release_part(self, tape_name: str) -> None:
        p = self._open_paths.pop(tape_name, None)
        if p is not None:
            OPEN_PARTS.discard(p)

    def _maybe_rotate(self, tape_name: str) -> None:
        """Close the current part (footer!) once it is older than rotate_secs; the next batch opens
        a new one. Runs on the tape thread, never on the engine's path."""
        opened = self._part_opened.get(tape_name)
        if opened is None or time.monotonic() - opened < self.rotate_secs:
            return
        w = self._writers.pop(tape_name, None)
        if w is not None:
            try:
                w.close()
            except Exception as e:
                self.parquet_errors += 1
                import logging
                logging.getLogger("vajra.tape").error("part close failed for %s: %r", tape_name, e)
            finally:
                self._release_part(tape_name)

    # ---- internal -----------------------------------------------------------
    def _jsonl_handle(self, tape_name: str):
        if tape_name not in self._jsonl:
            path = self.partition / f"{tape_name}.jsonl"
            self._jfresh[tape_name] = not path.exists() or path.stat().st_size == 0
            self._jsonl[tape_name] = open(path, "a", encoding="utf-8")
        return self._jsonl[tape_name]

    def _disk_error(self, e):
        self.disk_errors += 1
        now = time.monotonic()
        if now - self._last_disk_log > 30:
            self._last_disk_log = now
            try:
                from .console import warn
                warn(f"  !! TAPE WRITE FAILING ({e!r}) — {self.disk_errors} rows lost so far. FREE DISK SPACE. "
                     f"The engine keeps trading.")
            except Exception:
                pass

    def _enqueue(self, name, rows):
        """Bounded hand-off (teardown T06: on a slow disk the queue grew without limit -> GBs of RAM
        and a 68 s close). When full, the PARQUET copy of this batch is dropped and the drop is
        counted + logged once a minute."""
        try:
            self._q.put_nowait((name, rows))
        except queue.Full:
            self.parquet_rows_dropped += len(rows)
            now = time.monotonic()
            if now - self._last_drop_log > 60:
                self._last_drop_log = now
                import logging
                logging.getLogger("vajra.tape").error(
                    "TAPE_BACKLOG: disk too slow, parquet batch dropped (%d rows so far)",
                    self.parquet_rows_dropped)

    # ---- public API ---------------------------------------------------------
    def append(self, tape_name: str, row: dict[str, Any]) -> None:
        """Append one row to a parquet tape (with a jsonl durability sidecar for the small tapes)."""
        if self._closed:
            raise RuntimeError("TapeWriter is closed")
        if tape_name not in self._buffers:
            raise KeyError(f"unknown parquet tape {tape_name!r}")
        row = dict(row)
        row.setdefault("schema_version", self.schema_version)
        if "ts" not in row:
            raise ValueError(f"tape row for {tape_name} missing required 'ts' (ms)")
        if self._jsonl_for.get(tape_name, False):
            # Durable, immediate jsonl write. A full / failing disk must NOT kill the trading session
            # (teardown T15: it ended the day mid-trade with no SHUTDOWN): count, log, keep trading.
            try:
                fh = self._jsonl_handle(tape_name)
                fh.write(json.dumps(row, separators=(",", ":"), default=_json_default) + "\n")
                self._dirty.add(tape_name)
                self._jrows[tape_name] = self._jrows.get(tape_name, 0) + 1
            except OSError as e:
                self._disk_error(e)
        # Buffer for columnar parquet row-group; hand full buffers to the background writer.
        buf = self._buffers[tape_name]
        if not buf:
            self._buf_since[tape_name] = time.monotonic()
        buf.append(row)
        if len(buf) >= self.flush_threshold:
            self._buffers[tape_name] = []
            self._enqueue(tape_name, buf)
        if time.monotonic() - self._last_jflush > 1.0:   # safety net if end_round() is never called
            self.end_round()

    def round(self, row: dict[str, Any]) -> None:
        """2.0.1: one compact jsonl line per engine round (round_tape.jsonl) — timing, feed health,
        gate, what the cockpit showed. Flushed with the other sidecars at end_round()."""
        if self._closed:
            return
        try:
            fh = self._jsonl_handle("round_tape")
            fh.write(json.dumps(row, separators=(",", ":"), default=_json_default) + "\n")
            self._dirty.add("round_tape")
        except OSError as e:
            self._disk_error(e)

    def client(self, payload: dict[str, Any]) -> None:
        """2.0.1: a dashboard beacon (client_tape.jsonl): what the BROWSER painted and played."""
        if self._closed:
            return
        try:
            fh = self._jsonl_handle("client_tape")
            fh.write(json.dumps({"recv_ms": now_ms(), **payload}, separators=(",", ":"), default=_json_default) + "\n")
            fh.flush()
        except OSError as e:
            self._disk_error(e)

    def release_live(self) -> None:
        """Every part in the folder is closed: drop the live marker so a merge may start."""
        self._live_stop.set()
        try:
            self._live_path.unlink()
        except OSError:
            pass

    def _touch_live(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._live_touched and now - self._live_touched < LIVE_TOUCH_SECS:
            return
        if self._live_stop.is_set():
            return
        self._live_touched = now
        try:
            with open(self._live_path, "w") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass

    def end_round(self) -> None:
        """Flush the jsonl sidecars once per engine round (durability within one round). Buffered
        parquet rows older than ~rotate_secs/4 are handed to the writer too, so a quiet tape (few
        rows per minute) still lands in a closed part promptly."""
        for name in list(self._dirty):
            fh = self._jsonl.get(name)
            if fh is not None:
                try:
                    fh.flush()
                except Exception:
                    pass
        self._dirty.clear()
        if not self._closed:
            self._touch_live()
        now = time.monotonic()
        self._last_jflush = now
        age_lim = max(1.0, self.rotate_secs / 4.0)
        for name, buf in self._buffers.items():
            if buf and now - self._buf_since.get(name, now) >= age_lim:
                self._buffers[name] = []
                self._enqueue(name, buf)

    def event(self, event_type: str, payload: dict[str, Any], ts_ms: int | None = None) -> None:
        """Append one row to event_tape (§9.7, jsonl-only)."""
        if self._closed:
            # 2.1: an event after the close (a late callback) is logged, never a crash in a finally.
            if not self._closed_after_event_warned:
                self._closed_after_event_warned = True
                import logging
                logging.getLogger("vajra.tape").error("event %r after the tape was closed", event_type)
            return
        if event_type not in S.EVENT_TYPES:        # self-enforcing contract — no undeclared types
            # 2.0: never kill a live session over a typo'd event type (1.x raised from inside the
            # round loop). Record it, flagged, and let tests catch it (test_event_types_declared).
            import logging
            logging.getLogger("vajra.tape").error("undeclared event_type %r", event_type)
            payload = {"undeclared_event_type": event_type, **(payload or {})}
            event_type = "ANOMALY"
        row = {
            "ts": int(ts_ms) if ts_ms is not None else now_ms(),
            "event_type": event_type,
            "schema_version": self.schema_version,
            "payload": payload,
        }
        try:
            fh = self._jsonl_handle("event_tape")
            fh.write(json.dumps(row, separators=(",", ":"), default=_json_default) + "\n")
            fh.flush()                             # events are rare and important: flush immediately
        except OSError as e:
            self._disk_error(e)

    def flush(self, tape_name: str | None = None, timeout: float = 30.0) -> None:
        """Write every buffered row now and WAIT (bounded) until the background writer has caught up."""
        self.end_round()                                # 2.1.1: the jsonl rows (order_tape!) hit the disk FIRST --
        names = [tape_name] if tape_name else list(self._buffers.keys())   # a window-close gives us ~4 s
        for name in names:
            rows = self._buffers.get(name) or []
            if rows:
                self._buffers[name] = []
                try:
                    self._q.put((name, rows), timeout=5.0)
                except queue.Full:
                    self.parquet_rows_dropped += len(rows)
        try:
            self._q.put(("__flush__", None), timeout=5.0)
        except queue.Full:
            pass
        self._wait_drained(timeout)

    def _wait_drained(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            if not self._worker.is_alive():
                break
            time.sleep(0.01)

    def close(self, timeout: float = 120.0, merge: bool = True) -> None:
        """Flush, close every part, merge the parts into the canonical parquet files. Every step is
        isolated: a failure in one tape (full disk) must not leave the others unclosed.
        ``merge=False`` (2.2, the live engine) leaves the closed parts for ``vajra.tape_merge``, which
        the caller starts detached -- the merge takes minutes and must not hold the window open."""
        if self._closed:
            return
        self._closed = True                            # late callers (beacons) are refused, not crashed
        try:
            self.flush(timeout=min(timeout, 30.0))
        except Exception:
            pass
        try:
            self._q.put(None, timeout=5.0)
        except queue.Full:
            pass
        self._worker.join(timeout=min(timeout, 30.0))
        for name, w in list(self._writers.items()):
            try:
                w.close()
            except Exception:
                self.parquet_errors += 1
            finally:
                self._release_part(name)
        self._writers.clear()
        for fh in self._jsonl.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        rt = self._recovery_thread
        if rt is not None and rt.is_alive():            # (a same-day restart's repair; normally long done)
            rt.join(timeout=min(timeout, 60.0))
        if not merge:                                   # the caller still has the raw-tick part open in this
            # folder: it calls release_live() when that is shut. The sidecars this session may drop are
            # handed to the detached merge in a small manifest (11 Nov: a 212 MB decision_tape.jsonl
            # stayed behind, because the drop used to run only after an in-process merge).
            if self.drop_verified_jsonl:
                self._write_sidecar_manifest()
            return
        self.release_live()
        # 2.1.1: the six merges run side by side (Arrow releases the GIL); one read per part.
        from concurrent.futures import ThreadPoolExecutor

        def _merge(name):
            try:
                merge_parts(self.partition, name, S.TAPE_SCHEMAS[name], self.compression)
            except Exception as e:
                self.parquet_errors += 1
                import logging
                logging.getLogger("vajra.tape").error("merge failed for %s: %r (parts kept)", name, e)
        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(_merge, list(S.PARQUET_TAPES)))
        if self.drop_verified_jsonl:
            self._drop_verified_sidecars()

    def _droppable_sidecars(self) -> dict:
        """{tape: rows} for the big jsonl sidecars this session wrote completely: created by this
        session, nothing dropped or failed, no earlier-session parquet .bak, parquet row count equal."""
        out = {}
        if self.parquet_rows_dropped or self.disk_errors or self.parquet_errors:
            return out
        for name in self.BIG_SIDECARS:
            j = self.partition / f"{name}.jsonl"
            if not (j.exists() and self._jfresh.get(name)):
                continue
            if any(self.partition.glob(f"{name}.parquet.*.bak")):
                continue
            n = self._jrows.get(name, 0)
            if n == 0 or self._prows.get(name, 0) != n:
                continue
            out[name] = n
        return out

    def _write_sidecar_manifest(self) -> None:
        todo = self._droppable_sidecars()
        if not todo:
            return
        try:
            tmp = self.partition / (SIDECAR_MANIFEST + ".tmp")
            tmp.write_text(json.dumps(todo), encoding="utf-8")
            tmp.replace(self.partition / SIDECAR_MANIFEST)
        except OSError:
            pass

    def _drop_verified_sidecars(self) -> list[str]:
        """Remove a big jsonl sidecar only when: this session created it, nothing was dropped or failed,
        there is no earlier-session parquet .bak, and the parquet footer reads back the same row count."""
        return drop_sidecars(self.partition, self._droppable_sidecars())

    def __enter__(self) -> "TapeWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _json_default(o: Any):
    # Tolerate numpy scalars / dates that might sneak in.
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, date):
        return o.isoformat()
    return str(o)


__all__ = ["TapeWriter", "now_ms", "merge_parts", "recover_partition", "parquet_sources",
           "iter_parquet_rows", "rebuild_from_jsonl", "part_paths"]
