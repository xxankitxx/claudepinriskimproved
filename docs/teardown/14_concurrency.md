# 14 — Concurrency & thread-safety: teardown
Verdict: Ready for Monday as far as concurrency goes, and slightly better than 1.0. Nothing here blocks Monday. The real risks are blocking I/O on the engine thread: Windows console writes and broker REST retries. Data races are not the problem.
Score (your area): 1.0 = 6/10, 2.0 = 7/10

## Thread inventory (2.0, live mode)
| Thread | Code | Touches shared state |
|---|---|---|
| main / engine round loop | `orchestrator.py:935` driving the `LiveBreezeSource.rounds()` generator (`feed.py:421`) | writes `orch.latest_state`, `_recent_events`, `sounds.events`, tape buffers |
| Breeze socket.io callback | `breeze.py:247 _on_ticks` | `breeze.latest` (under `_lock`), `_unmapped_warned` |
| uvicorn event loop (daemon) | `app.py:225`; SSE `dashboard.py:68-96`; `/api/state` runs in the anyio threadpool | reads `orch.latest_state` only |
| tape writer (daemon, new) | `tape.py:62 _work` | owns the row lists it is handed; `_writers` |
| ThreadPoolExecutor (transient) | `feed.py:302,348` | broker REST `option_step`; results applied on the main thread |

**Snapshot discipline holds, and I checked it in the code.** Every round, `_update_view` builds a new `latest_state` dict (`orchestrator.py:1339`) and publishes it with one reference assignment. The deques are copied on the engine thread before they are published: `list(self._recent_events)` at :1347 and `self.sounds.snapshot()` at :1398. `nifty`, `calc` and `nifty_chain` are copied too, and `level_history[-80:]` is a slice. The dashboard never touches `_recent_events` or `SoundBus.events` directly, and nothing mutates a published state dict afterwards. `_inst_last` reuses the published per-instrument dicts, but only through `{**…}` copies.

So the races the brief suspected do not exist:
- no "deque mutated during iteration";
- no `latest_state` swapped while it is being serialized: SSE holds its own reference `st` (`dashboard.py:78`);
- no tape-buffer race: `append` swaps in a new list before it enqueues the old one (`tape.py:124-126`).

`event_sink` (`orch._emit_event`) is only ever called on the engine thread, because the feed is a synchronous generator. 1.0 had the same discipline (`vajra2/vajra/orchestrator.py:1154`).

## Findings (most severe first)

### [HIGH] Blocking console writes can freeze the engine thread, and one of them runs while holding the Breeze tick lock (`orchestrator.py:943-949`, `breeze.py:286-291`) — plausible (known Windows behaviour; read in code)
- **Scenario:** the operator runs `run_vajra.ps1` in a normal Windows console, which has QuickEdit on by default. They click or drag in the console, for example to copy a number while mirroring a trade by hand. Windows then suspends every write to that console until the selection is cleared.
- **Engine thread:** in live mode it writes the heartbeat to stderr every 30 rounds (7.5 s), at `orchestrator.py:943`, so it blocks inside `print`. While it is blocked:
  - there are no rounds;
  - `executor.manage` does not run, so no −12% stop, no trail and no 15:25 flatten;
  - the dashboard keeps serving the last state, with only SSE `beat`s.
- **Breeze callback thread:** it writes to stderr while holding `self._lock` (`breeze.py:286-291`) when it meets an unmapped ws name. If that write blocks, the next `snapshot_raw()` (`breeze.py:308`) on the engine thread also blocks.
- **Same in 1.0:** heartbeat at `vajra2/vajra/orchestrator.py:796`.
- **Fix:**
  - Route the heartbeat and warnings through a `logging.handlers.QueueHandler`/`QueueListener`, so the console write happens on a throwaway thread.
  - In `_on_ticks`, only record the name under the lock (already done in the set) and do the write outside the lock.
  - In `run_vajra.ps1`, disable QuickEdit, or tell the operator not to select text in the console.

### [MEDIUM] Broker REST strike-step retries run inside the round loop and stall it (`feed.py:339-356`, called from `feed.py:454`) — reproduced from the 1.0 tapes (the code is identical in 2.0)
- **What happens:** `_step_maintenance` opens a `ThreadPoolExecutor` and waits for it inside `with`, on the engine thread, every 90 s for any stock whose step is unresolved. The first retry runs on the very first round (`_step_retry[inst]=0`).
- **Evidence** (`scratch/14/gaps.py` over `decision_tape` + `event_tape`):
  - 10-16: the round gap `11:12:30.572 -> 11:12:31.602` is **1030 ms**, the largest of the day. It lines up exactly with `STEP_RESOLVED_LATE BSE @ 11:12:30.572`.
  - 10-14: the first in-loop retry took from `13:25:45.988` to `13:26:04.159`, so the first round was delayed **about 18 s**.
