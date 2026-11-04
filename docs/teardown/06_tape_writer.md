# 06 — Tape writer (vajra/tape.py): teardown
Verdict: Ready for Monday as a writer. Live recording is about the same as 1.0, a little better on engine cadence under disk stalls. Durability after a crash or restart is worse in 2.0. The tape layout did not change, but the 2.0 ReplaySource now prefers the parquet, which is the file a crash or restart leaves broken or partial.
Score (your area): 1.0 = 6/10, 2.0 = 6/10 (writer alone 7/10; the replay read path drags it down)

Scope: vajra/tape.py in both trees, plus its callers (orchestrator.py:385-817,966; app.py:118-169) and its readers
(feed.py ReplaySource, prev-close seed). Every TapeWriter call runs on ONE engine thread. Feed events go
through `_feed_event`, which runs inside the synchronous `rounds()` generator. The dashboard never touches the tape.
So the only concurrency is the engine thread against the new `vajra-tape` worker.
Note: the box ran at load average 12–28 on 2 CPUs during these tests, so absolute ms are inflated. Compare 1.0 and 2.0 run side by side, not against the CHANGELOG figures.

## Findings (most severe first)

### [HIGH] 2.0 replay can no longer read a crashed or killed session, and silently drops everything before a same-day restart (vajra/feed.py:85-96; cause in tape.py:97-103)
- Background: ParquetWriter writes its footer only in `close()`. After a crash, kill -9, closed console window, power loss or a
  hung shutdown, `tick_tape.parquet` has row groups but no footer. Both versions behave the same here (reproduced: `ArrowInvalid: Parquet magic bytes not found`).
  1.0 ReplaySource read the **jsonl first** and only fell back to parquet. 2.0 reads the **parquet first** whenever it exists, with no fallback.
- Repro 1 (crash): I let `killer.py` write real-sized rounds with the 2.0 writer, then sent SIGKILL after 12 s.
  ```
  2.0 ReplaySource: replay FAILED after 0 rounds: ArrowInvalid Parquet magic bytes not found in footer
  1.0 ReplaySource: replay rounds: 359
  ```
- Repro 2 (crash, then same-day relaunch, which is what the operator does: 16 Oct had 4 STARTUP_CONFIGs): I opened a new 2.0
  TapeWriter on the same folder, wrote 300 rounds and closed it. `_writer()` renamed the broken parquet to
  `tick_tape.parquet.<ms>.bak` and started a fresh one.
  ```
  2.0 replay rounds after crash+restart: 300      <- the morning's 359 rounds silently missing
  1.0 replay rounds after crash+restart: 659
  ```
  The same thing happens after a *graceful* mid-day restart (token refresh). The earlier session sits in a valid `.bak` that nothing reads.
  It also happens when a parquet batch is dropped by the worker's except (tape.py:73-75). The parquet then has a
  silent hole of 256 rows (about 10 rounds), and 2.0 replays straight through that hole.
- Impact: replay and post-mortem of exactly the days that went wrong are lost or quietly wrong. Live trading on Monday is not affected.
  It gets worse over time: the manual (OPERATOR_MANUAL.md:221-223) tells the operator to delete old jsonl and "keep the
  parquet". On a crash day that keeps the broken file and deletes the good one.
- Fix (choose one): (a) in ReplaySource, if `ParquetFile()` raises, or if any `*.parquet.*.bak` exists beside it, or if the
  parquet's last ts is older than the jsonl's last line, use the jsonl. (b) Better: make the parquet crash-safe by rolling
  segments (`tick_tape.part-0001.parquet`, closing each every N minutes or N row groups) and replay all segments plus
  `.bak` files in ts order. (c) Change the manual: never delete a day's jsonl unless its parquet opens and its row count equals the jsonl line count.

### [MEDIUM] Unbounded queue: a slow disk makes memory grow without limit and close() take a minute or more (tape.py:56, 126, 171)
- The queue is `queue.Queue()` with no maxsize and no monitoring. Steady state produces about 1.14 parquet batches per second
  (24 tick + 24 basis + 24 level + 1 decision rows per round = 73 rows; one batch per 256 rows; 4 Hz).
  One 256-row tick batch takes **1.96 MB** of Python memory (tracemalloc, real 15 Oct rows).
