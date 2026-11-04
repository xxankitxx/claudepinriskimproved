# 26 — R26 Performance reviewer: findings

Scripts (all read-only against the repo): `/home/claude/vajra2_review/scratch/26/`
`cadence.py` (live cadence from basis_tape), `slow_why.py` (slow rounds vs tape flush), `bench.py` (5-min streaming profile, null/real writer, cProfile),
`stages.py` (per-stage timers), `micro.py` (component micro-benchmarks), `livebuild.py` (live `_build_snapshot` cost), `v2proto.py` (monkey-patched v2 prototype + equivalence check + GC test).
Measurement notes: sandbox has 2 shared vCPUs (load average up to 8.8 from other reviewers), so I report **thread CPU time** (`time.thread_time_ns`), not wall time, unless stated. Window: 10-15, 10:00-10:05 (after 1 min unmeasured warm-up), 1,197 rounds x 24 instruments x 26 strikes. Equivalence windows: 10-15 09:55-10:15, 10-16 15:10-15:26 (2 trades), 10-14 14:10-14:30.

## 0. Headline numbers (measured)

| item | value |
|---|---|
| Engine only (`process_round`, null tape) | **1.85-2.0 ms/round** mean, p99 2.9-3.2, max 4-5 (0.8% of the 250 ms budget) |
| Engine + current TapeWriter (jsonl + parquet) | **5.2-5.6 ms/round** mean, p95 12.4-13.1, p99 15.6-17.2, max 21-29 (flush rounds) |
| Live feed side (`snapshot_raw` + 24 x `_build_snapshot`) | 1.19 ms/round CPU (2.4 ms p95 with a 6,000 ticks/s synthetic ws thread) |
| Total round-thread CPU today (feed + engine + tape) | about **6.5 ms mean = 2.6% of 250 ms**; p99 about 19 ms |
| Live round period actually achieved (basis_tape NIFTY ts deltas) | **3.73 Hz (10-14), 3.82 Hz (10-15), 3.93 Hz (10-16)** vs 4.00 target |
| Live dt percentiles p50 / p95 / p99 / max | 10-14: 251 / 365 / 511 / 1,334 ms; 10-15: 251 / 329 / 454 / 1,868 ms; 10-16: 251 / 267 / 357 / 1,030 ms |
| Rounds slower than 300 ms | 10.7% / 7.4% / 2.8%; rounds >1 s: 7 / 10 / 1; time lost vs 4 Hz: 413 s / 763 s / 180 s (5.6% / 3.7% / 1.2% of session span) |
| Tape bytes per round today | **104.8 KB/round** (tick jsonl 82 KB + parquet 4.5 KB + level/basis/decision jsonl) = 9.8 GB per 6.5 h at 4 Hz |
| v2 prototype (see section 2) | round-thread 1.7-1.9 ms mean (p99 3.6, max 9) incl. tape; **0.9 KB/round = 0.08 GB/6.5 h**; process CPU 2.7-3.0 ms/round incl. writer thread |

Conclusion in one line: **throughput is not the problem; tail latency and disk are.** Mean CPU is under 3% of the budget. The 4 Hz shortfall comes from stalls, and the recorded stalls line up with tape flushes. The engine hot path is already cheap; polishing it buys under 10%.

## 1. Correctness / reliability defects (most severe first)

### [HIGH] Tape I/O runs synchronously on the round thread, and the round stalls line up with parquet flushes  (tape.py:80-143, orchestrator.py:289-298, 664)  certainty: confirmed-by-running (correlation) + confirmed-by-reading (mechanism)
- What happens: `append` does `json.dumps` + `fh.write` + `fh.flush()` per row (about 72 write syscalls per round), and every 256 buffered rows calls `Table.from_pylist` + `ParquetWriter.write_table` (SNAPPY, one row group per flush) inline. At 24 rows/round the tick/basis/level buffers fill every ~10.7 rounds (about 2.7 s), the decision buffer every 256 rounds (64 s).
- Evidence (`slow_why.py`, reconstructing the flush round from cumulative row counts, round i duration = ts[i+1]-ts[i]):