- **2.0 vs 1.0:** 2.0 is better because 17 stocks now have fixed steps (`vajra_config.yaml:37-54`). Four stocks are still resolved at runtime: IDFCFIRSTB, BSE, SHRIRAMFIN and BAJAJFINSV.
- **Monday:** retries before the open fail, and those stalls happen before 09:20, so they are harmless. The danger is after the open: if one chain keeps failing, the engine stalls for seconds every 90 s, with no stop management while it waits. The broker SDK's REST calls have no timeout that VAJRA controls.
- **Fix:**
  - Submit to a long-lived executor and keep the `Future`s.
  - On later rounds, apply only the futures where `done()` is true; never block the round.
  - Or add the 4 measured steps to the yaml, which removes the path entirely.

### [MEDIUM] An unclean shutdown leaves every parquet tape of the day unreadable, with no footer (`tape.py:174-188`) — reproduced
- **What happens:** `close()` → `flush()` → `_q.join()` → `w.close()`. If anything stops this sequence, the `ParquetWriter`s are never closed. The causes include:
  - a second Ctrl+C during the close;
  - closing the console window with the X (CTRL_CLOSE_EVENT: no `finally`);
  - Task-Manager kill;
  - a power cut.
- **Command and output:**
  ```
  $ python3 -B scratch/14/closeint.py      # KeyboardInterrupt injected into close()'s _q.join()
  KeyboardInterrupt inside TapeWriter.close()
  decision_tape.parquet UNREADABLE: Parquet magic bytes not found in footer. ... | size 5176
  jsonl lines 29        # of 35 appended: rows since the last end_round() were still in the buffer
  ```
- **What survives:** the jsonl sidecars, which are flushed once per round. A single Ctrl+C was clean in a real `vajra.app --mode replay` run. `scratch/14/sigint.py` printed rc −2 and exit 5.8 s after SIGINT; `SHUTDOWN` was written; all 4 parquets were readable, with row counts equal to the jsonl.
- **Why it matters:** `prev_close_snapshot` prefers the parquet (`feed.py:196`), and the manual tells the operator to delete the multi-GB jsonl. A footer-less parquet then silently drops the seed down to the jsonl or a STALE fallback.
- **Same in 1.0:** 1.0 closes its writers the same way.
- **Fix:**
  - Roll the parquet into per-N-minute part files (each closed, so each readable).
  - Or, in the next session, rebuild the parquet from the jsonl when the footer is missing.
  - Ignore SIGINT while `close()` runs: `signal.signal(SIGINT, SIG_IGN)` in `app.run`'s `finally`.

### [LOW] The SHUTDOWN reason is always `session_end`, whether the session ended at 15:30, by Ctrl+C or by a crash (`orchestrator.py:965`) — read in code and reproduced
- **Evidence:** in the Ctrl+C run above, `event_tape` ends `{"event_type":"SHUTDOWN","payload":{"reason":"session_end"}}`. The 1.0 tape for 10-16 shows `SHUTDOWN 15:25:52 session_end` 4 minutes before 15:30, and nothing on the tape says why.
- **Fix:** pass the reason in: `KeyboardInterrupt` → `operator_interrupt`, generator exhausted → `session_close`, an exception → `run_loop_error`.

### [LOW] Every SSE client serializes the state again (`dashboard.py:84-89`) — reproduced (cost measured)
- **Cost:** each client does its own `_clean()` + `json.dumps` of a full state (19.3 KB) once a second and a hot state (2.4 KB) every round.
- **Measured** (harness, 4 Hz paced replay of the real 10-15 tape, 240 rounds, plus a simulated 700 ticks/s Breeze thread):

  | config | engine CPU/round p50 | engine wall p99 | other-thread CPU | SSE delivery p50/p99 | max |
  |---|---|---|---|---|---|
  | 0 clients | 5.6 ms | 201 ms | 5.7 % | — | — |
  | 8 SSE | 4.2 ms | 67 ms | 3.4 % | 15 / 49 ms | 66 ms |
  | 32 SSE + 4 `/api/state` pollers @4 Hz | 5.5 ms | 298 ms | 15.1 % | 29 / 228 ms | 391 ms |

  The box was at load 14-24 on 2 CPUs. The wall-time tails come from the OS, not the GIL: the 0-client baseline itself had a 7.5 s stall, with the same thing seen in another run.
- **Conclusion:** each client costs about 0.4 % of a core. With 1-3 tabs, SSE cannot starve the round loop through the GIL, and engine CPU per round did not change.
- **Fix (optional):** serialize once per `round_ts` in a shared cache and send the same bytes to every client.