- Repro: stress.py, paced at 4 Hz, 15 ms of simulated engine work per round, `write_table` slowed by 1 s (stands in for AV scanning or a sync client on D:\):
  ```
  1.0: 2.4 rounds/s (engine falls behind), round wall p99=3347 ms max=29638 ms, close()=4.2 s
  2.0: 4.0 rounds/s (keeps cadence),       round wall p99=258 ms,  max=7829 ms
       queue grew linearly 0 -> 57 batches in 150 s, RSS 57 -> 111 MB, close()=68.3 s
  ```
  Extrapolated over a 6 h session that is about 0.36 MB/s, or roughly 7–8 GB, which means OOM on the operator's box. 1.0 cannot run out of memory this way, because it slows the engine instead.
  `finalize()` → `tape.flush()` → `q.join()` has no timeout. The 15:25 flatten is written before the flush, but a 68 s shutdown invites a
  second Ctrl+C, and that skips `close()`, so all parquet footers are lost (see the HIGH finding).
- On a healthy disk the queue stayed small: max 5 at 4 Hz pace, max 28–62 flat-out at 18–60 rounds/s, and always drained (end-of-run qsize 0–2).
- Fix: `queue.Queue(maxsize=64)`. When it is full, drop the parquet batch (the jsonl already has the rows) and emit an ANOMALY
  `TAPE_BACKLOG` once. Do not block the engine. Expose qsize on the dashboard health strip. In `close()`, wait with a deadline
  (`q.join` loop with timeout) and then close the writers anyway.

### [MEDIUM] The background thread does not take the parquet conversion off the engine's GIL (tape.py:59-72; CHANGELOG.md:75-77)
- `pa.Table.from_pylist` takes about 85% of a batch's cost and holds the GIL the whole time. I ran a second Python thread alongside it:
  ```
  from_pylist(256 tick rows)=40.8 ms  write_table=5.1 ms
  5x from_pylist: other python thread's max GIL starvation gap = 41.5 ms   (= one whole conversion)
  5x write_table: other thread max gap = 18.1 ms
  ```
  So whenever the worker converts while the engine is mid-round, the engine is frozen for the full conversion, just as in 1.0.
  The win only comes when the conversion happens to fall into the idle part of the 250 ms round.
- Measured: 1.0 and 2.0 run at the same time, 1200 rounds, 4 Hz, 15 ms simulated engine work:
  ```
  1.0: tape cost mean 11.9 ms, p99 69 ms;  round wall p50 24.7 / p99 94.5 ms
  2.0: tape cost mean  6.3 ms, p99 47 ms;  round wall p50 22.7 / p99 93.1 ms
  ```
  The mean improves, but p99 round time is unchanged. Flat out (unpaced) 2.0 was slower: p50 10.9 vs 6.0 ms and 15.2 vs 9.9 ms mean, because of GIL contention and thread switching.
  The CHANGELOG line "Engine stalls removed from the hot path" overstates it. The jsonl change (one flush per round) is the real, smaller win.
- Fix (if stalls matter): build columns incrementally per round. Append scalars to per-column Python lists on the engine thread,
  which is cheap. The worker then builds `pa.array` per column, which is much faster than `from_pylist` on nested dicts. Alternatively, convert in chunks of 32 rows so the GIL is released between chunks.
  Or move parquet writing into a subprocess that tails the jsonl.

### [MEDIUM] Windows rename while the file is open: a whole day's parquet is lost, and the only signal is a log line (tape.py:93-103) — same as 1.0
- If `tick_tape.parquet` is open in another process at restart (the operator's pandas or Excel session, a crashed VAJRA that is still
  dying, Defender), `Path.rename` raises PermissionError. `_writer()` fails, the worker logs it and drops the batch.
  It retries on the next batch, fails again, and keeps doing so for the rest of the day.
- Repro (rename monkeypatched to raise WinError 32-style PermissionError, 60 rounds): 6 lines of
  `parquet write failed for tick_tape (256 rows safe in jsonl): PermissionError(13, ...)`. Afterwards the folder held only the jsonl plus the old file.
  No event_tape row, no dashboard indication, and stderr gets one line every ~2.7 s all day.
- Fix: on a rename failure, fall back to a new file name (`tick_tape.<startms>.parquet`) instead of the fixed name, and emit an
  ANOMALY event once. Readers should glob `tick_tape*.parquet`.

### [LOW] Crash durability of the jsonl goes from about 0 rows lost to at most one round (tape.py:118-140) — acceptable
- 2.0 buffers jsonl writes for up to one round. `end_round()` runs every round (orchestrator.py:817), and there is a 1 s safety net (tape.py:127).
- SIGKILL test, 8 trials each, real rows: **0 rows lost from completed rounds in both versions, 0 torn last lines**.
  Rows (~3.9 KB) are smaller than the 8 KB BufferedWriter, which flushes whole earlier lines before buffering a new one. 200-append probe: the on-disk tail was mid-line 0 of 200 times in both.
  Neither version fsyncs, so an OS crash or power loss is equivalent in both. `event()` still flushes immediately (tape.py:161).
