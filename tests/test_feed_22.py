"""2.2 -- what the 9 Nov 2026 tape showed.

  * NIFTY ran 25-50 s behind the exchange for two hours (93 socket drops): the websocket reader did
    three small ``recv`` calls per frame and paid one interpreter switch interval for each while another
    thread was busy. The socket is now read in 64 KB chunks through ``_BufferedSock``.
  * entries are paused while the NIFTY stream is late (the exchange-lag gate).
  * the end-of-day merge runs in a detached process; an interrupted one is found at the next start.
"""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

from vajra.breeze import BreezeWrapper, BreezeCredentials, _BufferedSock


class _Sock:
    """A socket that hands out its bytes in whatever chunk is asked for, counting the calls."""

    def __init__(self, data: bytes):
        self.data, self.calls, self.timeout = bytearray(data), 0, 45.0

    def recv(self, n):
        self.calls += 1
        out = bytes(self.data[:n])
        del self.data[:n]
        return out

    def gettimeout(self):
        return self.timeout

    def fileno(self):
        return 7


def test_buffered_sock_serves_many_small_reads_from_one_network_read():
    frames = b"".join(bytes([0x81, 5]) + b"hello" for _ in range(1000))          # 7,000 bytes
    raw = _Sock(frames)
    b = _BufferedSock(raw)
    got = bytearray()
    for _ in range(1000):
        got += b.recv(2)                      # header
        got += b.recv(5)                      # payload
    assert bytes(got) == frames and raw.calls == 1
    assert b.gettimeout() == 45.0 and b.fileno() == 7        # everything else is the real socket's
    assert b.recv(4) == b""                                  # a closed socket still reads as closed


def test_buffered_sock_passes_large_reads_and_short_data_straight_through():
    raw = _Sock(b"ab")
    b = _BufferedSock(raw)
    assert b.recv(2) == b"ab" and not b._b                   # nothing left behind in the buffer
    raw2 = _Sock(b"x" * 200_000)
    b2 = _BufferedSock(raw2)
    assert len(b2.recv(100_000)) == 100_000                  # a read larger than the chunk is not capped


class _Part:
    skip_utf8_validation = False


class _Ws:
    def __init__(self):
        self.sock = _Sock(b"")
        self.frame_buffer, self.cont_frame = _Part(), _Part()


def test_hook_buffers_each_new_socket_once_and_can_be_switched_off():
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    ws = _Ws()
    bw._buffer_socket(ws)
    assert isinstance(ws.sock, _BufferedSock) and ws.frame_buffer.skip_utf8_validation
    assert ws.cont_frame.skip_utf8_validation and bw.sockets_buffered == 1
    inner = ws.sock
    bw._buffer_socket(ws)
    assert ws.sock is inner                                  # never wrapped twice
    off = BreezeWrapper(BreezeCredentials("k", "s", "t"), buffered_reader=False)
    ws2 = _Ws()
    off._buffer_socket(ws2)
    assert not isinstance(ws2.sock, _BufferedSock) and ws2._vajra_buf
    assert "buffered" in bw.health()


def test_trigger_hook_wraps_the_socket_of_a_reconnected_engineio():
    class _Eio:
        def __init__(self):
            self.ws = _Ws()
            self.ping_timeout = 20

        def _trigger_event(self, event, *a, **k):
            return "lib"

    class _Sio:
        def __init__(self):
            self.eio = _Eio()

        def _handle_eio_message(self, data):
            pass

    class _H:
        def __init__(self):
            self.sio = _Sio()
            self.tokenlist = set()

    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    h = _H()
    bw._hook_eio(h)
    eio = h.sio.eio
    eio._trigger_event("message", "42[]")
    assert isinstance(eio.ws.sock, _BufferedSock)
    eio.ws = _Ws()                                           # the library reconnected: a new websocket
    eio._trigger_event("message", "42[]")
    assert isinstance(eio.ws.sock, _BufferedSock) and bw.sockets_buffered == 2


