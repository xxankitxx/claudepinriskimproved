# 15 — Failure-injection teardown: teardown
Verdict: 2.0 handles faults better than 1.0. It fixes the two failures that cost money (stops skipped on rounds where the band or basis failed, and HTTP 500 on NaN) and one replay crash. But both versions share four failure modes that still end the day early or misreport the 15:25 flatten. It is safe to run on Monday only if the operator watches for the cases listed below.
Score (your area): 1.0 = 4/10, 2.0 = 6/10

Method: `/home/claude/teardown/scratch/15/harness.py <root> <scenario>` runs the real `Orchestrator.run()` of each version over **recorded 15 Oct 09:57:45–10:01:15 rounds** (all 24 instruments, ~840 rounds), with one fault injected. The 15:25 scenarios use the same clean rounds re-stamped +5h25m, so that 09:59 becomes 15:24, because the real 15:15+ cash feed is garbage on all three days. A paper position (ATM 22850 CE, 780 qty @124.70) is opened at 09:58:00 through the engine's own `executor.enter` + `_emit_orders`, so every mid-trade fault hits the same position in both versions. Where noted, a `crash_held` variant drives the held option to −20% during the fault window to test whether the stop still runs.

After every run the harness checks: whether `run()` raised, `SESSION_ERROR`/`SHUTDOWN` events, the order_tape (jsonl and parquet, row counts, ts ordering), `executor.position`, the dashboard `latest_state.position`, and serialisation through the real `/api/state` endpoint (`build_app(orch)`; ~100 checks per fault run). `synth.py` repeats the key faults on the synthetic expiry-day feed, 14:50–15:30, which has real trades across 15:25 (expiry-day / Monday code path). Raw results: `scratch/15/results.jsonl`.

## Results matrix (forced position; "stop" = held option at −20% during fault)
| Fault | 1.0 | 2.0 |
|---|---|---|
| NIFTY missing 4 / 40 / 480 rounds | survives, 1 FEED_GAP, position held unmanaged in gap | same |
| ATM PE missing 90 s + stop | **stop skipped for 359 rounds** at −20% (luckily recovered, +14,430) | **SL_HIT at 09:59:00.086, −19,492** |
| NIFTY spot NaN 60 s + stop | **stop skipped 240 rounds** | **SL_HIT on first round** |
| NIFTY spot inf | guarded (FEED_GAP NONFINITE_SPOT) | same |
| exception in compute_basis (NIFTY) 60 s + stop | stop skipped 240 rounds | SL_HIT first round |
| exception in compute_band | caught; no recompute was due mid-trade (not informative) | same |
| held quote missing / zero / NaN 120 s | HOLD (no phantom 0.0 exit) | same |
| NaN on ATM CE 5 s | 20× INSTRUMENT_ERROR (1 per round, latch broken) + NIFTY basis EWMA NaN forever | 1 INSTRUMENT_ERROR; **EWMA NaN forever too** |
| VIX None (replay row) | **session ends** (TypeError in `feed._snapshot_from_row`), flattened 09:58:59 | survives |
| VIX NaN (live path), synthetic | **/api/state fails 162/162 rounds** | 0 failures |
| exception thrown by the feed | session ends, SESSION_ERROR, flatten at last round | same |
| exception in `_compute_alarm` (dashboard builder), once | **session ends for the day** | **same** |
| empty rounds 120 s | survives, 1 FEED_GAP | same |
| round ts stepped 5 s backwards | accepted silently; decision_tape non-monotonic | same |
| duplicate round timestamps 30 s | benign | same |
| 2-min gap (no rounds) mid-trade | survives, manages on resume | same |
| 2-min frozen cache mid-trade | no signal | FEED_GAP feed_sanity; entries paused |
| 2-min gap across 15:25 | flatten 15:26:00.086 @144.55 (real price) | same |
| 2-min frozen cache across 15:25 | **flatten 15:25:00 @126.70 = 60-s-old frozen price** | **same** (gate tripped, fill not flagged) |
| feed ends 15:24:30 / NIFTY missing to close | EXIT END_OF_SESSION stamped 15:24:29.924; dashboard still OPEN | same |
| held quote missing to close | REJECT NO_QUOTE_AT_FLATTEN, left open, no alarm | same, plus 1 FEED_GAP manage_quote |
| disk full: jsonl sidecars only | **session dies** (OSError escapes run) | survives, but jsonl silently stops and parquet loses rows |
| disk full: parquet only | survives; parquet silently truncated, no event | same |
| disk full: whole disk | **dies: no SESSION_ERROR, no SHUTDOWN, tape = ENTRY only** | **same** |