| day | flush rounds slow (>300 ms) | non-flush rounds slow | share of all slow rounds that are flush rounds (flush rounds are 9.4% of rounds) | decision-flush rounds slow |
|---|---|---|---|---|
| 10-14 | 43.8% (mean 325 ms) | 7.3% | 38% | 5.6% |
| 10-15 | 34.8% (mean 305 ms) | 4.5% | 44% | 34.1% |
| 10-16 | 19.8% (mean 276 ms) | 1.1% | 64% | 3.4% |

  Median excess over 250 ms in slow rounds is 90-109 ms on the operator's machine, but only 13-25 ms of CPU for the same flush on this Linux box. The gap is the operator's I/O environment (Windows file handling, antivirus on a file appended and flushed 72x per round, GIL contention with the ws/uvicorn threads). I cannot reproduce that here. Flush is a 3-9x enrichment of slow rounds, but it explains only 38-64% of them (see next finding).
- Why it matters: a 0.3-1.9 s round gap delays the 7 pt approach entry and, worse, the hard-stop/trail check while a position is open (12% SL on a fast option premium). It also makes the "4 Hz" claim false (3.73-3.93 Hz) and degrades continuation timing (`confirm_secs`) resolution.
- Fix (ordered): (1) drop the tick/basis/level jsonl sidecars (section 2.1); (2) move parquet writing to a background thread behind a `SimpleQueue`; the round thread only enqueues; (3) bound the queue and count drops into an event instead of blocking; (4) close/rotate part files so a crash loses at most seconds (R15 section 2.1). The prototype measured round-thread CPU p99 3.6 ms and max 9.3 ms with these changes (but see GC finding below, it must ship with it).

### [HIGH] No disk-space pre-flight; one live day is 2.7-8 GB and there is no rotation  (tape.py, OPERATOR_MANUAL.md:184-186, app.py:110)  certainty: confirmed-by-reading + measured
- Today: 104.8 KB/round x ~80k rounds = 8 GB (10-15). Manual says "8 GB, no auto-rotation, delete jsonl by hand". `grep disk_usage|shutil` over the repo returns nothing. A full disk mid-session makes `fh.write` raise `OSError` inside `process_round` (outside the per-instrument try/except) so `run()` catches it, calls `finalize`, and ends the trading day, possibly with a position open (the `finalize` path then also writes SHUTDOWN to the same full disk).
- This sandbox has 27 GB free: three worst-case v1 days. The three recorded days alone were 16.4 GB of jsonl if it was ever written (R15).
- Fix: pre-flight + runtime watchdog, code sketch in section 2.5.

### [MEDIUM] Buffering Python dict rows for seconds causes multi-hundred-ms garbage-collector pauses (design trap for any async writer)  (prototype, `v2proto.py`)  certainty: confirmed-by-running
- The natural v2 design (queue of row dicts, flush every 2 s, 8,192-row groups) buffers ~10k rows x 26 nested strike dicts, about 1M live Python objects. Measured in the 5-min window: a **gen-2 collection of 167 ms (with the replay corpus frozen) / 585-650 ms (harness corpus live)**, hitting the round thread. Total GC time 329-949 ms over 1,197 rounds vs 33 ms with the current writer (256-row buffers never reach gen 2).
- Fix: `gc.freeze()` after startup and `gc.set_threshold(50_000, 50, 100)` (measured: 26 collections, max pause 7.6 ms, round thread max 9.3 ms), or better, never buffer dicts: write columnar (numpy/Arrow builders per round) so the buffered object count is near zero. Do both. Add a test: round-thread max pause under 25 ms with the writer enabled.

