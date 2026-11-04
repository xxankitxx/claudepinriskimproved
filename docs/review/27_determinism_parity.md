# 27 — Determinism & replay-vs-live parity (§11.5): findings

Scope: every remaining source of divergence between a live day and its replay. Scripts are in
`/home/claude/vajra2_review/scratch/27/`: `parity10.py`, `coldstart.py`, `presence.py`, `dups.py`,
`window_replay.py`, `parity.py` (the proposed tool, working).
All measurements are on `tapes/2026-10-16`, read through pyarrow row-groups into a null writer. No full-day replay was run.

## 0. Headline result (empirical)

Replaying the first 30 min of 2026-10-16 (11:11:02 to 11:41:02, 7,179 rounds, includes the first non-zero bias at 11:24:27 and 118 REVERSAL_WATCH events) through the real `Orchestrator`:

| compared field | recorded vs replayed |
|---|---|
| `raw_bias`, `bias_ewma`, `decision`, `reason_code`, 3 flags | **bit-identical on all 7,179 shared rows** (max abs diff 0.0) |
| NIFTY `upper_level`/`lower_level` | identical on 7,179/7,179 rows |
| REVERSAL_WATCH events | 118 = 118 |
| decision_tape row set | 4 recorded rows have NO replay counterpart (ts 11:11:02.302/.601/.851, 11:11:03.101) |
| decision `instruments` vector | differs on **every** row (24 entries recorded vs fewer replayed; see F1) |
| non-NIFTY level rows | 19,907 of 154,919 differ (BANKNIFTY 7,179 = 100%, IDFCFIRSTB 5,269, BSE 4,570, TCS 2,881) |

**First divergence: ts 1792129263352 (11:11:03.352), decision_tape `instruments` vector, 24 recorded vs 7 replayed entries.**
There is no scalar (bias/decision) divergence in the first 30 min. The first state-bearing divergence is
earlier: BANKNIFTY's level row at 11:11:03.352 has basis 123.85 recorded vs 122.25 replayed, and upper 56201.15 vs 56202.75 (F1b).

Also measured: a cold engine started mid-day (the live-restart case) at 12:41:02 never equals the warm recorded run:
`bias_ewma` differs on 7,071 of 7,071 rows (first row 0.0092, max 0.0127 at 12:43:47), 4,527 `raw_bias` mismatches,
NIFTY levels differ by 0.9 pts from the first row, and BHARTIARTL/BANKBARODA state mismatches total 2,621/2,410 rows (F2).
Decision words did not change in that window, but 0.0127 is 5% of `bias_threshold` 0.25.

So the "replay is deterministic" claim holds for replay-vs-replay and for scalar decisions on a single uninterrupted session.
It does not hold for the full recorded artifact set (tape row sets, vector, sister levels, events), nor for any day with restarts.

---

## 1. Correctness defects (most severe first)

### [HIGH] F1. tick_tape is written downstream of the engine's own filter, so replay input != live input  (orchestrator.py:299-316, 423-449; decision_maker.py:114-120)  certainty: confirmed-by-running
- Mechanism: `tape.append("tick_tape", ...)` runs only after `compute_basis` returns ok (orchestrator.py:308). Any instrument whose ATM quote is missing, or whose step is wrong, never reaches the tape. Live still holds it in `snapshots` and gives it a NEUTRAL signal.
- Consequences measured:
  - **(a) Vector divergence.** Live decision rows contain NEUTRAL placeholder entries for gapped instruments; replay rows do not. Rows whose vector differs from the tick-tape instrument set:
    - 10-14: 88
    - 10-15: 108
    - 10-16: 59,019 (ITC is absent from the tick tape until its step was corrected at ~15:xx; `presence.py`).
  - **(b) Sister state shifted by early rounds.** `process_round` returns early on a NIFTY-less round (orchestrator.py:424). In live, NIFTY was in `snapshots` with `basis` failing, so sisters/stocks were processed; the tape has no NIFTY row, so replay skips those rounds. BANKNIFTY anchored at 11:11:02.851 live but at 11:11:03.352 in replay. Sister freshness clocks, basis EWMAs and validators then run 0.5 s offset forever. That is why 100% of BANKNIFTY level rows differ (basis 123.85 vs 122.25).
  - **(c) Rows with no tick at all.** Live writes a decision row for rounds where nothing survived the filter or NIFTY basis failed. Replay cannot regenerate those rows (4 on 10-16, 1-2 on each other day).
  - **(d) What-if replays cannot test fixes.** Because the tape holds only post-filter instruments, a strike-step or ATM-selection fix can never be validated on a recorded day. The data that would show it was never taped.