## Findings (most severe first)

### [HIGH] One exception anywhere outside the per-instrument guard ends the whole trading day (orchestrator.py:528-820, 950-956) — reproduced (both)
- Scenario: only `_process_instrument` is wrapped (`orchestrator.py:570-581`). `decision.evaluate`, the cooldown/recompute block, `_approach_entry`, `executor.enter/manage`, `_emit_orders`, `_compute_alarm` and `_update_view` are not. Any throw there propagates to `run()`. `run()` writes SESSION_ERROR, `finalize()` flattens at the last round, and the loop **never resumes**. In live mode `app.run` then sets `server.should_exit`, so the cockpit disappears as well.
- Evidence: `harness.py <root> exc_view` raises one KeyError inside `_compute_alarm` at 09:59. Both versions: `SESSION_ERROR run_loop KeyError('injected dashboard-state failure')`, EXIT END_OF_SESSION at 09:58:59.835 @126.70, decision_tape stops at 299 rows (base run: 837). A display-only bug becomes "no trading for the rest of the day".
- Fix: split `process_round` into guarded stages. Run (a) the engine and decision stage in try/except; on failure set gate `ENGINE_ERROR`. Run (b) position management in its **own** try/except, every round NIFTY is present. Run (c) `_compute_alarm`/`_update_view` in try/except that keeps the previous `latest_state` plus an error banner. Emit an edge-latched `ANOMALY ROUND_ERROR`. `run()` should exit only on `stop()`, or after N consecutive failed rounds.

### [HIGH] Disk full kills the session mid-trade with no SHUTDOWN, and the tape shows an orphan ENTRY (tape.py:154-170, orchestrator.py:950-966) — reproduced (both)
- Scenario: D:\ fills up (a multi-GB tick jsonl per day makes this realistic on the operator box). `diskfull_all` points every jsonl handle at `/dev/full` and makes the parquet writer raise at 09:59. Both versions: `run()` raises `OSError: [Errno 28]`. The event write inside the `except` handler raises again, then `finalize()` → `_emit_orders` raises. Result: no SESSION_ERROR, no SHUTDOWN, order_tape = ENTRY only. `executor.position` is already flat in memory, because `_close` mutates before the append fails. The dashboard's last state still says OPEN, and the process exits.
- The operator, who mirrors the trade by hand, loses the cockpit with a live position and no exit instruction.
- 2.0 nuance (`diskfull_jsonl`, event tape still writable): 2.0 survives. But `end_round()` swallows the flush errors (`tape.py:157-161`), so the "crash-safe" sidecar silently stops at 298 decision rows while parquet runs to 837, and ~20% of tick rows are lost from both (15,796 vs 20,088). The only trace is 91 `INSTRUMENT_ERROR OSError` anomalies.
- Fix: catch `OSError` in `TapeWriter.append/event/end_round` → switch to degraded mode (one stderr line, a `latest_state["tape_error"]` red banner, keep a bounded in-memory ring) and **never raise into the engine**. Keep managing and flattening the position. Check free space at startup and every few minutes.

