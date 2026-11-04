# 17 — Performance & memory over a full live day: teardown
Verdict: Ready for Monday on performance. 2.0 is better than 1.0: same median round cost, p99 round CPU cut ~2.4x, GC pauses cut ~30x, SSE delivers every round to 3 tabs at no measurable engine cost. No in-process structure grows without bound. The one real operational risk is unchanged from 1.0: about 8.6 GB/day of un-rotated jsonl on disk. Several CHANGELOG numbers (GC "~8 ms", unreproducible bench tool) are overstated or not reproducible.
Score (your area): 1.0 = 6/10, 2.0 = 8/10

## Method and caveats (read first)
- Harness `/home/claude/teardown/scratch/17/bench.py`: streams the real 15 Oct tick_tape (`iter_batches`), feeds each round to `Orchestrator.process_round` with the REAL `TapeWriter` (parquet + jsonl), and times each round with `perf_counter` (wall) and `thread_time` (CPU). It runs each version's own code (1.0 from `/home/claude/vajra2`, 2.0 from `/home/claude/teardown/vajra2`). The 2.0 live-mode GC settings (`collect; freeze; set_threshold(50000,50,100)`) are applied with `--gc live`.
- The box had load average 20-32 on 2 vCPUs the whole time (20 reviewers). **Wall-clock latency figures are therefore inflated 5-10x and not usable as absolute numbers**; I report CPU time (thread_time) as the reliable figure. `thread_time` still includes hypervisor steal, so isolated multi-second spikes are environment, not code (shown below). Absolute CPU on the operator's Windows machine will differ; the 1.0 vs 2.0 ratios are what matter.
- Mode `--nulljsonl` replaces the jsonl file handles with a byte counter so the full day does not write 8 GB to a shared disk. Everything else (json.dumps per row, parquet conversion and write) still runs. Real-file runs (no flag) were also done for the first windows (see disk section).
- ONE full-day replay done (2.0, 09:15-15:30, 79,747 rounds, 4,059 s wall). Windows of 2,000-3,000 rounds for the 1.0 vs 2.0 comparison. The live Breeze snapshot-build path (24 instruments, ws lock) cannot be exercised without the broker and is NOT measured.
- Note on a side effect: my first runs wrote their JSON result into the CWD, which the harness had chdir'd to the code roots. I moved those stray files out immediately and fixed the harness to use absolute paths; no source files were modified.

## Findings (most severe first)