### [MEDIUM] 4.5-7.3% of non-flush rounds are still slow and there is no instrumentation to say why  (orchestrator.py:423, feed.py:381-429)  certainty: confirmed-by-running (existence), plausible (cause)
- 10-14/15/16: 7.3% / 4.5% / 1.1% of non-flush rounds exceed 300 ms; clusters such as 10-15 15:05:35-15:05:46 (five consecutive rounds of 1.5-1.9 s) and 10-14 14:15:32-14:16:06 (83% of rounds slow for a minute) are not flush-shaped. Hour-level slow share is bursty (10-15 10:xx 12.5%, 14:xx 12.8%, 15:xx 14.4% vs 11:xx 2.8%), which points to market-activity-driven contention (ws callback thread holding `_lock` and the GIL while parsing/normalising bursts) and not to the engine (1.9 ms and flat).
- I measured the candidates I can: `snapshot_raw` dict copy of 673 keys = 2.7 us; `_build_snapshot` x24 = 1.19 ms; with a synthetic 6,000 ticks/s feeder thread the build rose to p95 2.4 ms, max 5.3 ms. So lock/GIL contention at that rate cannot explain 100+ ms excess. Unmeasured and plausible: real ws thread load (socket.io JSON parse), `_on_ticks` batches under the lock, Windows antivirus/disk, uvicorn threads, sleep granularity.
- Fix: stage timers on the round thread (t_raw_lock_wait, t_build, t_engine, t_tape, t_view, gc_ms, round_dur, `len(ticks)` since last round) written as one `health_tape` row per second (max over the second) plus an edge event `SLOW_ROUND` when a round exceeds 100 ms with the stage that dominated. Add `gc.callbacks` timing. One live day then settles the question.

### [MEDIUM] Dashboard `tick()` has no in-flight guard, polls at a fixed 250 ms unsynchronised to the engine, and re-fetches the whole state even when nothing changed  (dashboard.py:638-639, 995)  certainty: confirmed-by-reading + measured size
- `setInterval(tick,250)` with `await fetch` and no `_busy` flag: during an engine/GIL stall of 300+ ms two or three requests pile up and their responses can paint out of order (older state after newer). The watchdog (`_wdLastRtsChange`) is fed by whichever response lands last.
- Phase aliasing: the engine period is 251-260 ms and the browser period is 250 ms, so the poll drifts against the round: the cockpit shows a duplicated round roughly every 25-60 polls and skips one just as often. Sounds fire on the sampled state, so a one-round transient (approach inside 7 pts for 1-2 rounds) can be missed or double-seen.
- Cost on the server is small: `/api/state` is 20.8 KB, `json.dumps` 0.2 ms, about 0.08% of one core at 4 Hz per tab, 83 KB/s per tab (journal + level_history + events grow it to roughly 40-60 KB late in a busy day). So this is a correctness/UX finding, not a CPU one.
- Fix: guard (`if(_busy) return; _busy=true; try{...}finally{_busy=false}`); serve `GET /api/state?since=<round_ts>` returning 204 when unchanged; better, SSE (`/api/stream`) pushing on round change, with the cold fields (journal, level_history, events) sent only when their version counters change. Expected: ~5x fewer bytes, no pile-ups, rounds delivered exactly once.

### [LOW] `inferred_step()` is evaluated eagerly every round for every instrument even when the step is configured  (orchestrator.py:272)  certainty: confirmed-by-running
- `self.cfg.strike_step(inst, snap.inferred_step())` computes a sorted set of strikes (118.8 us/round, 5.9% of the engine) although NIFTY/BANKNIFTY/FINNIFTY have explicit steps and resolved stocks are in `strike_step` too. Fix: pass a lambda or call it only when the instrument is not in `cfg.raw["strike_step"]`.

### [LOW] Per-row `fh.flush()` x 72 per round; `flush_interval_secs: 5` is dead config  (tape.py:93, vajra_config.yaml:197)  certainty: confirmed-by-running (Linux page cache only)
- 24 rows written+flushed = 55 us here vs 14 us for one write+flush per round. On Windows each flush is a `WriteFile` and is the likely hidden cost (R15 flagged the same). Not measurable here; the fix is the same as above.

