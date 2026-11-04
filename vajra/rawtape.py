"""VAJRA 2.0.1 — raw broker-tick recorder (EOD forensics).

The engine's tick_tape is a 4/s SNAPSHOT of the ATM±N window. It replays the engine exactly, but it
cannot show what the broker actually sent and when: a late packet, a burst, a garbage print, a
silent websocket. This recorder writes EVERY normalised websocket tick with its receive time to
``tapes/<date>/raw_ticks.parquet`` on a background thread, bounded, never on the engine's path.

2.1: written as closed PART files (one every ``rotate_secs``) and merged at a clean close, so a
killed session keeps its raw ticks (2.0's single open writer left a footer-less, unreadable file).

Columns: recv_ms (wall clock at receipt), key, stock_code, kind, strike, right, ltp, bid, ask,
vwap, volume, oi, ltt (the exchange's last-trade time string when the broker sends one).
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .tape import merge_parts, _backup

SCHEMA = pa.schema([
    pa.field("recv_ms", pa.int64()),
    pa.field("key", pa.string()),
    pa.field("stock_code", pa.string()),
    pa.field("kind", pa.string()),
    pa.field("strike", pa.int64()),
    pa.field("right", pa.string()),
    pa.field("ltp", pa.float64()),
    pa.field("bid", pa.float64()),
    pa.field("ask", pa.float64()),
    pa.field("vwap", pa.float64()),
    pa.field("volume", pa.int64()),
    pa.field("oi", pa.int64()),
    pa.field("ltt", pa.string()),
])

NAME = "raw_ticks"


class RawTickRecorder:
    BATCH = 4_000           # rows per parquet row group (2.1.1: 20k dicts -> Arrow held the GIL 60-150 ms)
    MAX_BACKLOG = 250       # batches queued before we start dropping (disk far slower than feed)

    def __init__(self, partition: str | Path, enabled: bool = True, rotate_secs: float = 120.0,
                 max_batch_secs: float = 5.0):
        self.enabled = bool(enabled)
        self.partition = Path(partition)
        self.path = self.partition / f"{NAME}.parquet"
        self.rotate_secs = float(rotate_secs)
        self.max_batch_secs = float(max_batch_secs)
        self._buf: list[dict[str, Any]] = []
        self._buf_since = time.monotonic()
        self._q: "queue.Queue" = queue.Queue(maxsize=self.MAX_BACKLOG)
        self._lock = threading.Lock()
        self.rows = 0
        self.dropped = 0
        self._writer = None
        self._part_idx = 0
        self._part_opened = 0.0
        self._closed = False
        if self.enabled:
            try:
                merge_parts(self.partition, NAME, SCHEMA)       # a killed earlier session today
            except Exception:
                pass
            if self.path.exists():                      # same-day restart: keep the earlier capture
                try:
                    _backup(self.path)
                except OSError:
                    pass
            self._thr = threading.Thread(target=self._work, name="vajra-rawtape", daemon=True)
            self._thr.start()

    def record(self, norm: dict, raw: dict | None = None, recv_ms: int | None = None) -> None:
        """Called from the websocket thread with the normalised tick (cheap: append to a list)."""
        if not self.enabled or self._closed:
            return
        ltt = None
        if raw is not None:
            ltt = raw.get("ltt") or raw.get("last_trade_time") or raw.get("exchange_time") or raw.get("ltt_time")
        row = {
            "recv_ms": int(recv_ms if recv_ms is not None else time.time() * 1000),
            "key": norm.get("_key"), "stock_code": norm.get("stock_code"), "kind": norm.get("kind"),
            "strike": norm.get("strike"), "right": norm.get("right"),
            "ltp": norm.get("ltp"), "bid": norm.get("bid"), "ask": norm.get("ask"), "vwap": norm.get("vwap"),
            "volume": norm.get("volume"), "oi": norm.get("oi"),
            "ltt": str(ltt) if ltt is not None else None,
        }
        with self._lock:
            self._buf.append(row)
            now = time.monotonic()
            if len(self._buf) >= self.BATCH or now - self._buf_since >= self.max_batch_secs:
                b, self._buf = self._buf, []
                self._buf_since = now
                self._enqueue(b)

    def _enqueue(self, rows):
        try:
            self._q.put_nowait(rows)
        except queue.Full:
            self.dropped += len(rows)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                b, self._buf = self._buf, []
                self._buf_since = time.monotonic()
                self._enqueue(b)

    def _open_part(self):
        self._part_idx += 1
        p = self.partition / f"{NAME}.part-{self._part_idx:05d}.parquet"
        while p.exists():
            self._part_idx += 1
            p = self.partition / f"{NAME}.part-{self._part_idx:05d}.parquet"
        self._writer = pq.ParquetWriter(str(p), SCHEMA, compression="zstd")
        self._part_opened = time.monotonic()
        from .tape import OPEN_PARTS
        self._open_path = str(p.resolve())
        OPEN_PARTS.add(self._open_path)                 # a reader merging today must skip this file

    def _work(self):
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                try:
                    tbl = pa.Table.from_pylist(item, schema=SCHEMA)
                    if self._writer is None:
                        self._open_part()
                    self._writer.write_table(tbl)
                    self.rows += len(item)
                    if time.monotonic() - self._part_opened >= self.rotate_secs:
                        self._writer.close()
                        self._writer = None
                        self._release_part()
                except Exception:
                    self.dropped += len(item)
            finally:
                self._q.task_done()

    def close(self) -> None:
        if not self.enabled or self._closed:
            return
        self._closed = True
        self.flush()
        try:
            self._q.put(None, timeout=5.0)
        except queue.Full:
            pass
        try:
            self._thr.join(timeout=30)
        except Exception:
            pass
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
            self._release_part()
        try:
            merge_parts(self.partition, NAME, SCHEMA)
        except Exception:
            pass

    def _release_part(self) -> None:
        p = getattr(self, "_open_path", None)
        if p is not None:
            from .tape import OPEN_PARTS
            OPEN_PARTS.discard(p)
            self._open_path = None


__all__ = ["RawTickRecorder", "SCHEMA"]