- Fix: move the tick append before `compute_basis` and tape every snapshot instrument, with the basis columns nullable. Add a `round_tape` row per round (`rid`, `ts`, instruments present, instruments gapped with reason, `boot_seq`). Make `ReplaySource` yield empty rounds so `FEED_GAP` and latch behaviour replays. Separately, never let a round with NIFTY-basis-fail skip sister processing differently in live and replay: have a single `process_round(snapshots, gaps)` signature for both.

### [HIGH] F2. Live restarts reset all engine state; replay of the concatenated tape does not  (app.py:129, orchestrator.py:121-198, execution.py:100-110)  certainty: confirmed-by-running (cold-start analogue) + by reading
- A restart builds a fresh `Orchestrator`. Reset on restart:
  - `bias_ewma` (3 min half-life), `BasisMonitor` EWMA and rolling std (30 samples), `ContinuationValidator`, the `RollingStats` volume activity window
  - freshness clocks (NIFTY re-anchors immediately; first round is "interval" off the seed)
  - cooldowns
  - Executor capital back to 100,000
  - `_paper_seq` (order ids restart at `PAPER-1`)
  - any open position (forgotten with no EXIT row)
- Replay (`ReplaySource`) merges all sessions of a date into one stream and runs one engine. Parity is therefore impossible by construction on any day with a restart. The 10-16 STARTUP_CONFIG count is 4, but the 3 earlier boots failed to connect and wrote no ticks, so none of the 3 recorded days exercises this. The case is untested, not proven safe.
- Measured (`coldstart.py 90 30`): cold start at 12:41:02 vs recorded warm run. See §0: `bias_ewma` differs on 100% of rows, up to 0.0127; NIFTY level shifted 0.9 pts; hundreds to thousands of per-instrument state mismatches.
- A live restart while holding a position loses the position, and the replay then books an EXIT that live never did. A restart also resets starting capital, so the P&L ledger is not continuous.
- The parquet fallback in `ReplaySource` (feed.py:73-79) reads only `tick_tape.parquet`. After a same-day restart the earlier sessions are in `.bak` files (tape.py:68-78), so jsonl-replay and parquet-replay give different row sets. The jsonl preferred branch hides this only as long as the sidecar exists; the recorded tapes have no jsonl.
- Fix: see §2 P1 (boot boundaries as events; replay resets at boundaries, or persist/restore engine state). Do one or the other; today live is "reset" and replay is "continuous".

### [HIGH] F3. Replay uses today's config/weights, not the config the day ran with; runtime-resolved steps are never taped  (replay.py:76-79, app.py:62-67, feed.py:244-278, 335-370)  certainty: confirmed-by-running
- `replay_session` loads the current `vajra_config.yaml` and `nifty_weights.csv`. The STARTUP_CONFIG event is written before the feed resolves anything (app.py:62-67, called at app.py:112), so its `strike_step` contains only the 3 index steps plus `resolve_from_chain`. The ~20 stock steps resolved by `get_option_chain_quotes`, and the later corrections (STEP_RESOLVED_LATE, STRIKE_STEP_CORRECTED events, 15 of them on 10-16), exist only as ANOMALY events that nothing consumes.
- Replay falls back to `snap.inferred_step()`. IDFCFIRSTB at 11:11:46: recorded K_CE/K_PE 77/79, replayed 76/80.
- Weights: fixing TCS in the CSV silently changes the replay of every historical day. That is wanted for tuning but must be explicit and diffed. Today a replay is a mix of "recorded input" and "current code/config" with no record of which.
- Fix: P1 manifest below. `replay.py --config-from-tape` becomes the default and `--set` the explicit override. Print a config-diff banner.