- Fix: none needed. Optionally, on open, append "\n" if the existing jsonl does not end in one, so a restart cannot glue onto a torn line.

### [LOW] The worker thread dying would hang shutdown (tape.py:62-77, 171)
- `except Exception` covers realistic failures, and `task_done()` is in a `finally`. If the worker died from a BaseException, `flush()`'s `q.join()` would block forever.
  `close()`'s `join(timeout=30)` never even runs, because `flush()` comes first. Not reproducible with real inputs.
- Fix: `if not self._worker.is_alive(): write synchronously` inside `flush()`/`append`. Use the bounded wait described above.

### Checked and fine
- **Row ordering:** one FIFO queue and one worker, so batches keep order. Every stress run verified that ts is monotonic across the whole parquet
  and that parquet rows equal jsonl lines equal expected (e.g. 345,600 / 345,600 tick rows in the 14,400-round run).
- **`_writer()` race:** only the worker creates writers. `close()` touches `_writers` only after `flush()` has drained the queue and the sentinel has been joined. No race.
- **Flush during close:** `close()` = flush (join) → sentinel → join → close writers → close jsonl. The order is correct, and a second `close()` returns immediately.
- **Row aliasing:** rows are shallow-copied (tape.py:113). I checked every row builder for nested objects the engine might mutate after hand-off
  (`_tick_row` strikes, `instruments_vector` decision_maker.py:123, signal/order rows). All are freshly built per round, so there is no torn read.
- **Memory over a session:** steady RSS growth is about 4.5 MB per 1000 rounds, **identical in 1.0** (1.0: 129/137/146 MB at
  2k/4k/6k rounds; 2.0: 129/137/…/186 MB at 2k/4k/…/14k). The cause is ParquetWriter's in-memory row-group metadata (footer of 3.0 MB per 1350 tick row groups),
  not the queue. Extrapolated over a full 90k-round day that is about 130 MB baseline plus 400 MB. Fine on the operator's PC. Live 15 Oct footers: tick 16.6 MB, level 17.6 MB.
- **Replay mode isolation:** replay writes to `tapes_replay/` (app.py:118), so the recorded day is never renamed to `.bak`. This is a real 1.0 bug fixed.
- **Undeclared event_type:** it is now an ANOMALY instead of a crash (tape.py:146-152). Better.

## What 2.0 does better than 1.0 (evidence)
- Engine cadence survives a slow disk: 4.0 rounds/s vs 2.4, p99 258 vs 3347 ms (1 s write stall test).
- Mean tape cost per paced round is 6.3 vs 11.9 ms. The jsonl is flushed once per round instead of once per row.
- Replay never writes into or renames the recorded day. A typo'd event type no longer kills the session.

## What 2.0 does worse than 1.0 (evidence)
- ReplaySource prefers the parquet: a crashed day fails to replay (0 rounds vs 359), and a restarted day silently loses everything before the restart (300 vs 659 rounds).
- The unbounded queue makes OOM possible on a persistently slow disk (+0.36 MB/s measured), and close() took 68 s vs 4 s.
- Unpaced throughput is lower (61 vs 96 rounds/s in the first run) because of GIL contention. No live impact at 4 Hz.

## Tests / checks you ran
All scripts are in /home/claude/teardown/scratch/06/. The round template is one real 15 Oct round: 24 tick, 24 basis, 24 level and 1 decision rows, 114 KB of JSON (round.pkl).
- `stress.py <root> <out> N [--pace 4 --engine-ms 15 --slowdisk MS --devnull-jsonl]`: 2000-round flat-out runs (x3 interleaved),
  1200-round paced runs (concurrent 1.0/2.0), 600-round slow-disk runs, a 14,400-round (1 h equivalent) 2.0 memory run, and an 8,000-round 1.0 memory run.
- `gil.py`: component timings and the GIL starvation probe.
- `killer.py` + `killtest.py`: 8+8 SIGKILL trials at 4 Hz and flat-out. Checked lost rows, torn tails and parquet readability.
- Crash-then-replay and crash-restart-replay through both ReplaySources (results above).
- Rename-PermissionError emulation. tracemalloc size of a 256-row tick batch.
- Inspected the recorded 14/15/16 Oct tapes: row groups are 256 rows, footers are 0.2–17.6 MB, and every day ended with a graceful SHUTDOWN. 16 Oct shows 3 failed Breeze
  starts before the real one, so restarts do happen.
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_determinism_and_tapes.py` → 9 passed. No test covers kill, restart or replay fallback.