### [MEDIUM] Disk: ~8.6 GB/day, no rotation, jsonl is 95% of it (tape.py:114-118, OPERATOR_MANUAL.md:221) — reproduced
- Full-day count from the 79,747-round run (bytes written to the jsonl sidecars, counted): tick_tape 6.575 GB, level_tape 0.974 GB, basis_tape 0.473 GB, decision_tape 0.190 GB, event/order/signal ~0. Parquet twins on disk: tick 285.8 MB, level 67.0 MB, basis 45.2 MB, decision 3.1 MB = 401 MB. **Total about 8.6 GB/day (jsonl 8.21 GB + parquet 0.40 GB).**
- 3,000-round real-file windows (`files_total_mb`): 1.0 = 315.1 MB, 2.0 = 323.7 MB. Extrapolated to a day: 1.0 ~8.4 GB, 2.0 ~8.6 GB. 2.0's level_tape.jsonl is 28% bigger (36.8 MB vs 28.8 MB per 3,000 rounds) because of the added regime/band_method/legacy_* columns; decision_tape +3%.
- Scenario: a week of runs is ~43 GB on `D:\`. A full disk mid-session makes `fh.write` raise inside `TapeWriter.append`, which is inside the round loop (the per-instrument try/except covers `_process_instrument` but `decision_tape` and the final flush are outside it, see orchestrator.py:803-818). The `run()` loop then ends with SESSION_ERROR and finalize, i.e. the engine stops trading for the day.
- Fix: before Monday, check free space on `D:` is above 20 GB. Medium term: stop writing `tick_tape.jsonl` (the parquet is the replay source and is 16x smaller), or gzip/rotate hourly; add a startup free-space guard that emits an ANOMALY event. 2.0 does not worsen this versus 1.0 beyond +2.6%.

### [LOW] GC change works but the claimed numbers are not reproduced (app.py:156-163, CHANGELOG.md:75-77) — reproduced
- The code comment claims 1.x GC paused the engine 167-650 ms and 2.0 "max pause measured ~8 ms". I measured GC pause duration with `gc.callbacks` using thread CPU time over the identical 2,000-round window (10:00 start, real TapeWriter):

| Run | collections | max pause | p99 pause | total GC time |
|---|---|---|---|---|
| 1.0 default GC | 921 | 46.2 ms | 6.0 ms | 791 ms |
| 2.0 default GC (no freeze) | 821 | 50.3 ms | 6.2 ms | 817 ms |
| 2.0 `--gc live` (freeze + 50000/50/100) | 27 | **19.5 ms** | 19.5 ms | 147 ms |

- So freeze + threshold gives 30x fewer collections and 5.5x less total GC time, and the worst pause fell 46-50 -> 19.5 ms. But it is 19.5 ms, not ~8 ms, and the 167-650 ms "1.x pauses" are not reproducible as CPU time (they appear only as wall-clock under load). Each surviving pause is a gen0 sweep of 50,000 young objects (about 5 ms mean, up to ~20 ms), once per ~83 rounds (~21 s). Harmless against a 250 ms budget; just do not trust the quoted figure.
- Full-day check of the setting: `gc_objs` (len(gc.get_objects())) stayed flat at 36-43k from round 2,500 to 79,747 and the number of collections was 958 for the day. No garbage accumulates under 50000/50/100. The freeze happens before `orch.run()` and only in `mode == "live"`, so replay/real/synthetic runs and my harness (which applies it manually) are not affected; the freeze does not cover objects created after startup (breeze tick dict, per-round state), which is fine because those are small.
- Fix: none needed; correct the comment/CHANGELOG to "max ~20 ms, 30x fewer collections".

### [LOW] `tools/latency_bench.py` has a SyntaxError, so the CHANGELOG's headline latency claims cannot be re-run (tools/latency_bench.py:30) — reproduced
- `python3 -c "import ast; ast.parse(open('tools/latency_bench.py').read())"` -> `SyntaxError: invalid syntax. Perhaps you forgot a comma?` at `pq.ParquetFile("str(Path(__file__).resolve().parent.parent / "tapes" / ...)")` (quoting mangled during a path edit). It also hard-codes 15 Oct 09:50 and port 8811/8812.
- Impact: the claims "rounds never delivered 0.5% -> 0%, delivery 38/68/264 -> 8/18/20 ms, engine p99/worst 20/250 -> 10/82 ms" (CHANGELOG.md:78-80) came from a version of the tool that is not what is shipped. My independent measurements below agree on the direction and roughly on the engine numbers.
- Fix: drop the stray quotes: `pq.ParquetFile(str(Path(__file__).resolve().parent.parent / "tapes" / "2026-10-15" / "tick_tape.parquet"))`.

### [LOW] Tape queue is unbounded and `close()` can abandon in-flight parquet writes (tape.py:62-76, 171-189) — read in code
- `self._q = queue.Queue()` has no maxsize. If the parquet thread falls behind (Windows AV scan, slow disk), batches of 256 tick rows (several MB each as Python objects) pile up in RAM instead of back-pressuring. In my runs `tape_q` was 0 at every sample (79,747 rounds), and converting 256 tick rows costs only ~2.7 ms CPU, so under normal conditions this never happens.
- `close()` calls `flush()` (which does `_q.join()`) and then `worker.join(timeout=30)`; with a stuck disk the join times out and `w.close()` runs concurrently with the worker. The jsonl sidecar still has every row, so no data is lost, only parquet completeness.
- Fix: `queue.Queue(maxsize=64)`; on `Full` fall back to a synchronous write or drop the parquet batch with an ANOMALY event (the jsonl stays authoritative).

### [LOW] SSE "full" payload can grow to ~100 KB late in a busy day; encoded on the event loop per tab (dashboard.py:68-95) — estimated
- Measured now: hot = 2.4-3.0 KB per round, full = 19-20 KB once per second (cap by design: `level_history[-80:]`, `journal[-80:]`, events deque 40, sound ring 30; orchestrator.py `_update_view`). With the all-in/no-cap rule a day can reach 80 journal rows and 80 level rows, adding roughly 40 KB + 36 KB (estimate from per-row JSON size, not measured) -> ~95 KB full payload per tab per second.
- Encode cost: `_clean` (recursive Python copy) + `json.dumps` measured at 0.63-0.66 ms CPU for the 20 KB full state and 0.06-0.11 ms for hot. Scaled 5x for a late-day full payload: ~3 ms CPU per tab per second, run on the uvicorn event loop (it competes with the engine for the GIL). Three tabs: ~10 ms CPU per second total. Not a problem, but `_clean` is done independently per client; caching one encoded string per round would make cost independent of tab count.
- Fix (optional): encode once per round in the engine thread after `_update_view` and let all generators send the cached bytes.

### [LOW] Unmeasured: live snapshot-build cost shares the engine thread (feed.py:411-449)
- `LiveBreezeSource.rounds()` builds all 24 snapshots (about 26 lookups each plus `VWAPTracker.resolve`) and then the engine runs, all in one thread, paced adaptively to 0.25 s. Replay cannot measure the build. The structure is bounded (`snapshot_raw` copies the ~1-2k-entry tick dict under one lock per round), so I expect low single-digit ms, but I have no number.

## Per-round engine latency: 1.0 vs 2.0 (real TapeWriter, real 15 Oct rounds, 10:00 start, 3,000 rounds)
CPU ms per round (thread_time; load-robust). 1.0 = `/home/claude/vajra2`, 2.0 = teardown snapshot.

| Run | p50 | p99 | max | mean |
|---|---|---|---|---|
| 1.0 (jsonl counted, no disk) | 5.89 | **28.7** | 340.6 (round #10 warm-up; next worst 44) | 8.02 |
| 1.0 (real files, 3,000 r) | 6.25 | 28.6 | 1,130 (steal/warm-up) | 9.91 |
| 2.0 default GC (real files) | 6.41 | 12.5 | 645 (steal) | 7.44 |
| 2.0 live GC (non-expiry regime) | 6.29 | 12.9 | 1,389 (steal) | 8.11 |
| 2.0 live GC, forced **expiry** regime (Mon 19 Oct code path: `resolve(2026-10-19)`) | 6.40 | 12.4 | **16.8** | 7.09 |
| 2.0 full day, 79,747 rounds, live GC | 5.85 | **11.0** | 3,410 (3 consecutive rounds at 11:30:58, see below) | 6.60 |

- p50 is the same (about 6 ms), but 2.0's p99 is 2.4x lower (28.7 -> 11-12.9 ms). The 1.0 tail comes from parquet conversion and per-row `fh.flush()` on the engine thread, which 2.0 moved to a background thread and one flush per round (tape.py:53-77, 120-140). The CHANGELOG "p99 20 -> 10 ms" matches my 28.7 -> 11-12.9 ms in direction and magnitude.
- Multi-second maxima are environment: in the full-day run, three consecutive rounds (#24822-24824, 11:30:58) cost 382/3,410/3,170 ms and wall max was 52.8 s. Replaying that exact window alone (`bench.py ... 11:30 600`, command in scratch) gave **max 21.5 ms**, p99 11.9 ms, so the spike is not deterministic and not data-driven (hypervisor stall on the shared box). The same kind of spike appears in 1.0 runs (1,130 ms, 340 ms at warm-up). Excluding those, the worst real rounds in clean runs are 14-21 ms (2.0) and ~45 ms (1.0).
- Wall-clock under the load of 20-30 (not meaningful as absolute): 2.0 full day p50 24.7 ms / p99 160.6 ms; rounds over 250 ms wall: 95 of 79,747, all during contention. CPU headroom: mean 6.6-8 ms CPU per 250 ms budget is ~3%.
- Where the time goes (cProfile, 400 rounds): `tape.append` cumulative 31% of engine time (json.dumps of the tick rows: 29k `iterencode` calls = the largest single cost), `_snapshot_from_row` (replay only, not live), `_tick_row` 7%, `sorted` / `round_to` (Decimal) / `pin_locator` lambdas are small. Writing the 82 KB/round tick jsonl line is the dominant engine cost. Dropping the jsonl would roughly cut round CPU by a third and remove the disk risk above.

## Full 6.25-hour simulation: memory growth (2.0, 79,747 rounds, accelerated)
Counters sampled every 2,500 rounds (`full_v2.json`, `.progress`):

| round | RSS MB | gc objects | journal | trades_log | level_history | events strip | sound ring | tape_q |
|---|---|---|---|---|---|---|---|---|
| 2,500 | 334 | 42,727 | 1 | 3 | 3 | 4 | 5 | 0 |
| 22,500 | 463 | 40,496 | 5 | 10 | 13 | 18 | 15 | 0 |
| 42,500 | 616 | 40,070 | 7 | 14 | 19 | 26 | 21 | 0 |
| 62,500 | 769 | 40,156 | 9 | 18 | 23 | 31 | 28 | 0 |
| 79,747 | 884 | 36,298 | 9 | 18 | 27 | 33 | 29 | 0 |

- Python-level state is bounded: tracked-object count is flat (36-43k) for the entire day; `journal`/`trades_log`/`level_history` grow only per trade/recompute (9 round trips, 27 NIFTY re-anchors that day) and are shown capped (`[-80:]`) in the state; `_recent_events` deque(40) and the sound ring `deque(30)` are capped; tape buffers hold at most 256 rows per tape (`tape_buf` 268-887 rows total at samples, always flushed by the threshold); `tape_q` was 0 at every sample. `RollingStats` windows, `FreshnessEngine`, `FeedHealth`, `ApproachEpisode`, `BasisMonitor`, `ContinuationValidator` hold scalars or fixed windows (read in code: mathx.py, engine/*.py). `VWAPTracker` is three dicts keyed by (inst, strike, right): bounded by the number of distinct strikes visited (at most ~1-2k keys) and only populated when the feed supplies no VWAP. `LiveBreezeSource._subscribed_options` / `_unmapped_reported` / `_step_obs` are bounded sets/dicts. `breeze.latest` is keyed by instrument/strike (bounded).
- Unbounded-by-design but small: `journal` and `trades_log` have no trade cap (binding rule 1). `_update_view` copies the journal and walks it about five times per round, i.e. O(trades) per round. At 300 round trips that is on the order of 0.1-0.2 ms per round (estimate, read in code); not an issue.
- RSS rose 334 -> 884 MB (about 7 MB per 1,000 rounds) **but this is a harness artifact, not an engine leak**. Controls: (a) reading the same tick_tape with `ParquetFile.iter_batches` and building tables, with NO writing, grows Arrow-allocated memory 34 MB per 500 row groups (`pqmem_ctl.py`: 34.5 -> 68.0 MB at 500 -> 1,000 groups) — about 6.4 MB per 1,000 rounds, i.e. ~90% of the observed slope; (b) `pq.ParquetWriter` writing 4,000 identical 256-row tick batches (more than half a day of tick data) stays flat at 225 MB with 0 MB Arrow-allocated (`pqmem2.py`). Live mode never reads the tape, so a live day should sit near 300-400 MB (windows ended at 300-345 MB after 3,000 rounds for both versions). The unexplained residual (~0.7 MB/1,000 rounds, ~55 MB/day) is small and bounded by the day; I could not attribute it further.
- 1.0 comparison: same counters in a 3,000-round window show equal memory behaviour (RSS 337 -> 346 MB, gc objects flat at 65k vs 40k for 2.0 because 2.0 freezes startup objects). Both keep the same bounded structures. I did not run a second full day for 1.0 (limit of one full-day replay per reviewer); its in-process structures are the same list/deque shapes by code reading.

## SSE with 2-3 browser tabs (live pace 4 Hz, engine and uvicorn in-process, 400 rounds, 15 Oct data)
Harness `sse_bench.py` (clients are real HTTP streaming readers). 2.0 = SSE (`/api/stream`), 1.0 = 250 ms polling of `/api/state` (its only mode).

| Config | engine CPU mean / p99 ms | rounds seen per tab | delivery lag p50 / p95 ms (loaded box) | bytes per tab |
|---|---|---|---|---|
| 2.0, 0 tabs | 7.87 / 13.1 | n/a | n/a | n/a |
| 2.0, 3 tabs SSE | **7.57 / 11.9** | **99.8-100%** | 29.7-30.0 / 74-81 | hot 2.6 KB per round + full 19.3 KB per second |
| 1.0, 3 tabs polling | 8.49 / 30.7 | 83-86% | 129-142 / 244-263 | 18.4 KB per poll (4/s) |

- 3 SSE tabs cost the engine nothing measurable (mean CPU 7.57 vs 7.87 with no tabs). Offline, per-encode CPU: full state 0.63-0.66 ms (clean 0.26 + dumps 0.33), hot state 0.06-0.11 ms. So 3 tabs cost about 2.6 ms CPU per second on the event loop.
- Bandwidth per tab: 2.0 ~ 3x2.6 + 19.3 = 27 KB/s; 1.0 polling ~ 74 KB/s. Per round the hot payload is 2.4-3.0 KB (CHANGELOG says "~5 KB"; actual is smaller).
- 1.0 polling under the same load missed 14-17% of rounds per tab; 2.0 missed at most 0.2%. This supports the operator's "tick updates without delay" requirement.
- Concern checked: each generator loops `await asyncio.sleep(0.02)` and calls `request.is_disconnected()` (50 wake-ups/s per tab); no measurable cost at 3 tabs. A generator exits when the client disconnects (read in code, dashboard.py:73-89); a half-open TCP connection (laptop sleep) would linger until the OS times out, which is bounded.
- Browser-side memory (read in code): the JS keeps `_spotBuf/_premBuf/_floorBuf` trimmed to the last 5 minutes (`while(buf[0].t<cut) buf.shift()`), the play queue `_q` capped at 4, table updates replace `innerHTML`; no unbounded arrays found. I did not run a real browser for 6 hours, so this is a code-reading result.

## What 2.0 does better than 1.0 (evidence)
- p99 engine round CPU 28.7 ms -> 11-12.9 ms (same median ~6 ms); background parquet thread and one jsonl flush per round (tape.py:53-77, 120-140). Forced expiry-regime window: p99 12.4 ms, max 16.8 ms over 3,000 rounds.
- GC collections 921 -> 27 per 2,000 rounds (30x), total GC time 791 -> 147 ms; flat tracked-object count over a full day.
- Delivery: SSE delivers 99.8-100% of rounds to 3 tabs at ~30 ms vs polling's 83-86% at ~130 ms, with payload 3 KB/round instead of 18 KB/poll, and no impact on engine time.
- Replay no longer needs 11-34 GB (`ReplaySource._iter_rows` streams): my full-day replay peaked at 1.28 GB RSS including my own reader overhead, whereas the 1.0 `ReplaySource._read_rows` builds a list of all rows (feed.py in `/home/claude/vajra2`).

## What 2.0 does worse than 1.0 (evidence)
- level_tape.jsonl +28% and decision_tape +3% (wider rows), so disk is 8.6 vs 8.4 GB/day; no rotation added.
- `tools/latency_bench.py` ships with a SyntaxError, so the performance claims in the CHANGELOG cannot be re-run from the shipped tree; the "~8 ms" GC figure is 19.5 ms in my measurement.
- Some per-round work added by the new features (regime, feed health, sounds, alerts) is not visible as extra cost in the medians (6.3-6.4 ms vs 5.9-6.25 ms, within noise), so there is no regression, but p50 is also not faster.

## Tests / checks I ran
- `python3 -B bench.py /home/claude/vajra2|/home/claude/teardown/vajra2 2026-10-15 10:00 3000 <abs out>.json --sample 1000 [--gc live] [--expiry 2026-10-19] [--nulljsonl]` (results `lat_v1.json`, `lat_v1_nulljsonl.json`, `lat_v2.json`, `lat_v2_gclive.json`, `lat_v2_expiry_gclive.json` in `/home/claude/teardown/scratch/17/`).
- GC pause comparison with thread-CPU timing: `gc_v1_default.json`, `gc_v2_default.json`, `gc_v2_live.json` (2,000 rounds each).
- Full-day accelerated simulation, 2.0, 79,747 rounds, `--gc live --sample 2500 --nulljsonl` -> `full_v2.json` + `full_v2.json.progress`.
- Spike re-test: `bench.py ... 11:30 600` -> max 21.5 ms CPU (spike not reproducible).
- SSE and polling with 3 clients and 0 clients at live pace: `sse_bench.py` (`sse_v2_3.json`, `sse_v1_3.json`, `sse_v2_0.json`), including offline encode micro-benchmarks for `_clean + json.dumps` on the real final state.
- Memory-source controls: `pqmem_ctl.py` (reader only: grows), `pqmem2.py` (ParquetWriter only: flat); `conv.py` (256 tick rows -> Arrow table: ~2.7 ms CPU).
- cProfile of 400 engine rounds (`prof2.out`).
- AST parse of `tools/latency_bench.py` (SyntaxError). Read in full: `tape.py`, `alerts.py`, `feed_health.py`, `orchestrator.py`, `app.py`, `dashboard.py` (server side and JS growth patterns), `feed.py` (live source, VWAPTracker), `mathx.py`, `marketdata.py`, `engine/*.py`, `breeze.py` tick handling.
- Not done: no broker, so the live snapshot-build path and Windows file I/O were not measured; no real-browser 6-hour soak.