# ---- exchange-lag gate ---------------------------------------------------------------------------
class _Sounds:
    def __init__(self):
        self.rang = []

    def emit(self, kind, ts, text, force=False):
        self.rang.append(kind)
        return True


class _Brz:
    tick_lag_s = None


def _gate_host(limit=6.0):
    from vajra.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._breeze, o._max_exch_lag, o._lag_closed, o._lag_floor = _Brz(), limit, False, None
    o._lag_closed_ms, o._lag_alarmed, o._lag_alarm_ms = 0, False, 10_000
    o.sounds, o.events = _Sounds(), []
    o._emit_event = lambda et, p, ts_ms=None: o.events.append((et, p))
    return o


def test_lag_gate_closes_above_the_limit_and_reopens_below_half():
    o = _gate_host()
    assert o._lag_gate(1) is False                           # no measurement yet
    o._breeze.tick_lag_s = 1.2
    assert o._lag_gate(2) is False
    o._breeze.tick_lag_s = 9.5
    assert o._lag_gate(3) is True and o.sounds.rang == []     # entries pause at once, the alarm waits
    assert o.events[-1][0] == "FEED_GAP" and o.events[-1][1]["stage"] == "exch_lag"
    o._breeze.tick_lag_s = 5.0                               # better, but not below half the limit
    assert o._lag_gate(4) is True and len(o.events) == 1     # one event per episode
    assert o._lag_gate(10_003) is True and o.sounds.rang == ["feed_alarm"]   # still paused 10 s later
    assert o._lag_gate(20_000) is True and o.sounds.rang == ["feed_alarm"]   # once per episode
    o._breeze.tick_lag_s = 2.0
    assert o._lag_gate(20_250) is False and o.events[-1][1]["stage"] == "exch_lag_recovered"
    assert o.events[-1][1]["paused_secs"] == 20.2


def test_a_short_broker_stall_pauses_entries_without_an_alarm():
    """11 Nov: 10:43 and 13:25 -- the broker sent nothing for 12-15 s, then a burst drained in ~7 s."""
    o = _gate_host()
    o._breeze.tick_lag_s = 1.7
    o._lag_gate(0)
    o._breeze.tick_lag_s = 11.7
    assert o._lag_gate(1_000) is True
    o._breeze.tick_lag_s = 1.9
    assert o._lag_gate(8_000) is False and o.sounds.rang == []


def test_lag_gate_ignores_a_laptop_clock_that_runs_fast():
    o = _gate_host()
    for ts, lag in enumerate([9.0, 9.4, 8.8, 9.9, 10.5]):   # a steady ~9 s reading = clock skew, not lag
        o._breeze.tick_lag_s = lag
        assert o._lag_gate(ts) is False
    o._breeze.tick_lag_s = 17.0                              # 8 s worse than the best reading
    assert o._lag_gate(9) is True
    assert o.events[-1][1]["lag_secs"] == 8.2
    o._breeze.tick_lag_s = 10.0
    assert o._lag_gate(10) is False


def test_lag_gate_is_off_in_replay_and_when_disabled():
    o = _gate_host()
    o._breeze = None
    assert o._lag_gate(1) is False
    o2 = _gate_host(limit=0)
    o2._breeze.tick_lag_s = 99.0
    assert o2._lag_gate(1) is False


# ---- detached tape merge -------------------------------------------------------------------------
def _write_parts(tmp_path, d=date(2026, 11, 9)):
    from vajra.tape import TapeWriter
    tw = TapeWriter(tmp_path, d, rotate_secs=0.05, flush_threshold=5)
    for k in range(3):
        for i in range(10):
            tw.append("order_tape", {"ts": 1000 + 10 * k + i, "action": "ENTRY", "instrument": "NIFTY",
                                     "strike": 25000, "right": "CE", "qty": 65, "intended_price": 100.0,
                                     "fill_price": 100.0, "slippage": 0.0, "order_id": f"P{k}{i}",
                                     "live_orders_flag": False, "exit_reason": "", "pnl_realized": 0.0})
        tw.flush()
        time.sleep(0.12)
    return tw