### [LOW] Loop pacing drifts instead of using a deadline  (feed.py:385-429)  certainty: confirmed-by-running
- `sleep(poll - dt)` after the work gives period = max(work, 0.25) + sleep overshoot: p50 is 251 ms (3.98 Hz even in a quiet minute), and nothing re-syncs after a slow round. Use `next += 0.25; sleep(max(0, next - now)); if now - next > 0.25: next = now`. Cosmetic (0.4%) but makes the "4 Hz" tests meaningful.

## 2. Improvements / design proposals (ranked, with expected gain and measured basis)

Per-round CPU budget (thread CPU, ms, this box). "Measured" = ran it; "est." = arithmetic.

| stage | current | proposed v2 | basis |
|---|---|---|---|
| live feed: `snapshot_raw` + 24 `_build_snapshot` | 1.19 | 1.19 (leave) | measured |
| engine `process_round` | 1.85-2.0 | 1.70-1.75 | measured (equal outputs) |
| tape on the round thread | 3.2-3.5 mean, 13-25 spikes every ~2.7 s | 0.1-0.2 (enqueue) | measured: v2 writer round-thread mean 1.73-1.89 vs 1.73 null-writer-fast |
| tape on a background thread | 0 | 0.9-1.0 ms/round-equivalent (process CPU 2.74-2.97 minus round thread) | measured |
| **round thread total** | **6.4-6.7 mean, p99 about 19, max about 30** | **about 3.1 mean, p99 about 3.6-5.7, max about 9** | measured/est. |
| bytes per round | 104.8 KB | 0.9 KB | measured |
| bytes per 6.5 h day at 4 Hz | 9.8 GB (record: 2.7-8.0 GB) | about 0.08-0.1 GB | measured, 5-min extrapolation |

### 2.1 Tape pipeline (biggest win; do first)
1. **Remove `tick_tape.jsonl`, `basis_tape.jsonl`, `level_tape.jsonl`**; keep jsonl only for what is tiny: event, order, signal, and decision rows that are not plain HOLD. Measured v1 jsonl in the window: tick 118.3 MB, level 13.7 MB, basis 8.2 MB, decision 3.3 MB vs parquet 5.4 / 0.9 / 0.7 / 0.05 MB. That alone removes 97% of the bytes and 1.0-1.1 ms (json) + 0.2 ms (write/flush) per round.
2. **ZSTD level 3, row groups of 8,192+**, level_tape written on change plus a 30 s heartbeat (R15 2.2; 99.86% of rows are `held`). Measured on the 5-min window (the prototype `V2Writer`, same schemas): tick 5.41 -> 1.00 MB (5.4x), basis 0.74 -> 0.25, level 0.90 -> 0.016, decision 0.049 -> 0.027; total 7.1 -> 1.29 MB (5.5x). 10-15 full day would be about 70-90 MB tick + small tapes instead of 286 + 105 MB.
3. **Background writer thread with a bounded `SimpleQueue`**, wall-clock flush every 2 s (makes `flush_interval_secs` real), **rotate part files every 15-60 s** so a `kill -9` loses at most one part (an open `ParquetWriter` without a footer is unreadable; R15). EOD compaction merges parts after a `tape_fsck` pass. Hard-kill exposure drops from "whole day" to "<= one part".
4. **Use orjson for the remaining jsonl rows** (`orjson.dumps` 66 us vs `json.dumps` 590 us per round of 24 tick rows, 9x; need `OPT_NON_STR_KEYS` only if keys are not str; handles NaN as null, fixing the bare-NaN issue in R15). Add to requirements.txt.
5. **Never buffer dict rows**: build Arrow columns per round (`pa.array` of numpy slices; strikes as `FixedSizeList` of 26x8 floats or flat long-format) so the writer thread holds near-zero Python objects and `from_pylist`'s GIL-held pass (3.5 ms per 256 tick rows, 13.7 us/row) disappears. Until then ship `gc.freeze()` + `gc.set_threshold(50000, 50, 100)` (measured above).
6. Order of attack: 1 + 2 first (zero engine change, expected: removes ~2.5 ms/round and the disk problem), then 3 + GC settings, then 5.