### [LOW] The newest events-strip row can be mutated while the dashboard serializes it (`orchestrator.py:316-319`) — read in code
- **What happens:** run-length dedup updates `count` and `ts` in place on a dict that the previous `latest_state["events"]` already references.
- **Why it is harmless:** no keys are added, so no `RuntimeError`. At worst a client sees a count/ts pair one round apart.
- **Fix (if wanted):** replace the dict instead (`self._recent_events[-1] = {**last, ...}`).

### Checked and fine
- **SSE disconnects:** with a fake 4 Hz state, I opened 40 SSE clients that did not read for 20 s. Memory stayed bounded (RSS 37 → 39 MB) and CPU was 6 %. After they closed, CPU went back to 0.1 % with 3 threads, and `/api/health` answered in 2 ms (`scratch/14/ssefake.py`). The generators end, and uvicorn's flow control blocks stuck writers without buffering.
- **Signals:** uvicorn in a non-main thread installs no signal handlers (`uvicorn/server.py:334`), so Ctrl+C reaches the engine. `orch.run`'s `finally` → `finalize` (force_flatten + SHUTDOWN + flush) → `app.run`'s `finally` (`tape.close`, `breeze.disconnect`, `server.should_exit`). The ordering is correct.
- **15:30 close:** the generator breaks (`feed.py:428`) and `finalize` runs on `last_good`.
- **Run-loop exception:** caught (`orchestrator.py:950`); SESSION_ERROR is logged, then the session flattens.
- **Tape writer thread:**
  - It cannot deadlock. `task_done` is in a `finally`, and every row-group error is caught.
  - `close()` only joins after `_q.join()` has emptied the queue.
  - A warm `from_pylist` of 256 tick rows costs about 3 ms of CPU; the cold first call is about 290 ms (`scratch/14/conv.py`). Both now run on the tape thread instead of in the round, which is a real but modest gain.
  - The queue is unbounded, but throughput (about 3 ms per 2.7 s of rows) is far above the append rate.
- **Breeze lock:** `_on_ticks` does only cheap normalization under the lock, and the engine takes the lock once per round (`feed.py:430`). Contention is negligible.
- **`_unmapped_warned`:** `list(set)` runs on the engine thread while ticks may `add` on the callback thread. Under the GIL this is an atomic C-level copy, so it is safe on CPython 3.13 with the GIL. It would not be safe on free-threaded 3.13t.

## What 2.0 does better than 1.0 (evidence)
- **Push instead of poll:** SSE replaces 4 full `/api/state` polls a second. The hot event is 2.4 KB versus 19 KB for the full state, and delivery p50 is 15-29 ms. `_clean` stops the NaN-driven HTTP 500s (`dashboard.py:40-49`).
- **Parquet off the engine thread:** parquet conversion moved to a background thread, and jsonl is flushed once per round instead of once per row (`tape.py:53-60,130-140`).
- **GC:** `gc.freeze()` + a higher GC threshold in live mode (`app.py:159-162`) removes cyclic-GC pauses on the engine thread.
- **Fewer runtime stalls:** 17 stock steps are fixed in config, so far fewer in-loop REST stalls happen than in 1.0.
- **Typo'd event types:** an undeclared event type no longer raises inside the round loop (`tape.py:146-152`).

## What 2.0 does worse than 1.0 (evidence)
- **Nothing material in this area.** One new thread (the tape writer) adds one more thing that can be cut off at shutdown. The footer loss already existed in 1.0, and the 2.0 `close()` keeps the same ordering.

## Tests / checks you ran
- `scratch/14/gaps.py`: round-gap statistics and stall/event correlation on the 3 recorded decision tapes. Result: max gaps 1334 / 1868 / 1030 ms; the 10-16 BSE retry gap matches its event.
- `scratch/14/run.sh <label> 240 <tps> <n_sse> <n_poll>`, with `harness.py` (4 Hz paced replay of the real 10-15 tape through the 2.0 Orchestrator, the real uvicorn dashboard and a simulated Breeze tick thread) and `clients.py` (a separate process with SSE and poll clients). Runs: base0, sse0, sse8, sse32, b0, b32 (table above).
- `scratch/14/sigint.py`: real `python -m vajra.app --mode replay`, with SIGINT sent after 150 decision rows, once and then twice 0.2 s apart. Result: clean exit, SHUTDOWN written, all parquets readable.
- `scratch/14/closeint.py`: KeyboardInterrupt injected inside `TapeWriter.close()`. Result: footer-less parquet.
- `scratch/14/ssefake.py`: 40 stuck and then closed SSE clients. Result: no leak, CPU back to idle.
- `scratch/14/conv.py`, `gilbench.py`: cost of tape conversion and probe for GIL hold.
- I could not test against the real `breeze_connect` socket.io threads, because the SDK is not installed. Whether its threads are daemon threads at exit is unverified.