### [HIGH] A frozen feed cache across 15:25 flattens at a stale price and books it as a normal EXPIRY_FLATTEN (execution.py:221-228, feed.py:395-421) — reproduced (both)
- Scenario: in live mode `snapshot_raw()` returns the last cached tick for every key. If the websocket stalls, rounds keep arriving with frozen option quotes, because nothing stores tick age (`breeze.py:247-260`). `outage_frozen_1525` (frozen 15:24:00–15:26:00): both versions flatten at 15:25:00.228 @126.70, the frozen 15:24:00 price. The true post-stall price was 144.55 (see `outage_gap_1525`): a 14% fill error, +1,560 booked vs +15,483 real. 2.0's feed-sanity gate trips (`FEED_GAP feed_sanity`), but it only blocks entries and re-anchors. The exit row carries no stale flag. The same applies mid-trade: the stops cannot fire on frozen prices.
- Fix: store a receive time per key in `BreezeWrapper._on_ticks`. In `_build_snapshot`, drop option quotes older than ~5 s, so that `manage()` HOLDs and retries. At 15:25 with only stale quotes, emit `ANOMALY STALE_FLATTEN` plus a dashboard alarm ("flatten manually"), or record `quote_age_ms` on the order row.

### [MEDIUM] When NIFTY never returns before the close, finalize books a back-dated EXIT and the dashboard keeps showing OPEN (orchestrator.py:954-966) — reproduced (both)
- `nifty_miss_to_close` (NIFTY absent from 15:24:30): no 15:25 flatten happens. At session end `finalize(*last_good)` books `END_OF_SESSION` **stamped 15:24:29.924** at the 15:24:29 price, written after later events (event_tape is out of order: SHUTDOWN ts < FEED_GAP ts). `latest_state` is never refreshed after `finalize`: `dash_pos_open=True` in every END_OF_SESSION run. Synthetic expiry day: 5,460 qty CE "exited" at 15:19:45 @98.45 when the 15:25 price was 152.96.
- `held_miss_to_close`: correct REJECT `NO_QUOTE_AT_FLATTEN`, position left open, but no ANOMALY/SESSION_ERROR and no sound.
- Fix: in `finalize`, stamp with the final round ts and record `quote_ts`. If the quote is older than ~5 s, REJECT `STALE_QUOTE_AT_FLATTEN` instead of filling. Call `_update_view` after `force_flatten`. Emit `SESSION_ERROR severity=CRITICAL "POSITION OPEN AT CLOSE"` plus a feed_alarm sound for NO_QUOTE_AT_FLATTEN.

### [MEDIUM] A NaN ATM quote permanently poisons NIFTY's basis EWMA, and feed-health reads NaN basis as healthy (basis_engine.py:51-56, basis_monitor.py:63-75, feed_health.py:72-76) — reproduced (both)
- `held_nan_ltp_atm`: 5 s of NaN on the ATM CE. `compute_basis` returns ok with basis NaN, and `BasisMonitor.update` folds it into `TimeEWMA`. In both versions 539/539 subsequent NIFTY basis_tape rows have `d_basis_ewma = NaN`, so abrupt/REVERSAL detection is dead for the rest of the session. The round itself only survives because `locate_pin` then throws (`cannot convert float NaN to integer`), which is caught as INSTRUMENT_ERROR. `abs(NaN) > 45` is False, so `FeedHealth` never trips on it. Live path: VIX can be NaN through `normalize_vix(None)`; option LTPs are filtered by `_f` and are unlikely to be NaN.
- Fix: in `compute_basis`, return `ok=False, MISSING_ATM_QUOTE` when either ATM LTP is non-finite. In `BasisMonitor`/`FeedHealth`, skip non-finite inputs.

### [MEDIUM] A parquet write failure is silent (tape.py:46-57; 1.0 tape.py:118-127) — reproduced (both)
- `diskfull_parquet`: the session survives and jsonl is complete. Parquet tick_tape stops at 6,912 of 20,088 rows and order_tape.parquet is never created. The only trace is `logging.error`, with no event and no cockpit signal. Parquet is what the manual tells the operator to keep, and it is what the 2.0 prev-close seed reads.
- Fix: on the first failure, emit `ANOMALY TAPE_WRITE_FAILED` (latched) and surface it in `latest_state`.