### 2.2 Engine hot path (small, safe, exact: all outputs byte-equal in my three equivalence windows)
`v2proto.py` variant `fast` = (a) one cached sorted strike view per snapshot used by `all_strikes_sorted`, `strikes_for`, `inferred_step` (4 sorts -> 1 per instrument per round); (b) single-pass `locate_pin` with the same tie-break key; (c) `round_to` fast path (floor(x/step+0.5) unless the fractional part is within 1e-9 of .5, then the Decimal path): 1.28 -> 0.18 us/call, **0 mismatches over 200k random (value, step) pairs**; (d) closed-form IST seconds-of-day `((ts_ms//1000)+19800)%86400` plus cached `parse_hhmm`. Result: **1.85-2.0 -> 1.73-1.75 ms (about 8% of the engine, 0.05% of the budget)**, and `rows`, `events` and `journal` compared equal against the baseline for 3 windows (10-15 09:55-10:15; 10-16 15:10-15:26 with 2 trades and 4 order rows; 10-14 14:10-14:30). Verdict: worth doing only because it is nearly free; do **not** spend review time on it.
Per-stage cost of `process_round` today (`stages.py`, 2,030 us/round with timer overhead): `locate_pin` 391 us (19%), `_tick_row` 281 (14%), `basis_monitor.update` 238 (12%; `RollingStats.std` O(30) in Python + `basis_signal_weight` -> `datetime.fromtimestamp`), `compute_basis` 140 (7%), `inferred_step` 119 (6%), `_vol_ratio` 89 (4%), `all_strikes_sorted` 86 (4%), `continuation.update` 62 (3%), `_update_view` 54 + `_build_chain` 51 + `_compute_alarm` 13 (latest_state rebuild total 118 us = 5.8%), `freshness.update` 39, `_level_row` 35, `decision.evaluate` 28.

### 2.3 Items you asked about, measured: what is and is not worth touching

| item | measured cost | verdict |
|---|---|---|
| Decimal `round_to` | 1.28 us/call, about 48 calls/round = 61 us/round (3% of engine, 0.024% of budget) | not a problem; fast path is exact and optional. Keep the Decimal path for ties (spec golden) |
| `json.dumps` per `append` | 0.59-1.05 ms/round (tick rows 3.4-3.6 KB each); the largest single CPU item in the tape | remove the jsonl (2.1.1); orjson for what remains |
| per-round `sorted(snapshots)` | **0.26 us/round** | negligible, leave it. It is also what makes bias float-sum order deterministic |
| `all_strikes_sorted` | 1.8 us x 24 = 43-86 us/round | fold into the cached view (2.2) |
| `latest_state` rebuild at 4 Hz | `_update_view` 54 + `_build_chain` 51 + `_compute_alarm` 13 = 118 us/round | negligible CPU. Optional: build the cold fields (`journal`, `level_history`, `capital`, `trades_summary`) only when they change |
| dict copy in `snapshot_raw` under the lock | 2.7 us for 673 keys (lock hold also 2.7 us) | negligible. The risk is `_on_ticks` holding the same lock across a whole batch of `_normalize_tick` calls (about 10 us/tick), not the copy. Use a per-key `latest` write without a global lock (dict assignment is atomic under the GIL), or swap two buffers |
| dashboard poll at 250 ms | 20.8 KB, 0.2 ms encode, 83 KB/s per tab | CPU negligible; the defect is aliasing/no in-flight guard (section 1) |
| engine as a whole | 1.85-2.0 ms vs 250 ms | adequate; it scales to roughly 20x today's universe (about 40 ms/round) before cadence is at risk |