### [MEDIUM] F4. `app.py --mode replay` is a different, weaker replay than `replay.py`  (app.py:100-108, 111-112, 188-190)  certainty: confirmed-by-reading
- It re-resolves expiry from the current calendar (`cal.resolve(session_date)`) instead of the taped EXPIRY_RESOLVED, so a holiday-list edit flips `basis_mode`/`is_expiry_day` of a historical day. `replay.py` reads the event.
- It writes STARTUP_CONFIG/EXPIRY_RESOLVED with wall-clock `_now_ms_ist()` into the recorded folder (already known: the folder is shared).
- `session_date` defaults to wall-clock today.
- Fix: delete the `--mode replay` branch in `app.py` and delegate to `replay.py`.

### [MEDIUM] F5. `replay.py` fallback crashes when `expiry.holidays_verified: true`  (replay.py:53-56)  certainty: confirmed-by-running
- Config line 174 has `holidays_verified: true`. `replay.py` iterates every key starting with `holidays_` and calls `parse_holidays(True)`, giving `TypeError: 'bool' object is not iterable` (reproduced with an event-less tape dir). `app.py:25-30` already has the `isinstance(val, (list, tuple))` guard; `replay.py` was not patched.
- Hits when the tape has no EXPIRY_RESOLVED (corrupt or trimmed event tape). Also `shifted_from_holiday=False` is hard-coded in the event path (replay.py:41), so HOLIDAY_DECISION is lost.
- Fix: import and use `app.build_calendar(cfg)`.

### [MEDIUM] F6. Wall-clock timestamps leak into replay output  (tape.py:106, orchestrator.py:119-123, 746-770)  certainty: confirmed-by-reading
- `TapeWriter.event(ts_ms=None)` uses `now_ms()`. Callers with no ts: the MISSING_WEIGHT ANOMALY in `__init__`, and the two SESSION_ERROR seed events in `run()`. In replay these events carry the replay machine's clock (today, not the session date), so the replayed event_tape has out-of-session timestamps. A time-range diff or a per-day split of the event tape then misattributes them.
- Fix: an injected `clock` on `TapeWriter`/`Orchestrator`. Live passes wall-clock, replay passes `lambda: current_round_ts`. Forbid `ts_ms=None` in `event()` (raise) once the clock exists.

### [MEDIUM] F7. The pre-open seed is not replayed and leaves an out-of-order row in level_tape  (orchestrator.py:681-690, feed.py:178-230)  certainty: confirmed-by-running
- Live seeds NIFTY from the prior tape and writes a `prev_close_seed` level row stamped with the previous day's close ts. `level_tape` first row is 15:29:59.759 (the prior day), followed by 11:11:02.851. Any "ts is monotonic" assumption or range query breaks. Replay has no `prev_close_snapshot`, so the first NIFTY trigger reads `open` vs `interval` recorded (verified), and the seed row is absent.
- The seed snapshot itself is not taped; only its derived band is. The seed needs `tick_tape.jsonl` of the prior day (feed.py:188), which the archived tapes do not have, so the live seed silently falls to the stale June file.
- Fix: tape the seed snapshot and its source (`SEED` event with the NIFTY row), let replay consume it, and stamp the level row with the session start ts, with the true seed time in a payload field.

### [MEDIUM] F8. ReplaySource materialises and sorts the whole tape  (feed.py:70-96)  certainty: confirmed-by-reading
- `_read_rows` returns a list of dicts for every tick row (1.9M rows on 10-15), then sorts them. This is why a replay takes minutes and gigabytes. A 2-column scan of the 10-15 tick tape in plain pydict form OOM-killed one of my analysis runs (exit 137) on this 8 GB box.
- Verified: in all three days ts is monotonically non-decreasing in file order and there are 0 duplicate `(ts, instrument)` pairs (`dups.py`). The sort is unnecessary and risky because it hides a non-monotonic tape.
- Fix: stream row-groups with an assertion that `ts` never decreases and group by `rid`. My `parity10.py` and `coldstart.py` do this.