### [LOW] Backwards or duplicate round timestamps are accepted silently (orchestrator.py:528; feed.py:96-112) — reproduced (both)
- `ooo_ts` (one round 5 s in the past): no crash, no event, decision_tape non-monotonic (1 backstep). `dup_ts`: benign. 2.0's `ReplaySource` dropped 1.0's `rows.sort(...)` and assumes a ts-ordered tape. A hand-merged or out-of-order tape now splits rounds instead of erroring.
- Fix: in `process_round`, drop and flag `round_ts <= last_round_ts` (`ANOMALY CLOCK_BACKWARDS`).

### [LOW] Position and quote faults are mostly silent outside the event strip
- Held quote zero/NaN/missing correctly HOLDs in both versions (no phantom 0.0 exit). 2.0 emits a single latched `FEED_GAP manage_quote`; 1.0 emits it only when the band exists. Neither raises a sound or a banner while a live position is unpriced.

## What 2.0 does better than 1.0 (evidence)
- **Manages the open position on every NIFTY round, even when the band or basis failed** (`orchestrator.py:769-803` vs 1.0 `:622-649`, which needs `nifty_band`). Reproduced three ways: ATM-PE gap, NaN spot and compute_basis exception, each with the held option at −20%. 1.0 skipped the stop for 240–359 rounds (60–90 s); 2.0 hit SL_HIT on the first faulted round. Same result on synthetic expiry day with spot NaN 15:00–15:05: 1.0's trade 1 exits at 15:05 for −42,085, 2.0's at 15:01 for +36,744.
- **`/api/state` is NaN-safe** (`dashboard.py:39-53`): with VIX NaN on the synthetic feed, 1.0 failed 162/162 calls and 2.0 failed 0. All ~1,500 2.0 serialisation checks across every scenario passed.
- **Replay feed tolerates `vix: None`** (`feed.py:67`): 1.0's replay session dies at the first such row.
- **INSTRUMENT_ERROR latch fixed**: 1 event vs 479 for a persistent NaN tick.
- **Feed-sanity gate** flags a frozen or implausible NIFTY feed and blocks entries and re-anchors on it (verified in `outage_frozen_*`).
- Partial jsonl disk-full no longer kills the session (but see the silent data loss above).

## What 2.0 does worse than 1.0 (evidence)
- jsonl flush errors are now swallowed (`tape.py:157-161`), so a sidecar failure is invisible. In 1.0 it was loud, because it killed the session.
- `ReplaySource` no longer sorts rows (`feed.py:100-112`), so an unsorted tape produces split rounds instead of being ordered.
- Nothing else in this area regressed: every scenario where 1.0 survived, 2.0 also survived with identical orders.

## Tests / checks you ran
- `cd /home/claude/teardown/scratch/15 && python3 -B harness.py /home/claude/vajra2 <scen>` and `... /home/claude/teardown/vajra2 <scen>` for base_am, nifty_miss_40/480, atm_pe_miss_crash, held_nan_120, held_nan_ltp_atm, spot_nan_crash, vix_none, exc_band, exc_basis_crash, exc_feed, exc_view, empty_120, ooo_ts, dup_ts, outage_frozen_120, outage_gap_1525, outage_frozen_1525, feed_ends_1524, held_miss_to_close, nifty_miss_to_close, diskfull_jsonl, diskfull_parquet, diskfull_all (46 runs; results in `results.jsonl` and `keep.jsonl`).
- `python3 -B synth.py <root> {base,nifty_miss_1520,spot_nan,held_miss_close,vix_nan,empty,diskfull}` runs the synthetic expiry-day feed 14:50–15:30 (14 runs).
- Basis-EWMA poisoning check: read `out/{10,20}/held_nan_ltp_atm/2026-10-15/basis_tape.parquet` and found 539 NaN `d_basis_ewma` rows in each version.
- Harness caveats: the first `diskfull_parquet` version raised inside the harness (constructing a ParquetWriter on /dev/full). I fixed the injection and re-ran it; the table shows the re-run. The real 16 Oct 15:2x tape was unusable for forced entries (garbage spot gave PREMIUM_TOO_THIN), so the 15:25 scenarios use clean 15 Oct rounds re-stamped. `exc_band` was not informative, because no re-anchor was due mid-trade.