### 2.4 Cadence and observability
- Add the `health_tape` described in section 1 (stage timers, tick backlog, GC ms, queue depth, round period max/p99 per second). Gate: `SLOW_ROUND` event edge-triggered at 100 ms; expect zero on a v2 day on the operator's machine, else the cause is named.
- Deadline pacing (section 1, LOW) and a reported achieved-rate line in the heartbeat (`rate_30s`).
- Keep NIFTY at 4 Hz; if load must drop later, R15 option C (non-NIFTY at 1 Hz) halves tape rows with no engine change, but it must go before the engine (tape what you consume).

### 2.5 Disk-space pre-flight and runtime watchdog (spec + sketch)
Thresholds are for the v2 layout (about 0.1 GB/day) and for a v1-compat mode (about 8-10 GB/day):

```python
# vajra/diskguard.py  (call from app.run before TapeWriter(...), and every 60 s from a watchdog thread)
import shutil, os, time
from pathlib import Path

V1_DAY_GB, V2_DAY_GB = 10.0, 0.25          # worst observed 8.0 GB (10-15) x 1.25 margin / v2 measured 0.09 x 2.7
def preflight(tape_root: Path, jsonl_sidecar: bool, keep_days: int = 5, mode: str = "live"):
    tape_root.mkdir(parents=True, exist_ok=True)
    probe = tape_root / ".vajra_probe"
    with open(probe, "wb") as f:           # writable + fsync works (catches read-only / locked / network drive)
        f.write(b"x" * 4096); f.flush(); os.fsync(f.fileno())
    probe.unlink()
    free_gb = shutil.disk_usage(tape_root).free / 1e9
    day_gb = V1_DAY_GB if jsonl_sidecar else V2_DAY_GB
    hard = max(2.0, 1.2 * day_gb)          # cannot even finish today -> REFUSE to start
    warn = max(5.0, 3.0 * day_gb)          # fewer than ~3 sessions of headroom -> loud warning + event DISK_LOW
    used_gb = sum(f.stat().st_size for f in tape_root.rglob("*") if f.is_file()) / 1e9
    return {"free_gb": free_gb, "need_today_gb": day_gb, "hard_gb": hard, "warn_gb": warn,
            "tapes_gb": used_gb, "ok": free_gb >= hard, "warn_flag": free_gb < warn,
            "est_days_left": free_gb / day_gb}

def watchdog_step(tape_root, bytes_written_session, secs_elapsed, secs_left_in_session):
    free = shutil.disk_usage(tape_root).free
    rate = bytes_written_session / max(secs_elapsed, 1)            # measured live write rate
    need = rate * secs_left_in_session * 1.5                        # projected to 15:30 with 50% margin
    if free < 200e6:  return "STOP"        # graceful: flatten, write SHUTDOWN(reason=DISK_FULL), close
    if free < need or free < 1e9: return "DEGRADE"   # drop tick/basis detail to event+decision only, emit DISK_LOW
    return "OK"
```
Behaviour: refuse a live start below `hard` (print the free GB and what to delete; also list the oldest day dirs eligible for archive); warn + event `DISK_LOW` below `warn`; during the session the watchdog projects the live write rate to 15:30 and degrades (stops tick/basis parquet, keeps decision/order/signal/event) before the disk fills; STOP flattens the paper position and shuts down cleanly rather than letting an `OSError` unwind `run()`. Also wrap `TapeWriter.append/event` I/O in a guard so an `OSError` becomes a counter + one `TAPE_ERROR` event (R15), never an unwound round. Auto-rotation: after EOD fsck, `gzip`/`zstd` day dirs older than 5 trading days, delete jsonl older than 2 days, never today's. For this sandbox the guard would read 27 GB free = 2.7 v1-days (warn) but 100+ v2-days (ok).