### [LOW] F9. Round grouping by exact ts; clock step-backs  (feed.py:85-96, 387)  certainty: plausible
- Live stamps one `time.time_ns()` per round. Grouping by exact ts works because all instruments in a round share it (0 duplicates verified). A backwards NTP step would make live process in arrival order while replay re-sorts by ts, reordering rounds; `TimeEWMA` clamps negative dt to 0, so live and replay would then disagree silently.
- Fix: `rid` (monotonic round counter) in every tape row; replay groups by `rid`, asserts `ts` non-decreasing, and tapes an `ANOMALY` for a backwards step in live.

### [LOW] F10. Dashboard market clock uses the browser's wall-clock and local timezone  (dashboard.py:478-480, 484, 661-663, 707)  certainty: confirmed-by-reading
- `marketPhase()` and `todHM` use `new Date().getHours()`, not IST. If the browser is not on IST (VPN, a UTC laptop, a remote VM), the `snd-muted` pill, the watchdog `endOfDay` test (>= 15:29) and the `close in` countdown are wrong. In an evening replay `marketPhase()` is 'closed', so the muted-while-open warning never fires; harmless but untested.
- Fix: have the server publish `market_phase` and `clock_ist` derived from `round_ts` (the engine's clock) and use them everywhere. Never compute the phase from the browser. Or format with `Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata'})` as a minimum.

### [LOW] F11. DecisionMaker bloc aggregation is positional  (decision_maker.py:114-120)  certainty: confirmed-by-reading
- `bloc_vals` is built from whichever of BANKNIFTY/FINNIFTY are present in `signals`, then `bank = bloc_vals[0]`, `fin = bloc_vals[1]`. If BANKNIFTY is absent and FINNIFTY present (which a replay of the current post-filter tape can produce, but live cannot because it inserts NEUTRAL), `bank` becomes FINNIFTY's value. Numerically harmless today (the missing one is NEUTRAL and the sum is symmetric), but it breaks `agreement_scale` the day either value is non-zero and the other member is missing.
- Not seen in the data: BANKNIFTY and FINNIFTY were both present in all but 1-7 rounds per day, and only FINNIFTY was missing there.
- Fix: look up the two named members, defaulting to 0.0.

### [LOW] F12. Order ids are session-local  (execution.py:~84-88)  certainty: confirmed-by-reading
- `PAPER-n` restarts at 1 after a restart, so `order_id` is not unique per day and cannot key a parity diff. `parity.py` keys on `(ts, action, strike, right)` instead.
- Fix: `PAPER-<boot_seq>-<n>`.

### Observation for the lead (not a defect I can prove)
`tapes/2026-10-16/tick_tape.parquet` has mtime 20:30 while every sibling file is 20:17. If any `--mode replay` wrote into the recorded folder, the "recorded" tick tape may not be pristine. I verified tick-tape internal consistency (no duplicates, monotonic) and that decision rows match the 30-min replay, but a checksum comparison against the original copy would settle it.

---

## 2. Improvements / design proposals (ranked): the v2 parity contract

**P1. Taped run manifest + boot boundaries (fixes F2, F3, F6, F7).**
At every engine start write, as the first event, a `RUN_MANIFEST` that carries everything the decision path reads:
```
{run_id, boot_seq,                    # boot_seq = 0,1,2.. per date; written into every tape row as `boot`
 git_sha, python, pyarrow versions,
 config_resolved,                     # FULL yaml after defaults, minus secrets
 config_sha256, weights_table,        # the weights actually used incl. missing-weight list
 expiry_resolution,                   # full ExpiryResolution incl. shifted_from_holiday
 strike_steps,                        # step per instrument AT START, source per instrument (config|chain|inferred)
 seed: {source, snapshot_row}}        # the actual prev-close NIFTY row, not just its band