def test_close_without_merge_leaves_parts_and_merge_partition_finishes_the_job(tmp_path):
    import pyarrow.parquet as pq
    from vajra import tape_merge
    from vajra.tape import part_paths
    tw = _write_parts(tmp_path)
    tw.close(merge=False)
    tw.release_live()
    part = tw.partition
    assert part_paths(part, "order_tape") and not (part / "order_tape.parquet").exists()
    assert tape_merge.has_parts(part)
    assert tape_merge.stale_partitions(tmp_path) == [part]
    assert tape_merge.stale_partitions(tmp_path, exclude=part) == []
    assert "order_tape" in tape_merge.merge_partition(part)
    assert pq.ParquetFile(str(part / "order_tape.parquet")).metadata.num_rows == 30
    assert not tape_merge.has_parts(part) and not (part / tape_merge.LOCK_NAME).exists()
    assert tape_merge.merge_partition(part) == []            # nothing left to do


def test_a_held_lock_keeps_a_second_merge_out_and_a_stale_one_is_ignored(tmp_path):
    from vajra import tape_merge
    tw = _write_parts(tmp_path)
    tw.close(merge=False)
    tw.release_live()
    lock = tw.partition / tape_merge.LOCK_NAME
    lock.write_text("123")
    assert tape_merge.merge_partition(tw.partition) == [] and tape_merge.has_parts(tw.partition)
    old = time.time() - tape_merge.STALE_LOCK_SECS - 60
    os.utime(lock, (old, old))
    assert "order_tape" in tape_merge.merge_partition(tw.partition)


def test_spawned_merge_process_merges_the_folder(tmp_path):
    import pyarrow.parquet as pq
    from vajra import tape_merge
    tw = _write_parts(tmp_path)
    tw.close(merge=False)
    tw.release_live()
    root = Path(__file__).resolve().parents[1]
    assert tape_merge.spawn([tw.partition], root=root) is True
    deadline = time.time() + 600        # a low-priority process on a busy laptop can take minutes
    while time.time() < deadline and tape_merge.has_parts(tw.partition):
        time.sleep(0.25)
    assert not tape_merge.has_parts(tw.partition)
    assert pq.ParquetFile(str(tw.partition / "order_tape.parquet")).metadata.num_rows == 30
    assert tape_merge.spawn([tw.partition], root=root) is False     # nothing to merge -> no process


def test_a_live_engine_keeps_the_merge_out_of_its_folder(tmp_path):
    """A same-day restart while the earlier session's merge is still queued: the new engine's marker
    must stop the detached merge from touching the folder (it could pick up a file still being written)."""
    from vajra import tape_merge
    from vajra.tape import LIVE_NAME, live_marker_fresh
    tw = _write_parts(tmp_path)
    assert (tw.partition / LIVE_NAME).exists() and live_marker_fresh(tw.partition)
    tw.close(merge=False)                                    # parts closed, raw recorder (not here) still open
    assert live_marker_fresh(tw.partition)
    assert tape_merge.merge_partition(tw.partition) == [] and tape_merge.has_parts(tw.partition)
    tw.release_live()
    assert not (tw.partition / LIVE_NAME).exists()
    assert "order_tape" in tape_merge.merge_partition(tw.partition)


def test_inline_close_removes_the_marker_and_a_dead_engines_marker_goes_stale(tmp_path):
    from vajra import tape_merge
    from vajra.tape import LIVE_NAME, LIVE_FRESH_SECS, live_marker_fresh
    tw = _write_parts(tmp_path)
    tw.close()                                               # inline merge (replay / tests)
    assert not (tw.partition / LIVE_NAME).exists() and not tape_merge.has_parts(tw.partition)
    tw2 = _write_parts(tmp_path, d=date(2026, 11, 6))        # an engine that is killed: never closed
    tw2._live_stop.set()
    old = time.time() - LIVE_FRESH_SECS - 5
    os.utime(tw2.partition / LIVE_NAME, (old, old))
    assert not live_marker_fresh(tw2.partition)