## 3. Recommendations specific to the two operator asks
- **A (non-expiry levels / narrower band):** performance impact of any narrowing method is nil: `compute_band` costs 0.2 us/round and runs only on re-anchor (every 20 min) or after exits; a regime/extrinsic estimate adds microseconds on those events. So do not choose the method on CPU grounds; nothing below 1 ms per re-anchor is visible. Do add the extra level_tape columns (R15 2.2) because the tape is change-only in v2 and costs about nothing. One caution: a method that recomputes the band every round (e.g. continuously tracking spot +/- k) would multiply `compute_band` + a level row by 4 Hz x 3 indices; still cheap CPU, but it defeats the change-only level_tape and the "held band" rule.
- **B (sounds):** sounds are derived in the browser from a polled snapshot (aliasing section 1), so a 1-2 round transient can be missed or doubled. Drive sounds from edge-triggered `ALERT` events delivered over SSE (once per episode, with `seq`) instead of diffing polled state; that also makes the sound count replayable (R15 section 3B). No performance cost (events are rare).

## 4. Things that are fine and should NOT be changed
- Per-instrument isolation try/except, tape-driven clocks, deterministic `sorted(snapshots)` order and float-sum order: all cost nothing and my fast-path variant preserved byte-equal outputs only because I left them alone.
- `round_to` Decimal ROUND_HALF_UP semantics (spec golden): keep, at most add the exact fast path with the tie fallback.
- `snapshot_raw` taking one lock per round and copying (2.7 us): fine.
- One-pass `_build_snapshot` per instrument sharing the round's `raw`: 1.19 ms for 24 instruments is good.
- NIFTY at 4 Hz: the engine can afford it (2-3% of the budget); only the I/O has to move off the thread.
- The `ts`-only, no-wall-clock engine design: it is why replay at 80k rounds can be benchmarked at all (replay speed is about 500 rounds/s engine-only per core).

## 5. Tests I would add
1. **Perf budget test** (CI, not full-day): replay a committed 2-minute fixture (about 480 rounds, 11.5k rows) through `process_round` with a null writer; assert thread-CPU p99 under 8 ms and mean under 4 ms (3-4x headroom over 1.85/2.9 measured), so a 10x regression fails loudly. Use `thread_time_ns`, not wall.
2. **Round-thread stall test:** same fixture with the real v2 writer; assert max round-thread wall under 40 ms and zero gen-2 GC pause over 25 ms (`gc.callbacks`).
3. **Bytes/round budget:** synthetic day -> tape bytes per round under 2 KB for v2 (today 104.8 KB); assert no `*.jsonl` larger than 10 MB per hour for tick/basis/level.
4. **Equivalence gate for any hot-path change:** run the `Rec` writer on the three windows in section 0 (including the 10-16 15:10-15:26 window with 2 trades) and assert tapes, events and journal equal the golden pickles.
5. **Disk guard tests:** monkeypatch `shutil.disk_usage` to 1 GB / 100 MB; assert refuse-start, DEGRADE, STOP, that STOP flattens and writes SHUTDOWN(DISK_FULL), and that `OSError` on `append` never raises out of `process_round`.
6. **Cadence test with a fake clock:** deadline pacing keeps 4.00 Hz average across a simulated 300 ms stall (no drift), emits `SLOW_ROUND`, and `rate_30s` is reported.
7. **Dashboard:** JS/unit test that `tick()` never has two requests in flight and that `?since=` returns 204; server test for `/api/state` payload size under 40 KB with 30 trades.
8. **Live-day acceptance (first v2 day):** achieved rate >= 3.95 Hz; rounds over 100 ms under 0.1%; zero rounds over 500 ms outside a recorded `FEED_GAP`; `health_tape` names the stage for every `SLOW_ROUND`.

## Side observations passed to other reviewers
- ITC is present in only 1.7% of rounds on 10-16 (BANKBARODA 98%); every other instrument is present in at least 99% of rounds on 10-15/16. Feed/data reviewers: check `WS_NAME_TO_INSTRUMENT`/subscription for ITC.
- `verify_vajra.py` (pytest, about 12 s) runs before every live start in `run_vajra.ps1`, adding that to the cold-start delay; fine, but a mid-session restart (the operator restarts often: 4 STARTUP_CONFIG on 10-16) should skip it behind a `-SkipVerify` switch when the code hash is unchanged.