```
Anything that changes decision inputs later becomes a `CONFIG_PATCH` event `{round_rid, key, old, new, reason}`. That means `STEP_RESOLVED_LATE` and `STRIKE_STEP_CORRECTED` stop being ANOMALY narration and become inputs. Replay applies the patch before processing round `rid`.

**P2. Replay consumes the manifest and replicates boundaries.**
- `replay.py` default: build the config from the manifest (`--config-from-tape`); `--set` overrides are explicit and printed as a diff.
- At each `boot_seq` change replay constructs a fresh `Orchestrator` (cold reset), reproducing live exactly. Add `--continuous` to deliberately measure what restarts cost.
- Alternative that makes restarts harmless instead of replicated: checkpoint engine state (EWMAs, validators, freshness clocks, executor position/capital/cooldowns) to `state/<date>.json` every ~10 s and on exit, restore on boot, and tape a `STATE_RESTORED` event. This also fixes the position-loss risk in F2. I recommend it for live. The manifest/boundary approach is still needed for tapes recorded before it exists.

**P3. Tape the raw feed, not the engine's survivors (F1).**
`tick_tape` gets one row per instrument per round before any filter, plus a `round_tape` (`rid`, `ts`, `boot`, instruments present, gaps with reason). Basis/ATM columns become nullable outputs. Replay can then regenerate gaps, FEED_GAP latches and the 4 "empty" decision rows, and can test step fixes.

**P4. Injected clock.**
`Clock` protocol: `LiveClock.now_ms()` and `ReplayClock` returning the current `round_ts`. Pass it to `TapeWriter`, `Orchestrator`, `app.py`. Ban `time.time*`, `datetime.now` in `vajra/` outside `LiveClock` (a pytest grep check). Remove `ts_ms=None` from `TapeWriter.event`.

**P5. Streaming `ReplaySource`** with monotonic assertion and a `--from/--to` time window (lets CI run a 10-min slice in seconds; my harness is the prototype). Fixes F8, F9.

**P6. Per-round state digest.**
Add `state_hash` to `decision_tape`, a short hash of `(bias_ewma, raw_bias, NIFTY band, per-instrument state/dir/strength, executor position/capital)`. `parity.py` can then bisect the first diverging round in O(rounds) without reading the vectors, and a live operator can compare two machines' digests.

**P7. Delete `app.py --mode replay`; one replay path (F4, F5).**

### `parity.py` (working prototype: `scratch/27/parity.py`)
`parity.py REC_DIR REP_DIR [--from-ts] [--to-ts] [--strict-vector]`, read-only, exits 1 on any mismatch. Contract it enforces:
- `decision_tape` by `ts`:
  - set difference both ways
  - bit-equality for `raw_bias`, `bias_ewma`, `bias_threshold`, `routing_mode`, the 3 flags, `decision`, `reason_code`
  - `instruments` vector compared after dropping NEUTRAL/0/0 placeholders, unless `--strict-vector`
- `order_tape` by `(ts, action, strike, right)`: set difference both ways; `qty`, `fill_price`, `pnl_realized`, `exit_reason` equal. `order_id` is excluded (F12).
- `level_tape` NIFTY by `ts`: `upper_level`, `lower_level`.
- Tested on recorded 10-16 vs a 20-min `TapeWriter` replay: reports 4 recorded-only decision rows and 0 mismatches elsewhere; with `--strict-vector` it reports 4,772 vector mismatches. That is the F1 signature.

Add to v2: `level_tape` for all instruments, `event_tape` keyed by `(ts, type, payload-hash)` restricted to the session window, per-`boot` splits, and the `state_hash` bisect (P6).

---

## 3. Recommendations specific to the two operator asks

**A (non-expiry levels).** Parity requirement: every new knob (non-expiry band narrowing, sizing, last-entry cutoff, spot/implied-future plausibility check, frozen-spot detector) must be read from the manifest config. Any adaptive parameter that is learned during the day (e.g. a band width scaled by a running statistic) must be a function of the tape only and must reset at `boot` the same way live does. The lead's proposals will be measured on replays of recorded days: after P3, replays can include instruments that live gapped and so are comparable to live. Before P3, sister/stock-derived confluence in a replay differs from live by the F1 amounts (19,907 of 154,919 sister level rows in 30 min).

**B (sounds).** Sounds are a pure function of `/api/state`. Drive them from server-emitted edge events carrying a monotonic `event_id` (state replayed from the tape yields identical sounds), not from browser-side comparisons of consecutive polls (`_lastSide`, `_lastEntry`, `_breached`) which depend on poll timing and page reload. Use the server's `market_phase` for the "muted during market hours" pill (F10). This also makes the sound policy testable offline against a replay.

---

## 4. Things that are fine and should NOT be changed

- **All timing in the decision path is tape-ts driven**: `TimeEWMA` decays on tape-ts deltas (mathx.py:66-86), `basis_signal_weight`, `hhmm_reached`, `FreshnessEngine`, `ContinuationValidator`, cooldowns and `Executor` all take `ts_ms`. `timeutil.ist_datetime` builds IST explicitly, so the host timezone cannot change a decision. Confirmed: 7,179 bit-identical scalar rows.
- Wall-clock audit, every remaining use:
  - `tape.now_ms` as `_start_ms` (`.bak` name only, not decision path)
  - `feed.py:183, 242, 387` (live feed stamps, seed date; feed-side)
  - `app._now_ms_ist` (startup events; F4/F6)
  - dashboard JS (F10)
  - `breeze.py` and `credentials.py` (not decision path)
  - No `random`/`uuid`/`hash()`/`os.environ` use in the decision path. `set` use is membership-only (no iteration order dependence).
- Instrument order is fixed by `sorted(snapshots)` and `sorted(signals.items())`; `raw_bias` float-sum order is identical live vs replay (the comment at orchestrator.py:454-457 is right). `all_strikes_sorted`/`strikes_for` sort, so dict insertion order (live: -n..+n; replay: sorted) does not matter for `locate_pin`, `inferred_step`, or the integer volume sum.
- `round_to` uses Decimal HALF_UP (mathx.py:27-44); keep it.
- JSON/parquet float round-trip is exact (repr/float64), so parquet vs jsonl readers give identical values for rows both contain.
- The rule "tape rows written before band compute, band rows every round" (`level_tape` every round) is what made the cold-start and anchor-shift analysis possible; keep level rows per round.

---

## 5. Tests I would add

1. `test_replay_slice_golden`: replay the first 10 min of a committed 10-min slice of a recorded day (about 3 MB parquet), assert `parity.py` exit 0 under default contract. Run in CI, a few seconds.
2. `test_parity_strict_vector_after_P3`: after P3, the same slice must pass with `--strict-vector` (zero ghosts).
3. `test_restart_boundary_replay`: build a synthetic two-boot tape (second boot starts mid-session), live-run the two boots as two Orchestrators, replay the concatenated tape, assert parity exit 0. Today this fails (F2). Add `--continuous` as a separate documented non-parity mode.
4. `test_config_patch_replay`: feed a tape containing `STRIKE_STEP_CORRECTED` as `CONFIG_PATCH` and assert replay uses the old step before `rid` and the new after.
5. `test_no_wallclock_in_decision_path`: grep `vajra/` for `time.time`, `datetime.now`, `perf_counter` outside `clock.py`/`feed.py` live source; fail on a new hit. Plus run the replay with `time.time` monkeypatched to raise.
6. `test_event_ts_never_none`: `TapeWriter.event` without a ts raises.
7. `test_replay_expiry_from_tape_and_fallback`: `holidays_verified: true` with an event-less tape must not raise (F5); `app` and `replay` agree on `ExpiryResolution`.
8. `test_replay_source_monotonic`: a tape with a backward ts raises `NonMonotonicTape`; `rid` grouping works with equal ts.
9. `test_decision_maker_bloc_named`: BANKNIFTY missing + FINNIFTY non-zero gives the same `agreement_scale` as BANKNIFTY NEUTRAL + FINNIFTY non-zero (F11).
10. `test_dashboard_phase_from_server`: `/api/state` publishes `market_phase` from `round_ts` in IST, and the JS does not call `new Date()` for the phase (F10).
11. Determinism regression: replay the slice twice, with `PYTHONHASHSEED` 0 and 1, and diff `state_hash` columns (P6).