def test_a_new_session_leaves_parts_to_a_merge_that_is_already_running(tmp_path):
    """The in-process repair and a detached merge working on the same parts wrote every row twice."""
    import pyarrow.parquet as pq
    from vajra.tape import TapeWriter, LOCK_NAME, merge_parts, part_paths
    tw = _write_parts(tmp_path)
    tw.close(merge=False)
    tw.release_live()
    n_parts = len(part_paths(tw.partition, "order_tape"))
    (tw.partition / LOCK_NAME).write_text("999999")           # another process is merging this folder
    assert merge_parts(tw.partition, "order_tape") is None    # a reader must not merge them either
    tw2 = TapeWriter(tmp_path, date(2026, 11, 9), rotate_secs=0.05, flush_threshold=5)
    assert tw2.merge_in_progress and tw2.recovering == [] and tw2._recovery_thread is None
    assert len(part_paths(tw.partition, "order_tape")) == n_parts
    (tw.partition / LOCK_NAME).unlink()                       # that merge finished / died
    tw2.close()
    assert pq.ParquetFile(str(tw.partition / "order_tape.parquet")).metadata.num_rows == 30


def test_priority_is_really_raised_on_windows():
    """The first version passed a truncated 64-bit handle and failed without a word."""
    import sys
    import pytest
    from vajra import power
    if sys.platform != "win32":
        pytest.skip("Windows only")
    before = power.current_priority_class()
    try:
        assert power.raise_priority("above_normal") is True
        assert power.current_priority_class() == 0x00008000
        assert power.raise_priority("normal") is False and power.raise_priority("bogus") is False
    finally:
        if before:
            power._set_priority_class(before)


def test_detached_close_hands_the_verified_jsonl_sidecars_to_the_merge(tmp_path):
    """11 Nov: decision_tape.jsonl (212 MB) stayed behind -- the drop ran only after an in-process merge."""
    import json as _json
    from vajra import tape_merge
    from vajra.tape import TapeWriter, SIDECAR_MANIFEST
    d = date(2026, 11, 11)
    tw = TapeWriter(tmp_path, d, rotate_secs=0.05, flush_threshold=5)
    tw.drop_verified_jsonl = True
    for k in range(3):
        for i in range(10):
            tw.append("decision_tape", {"ts": 1000 + 10 * k + i, "instruments": [], "raw_bias": 0.0,
                                        "bias_ewma": 0.0, "bias_threshold": 0.25, "routing_mode": "CONFIRM",
                                        "divergence_flag": False, "reversal_freeze_flag": False,
                                        "expiry_gate_flag": True, "decision": "HOLD", "reason_code": "NO_APPROACH",
                                        "regime": "NON_EXPIRY", "feed_ok": True, "nearest_level_dist": 5.0})
        tw.end_round(); tw.flush(); time.sleep(0.12)
    part = tw.partition
    assert (part / "decision_tape.jsonl").exists()
    tw.close(merge=False)
    tw.release_live()
    assert _json.loads((part / SIDECAR_MANIFEST).read_text()) == {"decision_tape": 30}
    done = tape_merge.merge_partition(part)
    assert "decision_tape" in done and "dropped decision_tape.jsonl" in done
    assert not (part / "decision_tape.jsonl").exists() and not (part / SIDECAR_MANIFEST).exists()


def test_a_sidecar_whose_parquet_does_not_match_is_kept(tmp_path):
    import json as _json
    from vajra.tape import drop_manifest_sidecars, SIDECAR_MANIFEST
    import pyarrow as pa
    import pyarrow.parquet as pq
    part = tmp_path / "2026-11-11"
    part.mkdir()
    (part / "decision_tape.jsonl").write_text("{}\n" * 5)
    pq.write_table(pa.table({"ts": [1, 2, 3]}), str(part / "decision_tape.parquet"))
    (part / SIDECAR_MANIFEST).write_text(_json.dumps({"decision_tape": 5}))
    assert drop_manifest_sidecars(part) == []
    assert (part / "decision_tape.jsonl").exists() and not (part / SIDECAR_MANIFEST).exists()
