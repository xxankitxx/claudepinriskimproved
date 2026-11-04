# 15 — R15 Tape & schema reviewer (vajra/tape.py, vajra/schema.py, §9): findings

Evidence scripts: `/home/claude/vajra2_review/scratch/15/` (enum_audit.py, events_scan.py, meta_scan.py, ts_integrity.py,
cost_model.py, sanity_calib.py, restart_sim.py). Everything below marked "confirmed-by-running" was run on the three
recorded days or in a temp dir (nothing under /home/claude/vajra2 was touched).

## 0. Headline numbers (measured)

| item | 10-14 | 10-15 | 10-16 |
|---|---|---|---|
| tick_tape rows (24 instr x ~4 Hz) | 660,873 | 1,913,436 | 1,379,651 |
| tick row JSON size (same `json.dumps` as `append`) | ~3.4 KB | ~3.4 KB | ~3.4 KB |
| est. jsonl per day: tick / basis / level / decision | 2.24 / 0.16 / 0.26 / 0.06 GB | 6.58 / 0.48 / 0.76 / 0.19 GB | 4.65 / 0.34 / 0.55 / 0.14 GB |
| **total jsonl sidecar per day** | **2.7 GB** | **8.0 GB** | **5.7 GB** |
| parquet tick_tape (SNAPPY, 256-row groups) | 150 MB | 286 MB | 241 MB |
| parquet all tapes | 186 MB | 391 MB | 327 MB |
| parquet row groups in tick_tape | 2,582 | 7,475 | 5,390 |

- jsonl:parquet ratio is about 23:1 for the tick tape. The jsonl sidecar costs about 25 GB per trading week for a safety net that is
  only needed on a crash day.
- NIFTY rows change on 97% of rounds (spot or chain differs from the previous row). Non-NIFTY rows are byte-identical to their
  previous row 60% of the time. `level_tape` has 1.9M rows on 10-15 of which only about 2,700 are real (re-)computes
  (99.86% are `held` duplicates). The only column that varies on a held row is `freshness_age_secs`, which is derivable.
- Benign NIFTY "same spot" runs: p99 about 1 s, max 16 s (outside the 15:15 freeze and the last 30 s of the day).
  The 15:15 freeze runs are 286 s, 265 s and 306 s. Benign |basis - 60s rolling median| max is 14.6 pts; after 15:20 it is 128 to 279 pts.

## 1. Correctness defects (most severe first)

### [HIGH] Same-day restart splits the day across files; parquet-only replay silently drops earlier sessions  (tape.py:59-77, feed.py:76-93, replay_browser.py:41)  certainty: confirmed-by-running
- Simulated two sessions on one date: the dir ends up with `tick_tape.parquet` (session 2 only, 300 rows),
  `tick_tape.parquet.<ms>.bak` (session 1, 300 rows) and `tick_tape.jsonl` (600 lines, both sessions concatenated).
- Replay reads the jsonl first (`ReplaySource._read_rows`), so a restarted day replays only while the jsonl exists. The manual
  (OPERATOR_MANUAL §5) tells the operator to delete old `tick_tape.jsonl`. After that, `ReplaySource` and `replay_browser.py`
  read `tick_tape.parquet` only, which is the last session, with no warning. The `.bak` name does not match `*.parquet` globs,
  so every analysis script ignores it too.
- Even with the jsonl, the replay is not equivalent to the live run. Live restarts reset capital, position, cooldowns, EWMAs and
  freshness. Replay runs one continuous engine across the gap, and the merged `level_tape`/`decision_tape` jsonl contain
  both sessions' rows. The determinism gate only covers one synthetic session.
- The `.bak` name uses the NEW session's `_start_ms`, and no row carries a session id, so session boundaries cannot be recovered
  from the data.
- Fix:
  - Add `session_id` (int64 start ms) as a universal column on every tape row and in the event payload `SESSION_START`.
  - Name per-session files `tick_tape.<session_id>.parquet` (or a `part-<session_id>/` dir) instead of renaming to `.bak`.
  - Make readers glob `tick_tape*.parquet` and split by `session_id`, creating a fresh Orchestrator per session in `replay.py`.

### [HIGH] A never-closed ParquetWriter leaves an unreadable file; flush is row-count-only, so `flush_interval_secs` is dead config  (tape.py:96, 131-143; vajra_config.yaml:197)  certainty: confirmed-by-running
- `flush_interval_secs: 5` is referenced nowhere in code (grep). The only flush triggers are 256 buffered rows, `close()` and
  `finalize()`. Per-tape cadence at 4 Hz is therefore: tick/basis/level about every 2.7 s, decision about every 64 s, and
  signal/order only at session end.
- `kill -9` after 600 rows: `tick_tape.parquet` raises `ArrowInvalid` (no footer); jsonl has all 600 lines. Any hard stop (power cut, terminal closed,
  Windows task kill) makes the whole day's parquet unreadable. The jsonl is then the only record, yet the manual recommends deleting the jsonl after the day.
- Row groups of 256 rows give 2.6k to 7.5k row groups per file, which is poor for compression and scan speed (see section 2.3).
- Review copy has no `order_tape.parquet` or `signal_tape.parquet` (only jsonl). A normal close writes them (my restart_sim shows it), so either the
  bundle was curated or those days did not close cleanly. Uncertain; verify on the operator's disk.
- Fix:
  - Wall-clock flush: in `append`, if `now - last_flush[name] >= flush_interval_secs`, flush. The config key then becomes real.
  - Write bigger row groups (see 2.3) and close/reopen per hour (`part-HHMM`) so a crash costs at most one part.
  - Make deletion of the jsonl conditional on a `tape_fsck` pass (parquet readable and row count equals jsonl line count).

### [HIGH] `ReplaySource` loads the entire day into Python objects  (feed.py:80-110)  certainty: confirmed-by-reading (size by arithmetic)
- Both branches build `list[dict]` of ALL rows (`json.loads` per line, or `table.to_pylist()`). A tick row is a dict holding about 26 strike dicts, roughly
  15 to 25 KB of Python objects, so 1.4 to 1.9M rows would need about 20 to 40 GB. `replay_browser.py` already works around this with
  `iter_batches`. `replay.py` and `--mode replay` do not, and nothing tests them beyond a 150-round synthetic day.
- Fix: one streaming reader (`ParquetFile.iter_batches(batch_size=20000)`, grouping by `ts` as `replay_browser` does) shared by `replay.py`, `app --mode replay` and
  `replay_browser.py`, with the session-split logic above.

### [HIGH] `TapeWriter.event()` raises on an undeclared type, and the two call paths behave oppositely  (tape.py:103-104; orchestrator.py:230-247; feed.py:149-157)  certainty: confirmed-by-reading
- Static scan of all `event()/_emit_event()/_feed_event()` literals: today every type is declared (good, `enum_audit.py`).
- But a new type that is missing from `EVENT_TYPES` behaves differently by caller. From the feed (`_feed_event`) the `ValueError` is swallowed by `except Exception: pass`, so the event
  is silently lost. From the orchestrator, calls made directly in `process_round` (REVERSAL_WATCH, APPROACH_ALERT, ENTRY_REJECTED, caution window) are outside the
  per-instrument try/except. The exception reaches `run()`'s handler, which writes SESSION_ERROR, finalizes and ends the trading day, possibly with a position open.
- This bites exactly the 2.0 work (new SPOT_FROZEN etc.). The same applies to `tape.append("decision_tape", ...)` raising (e.g. `_json_default` on a bad object):
  that is also outside the per-instrument guard.
- Fix: `event()` never raises in live. On an undeclared type it writes `event_type="UNDECLARED"` with `payload.original_type` and counts it. Enforce at CI time instead:
  an AST test over all literal event types (my `enum_audit.py` is 20 lines), plus keep the gate-2 check. Wrap the `process_round` tape writes in a guard that emits TAPE_ERROR instead of killing the session.

### [MEDIUM] Declared enums are not enforced anywhere; `post_cooldown` is written but undeclared  (schema.py:23-32; orchestrator.py:565,573)  certainty: confirmed-by-running
- `BASIS_MODES, RIGHTS, RECALC_TRIGGERS, DEC_STATES, DECISIONS, ORDER_ACTIONS, ROUTING_MODES` appear only in schema.py's own `__all__`
  (grep: zero other references, not even in verify_vajra.py). Only `EVENT_TYPES` is enforced.
- Recorded `level_tape.recalc_trigger` includes `post_cooldown` (2 rows 10-15, 1 row 10-16), absent from `RECALC_TRIGGERS`; spec 9.2 and the dashboard colouring also omit it.
- `decision_tape.reason_code` can carry executor strings not in `Reason`: `NO_QUOTE`, `PREMIUM_TOO_THIN`, `INSUFFICIENT_CAPITAL`, `ORDER_PLACEMENT_FAILED` (orchestrator.py:613; test_entry_path asserts
  `INSUFFICIENT_CAPITAL`). None occurred on the three days (only NO_APPROACH / POSITION_ACTIVE / APPROACH_ENTRY), so no recorded violation, but it is latent.
  `order_tape.exit_reason` values (SL_HIT, TRAIL_SL, EXPIRY_FLATTEN, END_OF_SESSION, NO_QUOTE, PREMIUM_TOO_THIN, INSUFFICIENT_CAPITAL, ORDER_PLACEMENT_FAILED,
  NO_QUOTE_AT_FLATTEN) have no declared enum at all. ANOMALY `payload.kind` has 9 free-form values and no enum.
- Declared but never referenced `Reason` codes: `MISSING_TICK`, `SESSION_ERROR`, `STALE_CHAIN`.
- Also: for NIFTY after a prev-close seed the first intraday calc is labelled `interval`, not `open` (freshness seeds `_last_attempt_ms`), contradicting the docs; `open` only appears for sisters/stocks.
- Fix: a `validate_row(tape, row)` in `append` that, on enum mismatch, writes the row anyway and emits a rate-limited `TAPE_ERROR`; add `post_cooldown`; add `EXIT_REASONS`, `ANOMALY_KINDS`; add all the new codes below. Enforce with tests (section 5).

### [MEDIUM] Seed `level_tape` row is stamped outside the trade date  (orchestrator.py:717-718, 764)  certainty: confirmed-by-running
- `seed_from_prev_close(seed, seed.ts_ms)` uses the previous session's last tick time. `level_tape` first row: 10-15 file starts 10-14 15:29:12; 10-16 starts 10-15 15:29:59; 10-14 starts
  06-25 15:30:05 (the stale June seed). `STALE_PREV_CLOSE_SEED` carries the same June ts (so the 10-14 event_tape has one non-monotonic ts).
  Date-range slicing silently drops it and the partition is no longer date-pure.
- Fix: stamp the seed row with the session start ts and put the source time in a `anchor_ts` column.

### [MEDIUM] Seed depends on `tick_tape.jsonl`, which the manual says to delete; seed may read the garbage 15:20+ tail  (feed.py:185-215; OPERATOR_MANUAL §5)  certainty: confirmed-by-reading
- `prev_close_snapshot` only looks for `tick_tape.jsonl` of the last prior day. Delete it as advised and the seed falls back to the stale June recording (this is exactly the 10-14 STALE seed).
  The review copy has no jsonl, so a seed on 10-19 from this folder would be stale.
- "The close" is the LAST NIFTY row, which on all three days is after the 15:15 to 15:20 frozen/garbage cash feed (SHUTDOWN at 15:29:12 / 15:29:59 / 15:25:52), so the seed band comes from a broken spot.
- Fix: at `finalize` write a tiny `close_snapshot.json` (last sane NIFTY snapshot, with feed_sanity ok), and read that; fall back to the parquet tail.

### [MEDIUM] Parquet flush failures drop a whole 256-row batch and are invisible on the tape  (tape.py:122-129)  certainty: confirmed-by-reading
- Any exception (also a Windows rename lock on the `.bak` step) is logged to the `vajra.tape` logger only and `finally` clears the buffer. The jsonl still has the rows, but the
  parquet is silently short or absent and no `event_tape` row says so (violates R19 "provable from the tape").
- Fix: retry row-by-row to isolate the bad row, emit `TAPE_ERROR {tape, n_rows, error[:160]}` (rate limited), keep the buffer if the writer could not open. Also add a lock file so two writers (a replay with `--out tapes`) cannot append to a live day.

### [LOW] `schema_version` has two sources; verify checks the wrong one; replay never reads it  (__init__.py:12, vajra_config.yaml:7, verify_vajra.py:274)  certainty: confirmed-by-reading
- Package constant is the `TapeWriter` default and schema.py export. Live/replay/verify use the yaml. All are "1.0.0" today. If they drift, tapes carry mixed versions and gate 2 (compares to yaml)
  still passes. No reader inspects `schema_version`; `replay.py` stamps outputs with the current config version regardless of input.
- Fix: single source (the package constant); drop the yaml key (or assert equality at startup); readers must dispatch on the input version (section 2.5).

### [LOW] Other tape details
- `signal_tape.conviction` is signed (-0.008, -0.019, -0.156 recorded) because it logs `conf["confidence"]` while the executor gets `abs(...)` (orchestrator.py:963 vs 598). `continuation_confirmed`/`confirm_secs_elapsed`/`lead_mode_flag` are constants in the anticipatory path (wasted columns). certainty: confirmed-by-running.
- `_json_default` falls back to `str(o)` for any unknown object, so jsonl and parquet can diverge silently (parquet would error, jsonl would stringify); and `json.dumps` emits bare `NaN` (not strict JSON). Use `allow_nan=False` plus sanitising to null.
- No link between ENTRY order row, signal row, APPROACH_ALERT event and the decision row except identical `ts`; no `trade_id`.
- `fh.flush()` on every append of every tape (about 72 syscalls per round) is fine on Linux; on Windows it is the hidden cost behind the 4 Hz loop. Acceptable once jsonl volume is cut.
- `event_tape` is the only place feed gaps live (spot-frozen/implausible do not exist as events at all); it is small (0.01 to 0.2 MB) but semantically flooded (REVERSAL_WATCH 1,351 and 821). Gaps in `tick_tape` itself are not representable: the max round gap on all three days is about 1.9 s, and a round missing NIFTY writes nothing but a FEED_GAP latch.

## 2. Improvements / design proposals (ranked)

### 2.1 Version 2.0.0 and a session-aware layout (do first; everything else hangs off it)
- Universal columns on every tape: `schema_version` ("2.0.0"), `session_id` int64, `seq` int64 (per-session monotonic row counter, makes (session_id, seq) a total order and detects loss).
- Day dir layout: `tapes/<date>/<tape>/part-<session_id>-<NNN>.parquet` (hourly parts) plus `MANIFEST.json` (session list, per-part row counts and min/max ts, closed flag).
  Hourly parts bound crash loss and make `tape_fsck` trivial.
- Events added: `SESSION_START {session_id, version, git/config hash}`, `TAPE_ERROR`, `SPOT_FROZEN`, `SPOT_RECOVERED`, `SPOT_IMPLAUSIBLE`, `FEED_UNTRUSTED_ENTER/EXIT` (or a single `FEED_HEALTH` with state), `REGIME_CHANGE`, `BAND_NARROWED`.
  All edge-triggered with hysteresis, declared in `EVENT_TYPES`.

### 2.2 level_tape 2.0 (new regime), written on change plus a heartbeat
Write a row only on (re)compute plus a heartbeat every 30 s (rows go from 1.9M/day to about 5k/day). Add columns (all nullable so 1.0 rows map to null):

| column | type | meaning |
|---|---|---|
| `regime` | string | EXPIRY / NEAR (1 trading day to expiry) / MID / FAR; enum `REGIMES` |
| `dte_trading` | int32 | trading days to active expiry (holiday-aware; the 10-14/15/16 days = 3/2/1) |
| `tte_minutes` | float64 | minutes to 15:30 on expiry date |
| `method` | string | `ITM_VWAP` (v1) / `EXTRINSIC_CLAMP` / `HALF_WIDTH_CAP` ...; enum `BAND_METHODS` |
| `raw_half_width` | float64 | (raw upper - raw lower)/2 before narrowing (the v1 band; measured 90 to 132 pts p50) |
| `half_width` | float64 | half width actually used after narrowing |
| `narrowed` | bool | `half_width < raw_half_width` |
| `narrow_cap_pts` | float64 | cap parameter used (null if none) |
| `extrinsic_ce`, `extrinsic_pe` | float64 | VWAP(K) minus intrinsic at anchor spot for the ITM legs (the quantity that widens the band off expiry) |
| `extrinsic_est_pts` | float64 | model estimate (e.g. from ATM straddle or `sigma*sqrt(T)`) used by the narrowing method |
| `anchor_spot`, `anchor_ts` | float64, int64 | spot and wall time at which this band was computed (fixes the seed-ts defect) |
| `recalc_trigger` | string | add `post_cooldown`, `regime_change`, `narrow_refresh` |
| `age_secs` | drop | derivable as `ts - anchor_ts`; remove from held rows |

### 2.3 decision_tape 2.0 (idle diagnostics: why HOLD)
97% of decision rows are `NO_APPROACH`, which today conflates far-from-level, before 09:21, after 15:25, no band, stale levels, and untrusted feed. Add:
`spot`, `upper`, `lower`, `dist_up`, `dist_dn`, `nearest_side`, `nearest_dist`, `nearest_dist_pct_of_halfwidth`, `regime`, `narrowed`, `in_session` (bool),
`levels_stale` (bool), `feed_ok` (bool), `feed_flags` (int bitmask), `entry_gate` (string: enum below), `cooldown_remaining_ms`, `position_active`, `trade_id`.
Replace the one catch-all `NO_APPROACH` with typed `Reason` codes: `OUT_OF_SESSION_PRE`, `OUT_OF_SESSION_POST`, `NO_BAND`, `LEVELS_STALE`, `FEED_UNTRUSTED`, `FAR_FROM_LEVEL`, `LATE_ENTRY_CUTOFF`
(keep NO_APPROACH as an alias for 1.0 readers). Storage impact is small: decision_tape is only 1.9 to 2.6 MB per day today; 12 extra scalar columns add roughly 1 MB.
Aggregate a derived `idle_minutes_by_reason` table in the post-close report from this column. That turns "I sat idle" into a measured breakdown.

### 2.4 Feed health: a `health_tape` (parquet, 1 row/second, plus edge events)
Columns: `ts, session_id, seq, nifty_spot_age_ms, nifty_spot_run_secs (same-value run), nifty_future_age_ms, spot_vs_future_gap` (basis minus its 60 s median),
`basis, basis_med60, options_ticking (count of strikes whose ltp changed in last 5 s), rounds_last_sec, instruments_present, flags (bitmask), state` (OK / FROZEN / IMPLAUSIBLE / RECOVERING).
Calibrated thresholds from the three days (sanity_calib.py):
- `SPOT_FROZEN`: NIFTY spot unchanged >= 30 s while implied future/option LTPs changed in that window. Benign max is 16 s mid-day; the three real freezes are 265 to 306 s. 30 s fires 235+ s early, with a hysteresis exit of 3 distinct spot values.
- `SPOT_IMPLAUSIBLE`: |basis - median(basis, 60 s)| > 25 pts, or |basis| > 60 pts. Benign max deviation 14.6 pts; the bad period reached 128 to 279. After a frozen episode ends, keep state RECOVERING until the deviation is under 10 pts for 60 s (the 15:20 resume tick is the garbage one).
- Gate: entries and level re-anchors are blocked while state != OK; `decision_tape.reason_code = FEED_UNTRUSTED`. (Detector policy belongs to other reviewers; the tape must carry the evidence either way.)

### 2.5 Backward-compatible replay of 1.0 tapes
- Reader reads `schema_version` per row (1.0.0 rows have no `session_id`/`seq`/new columns). Implement `upgrade_row(tape, row)` that fills defaults: `session_id` = file/partition epoch, `seq` = row index,
  new columns null, `regime` = null (derive from `dte_trading` if the replay config has a calendar).
- Use `pa.Table.from_pylist(..., schema=TAPE_SCHEMAS_V2)` after upgrade, or `pq.read_table(columns=...)` with missing columns synthesised. Do not rewrite old tapes.
- Replay output stamps 2.0.0 but records `source_schema_version` in its `SESSION_START`.
- Splitting a 1.0 day into sessions: use STARTUP_CONFIG events (4 on 10-16) plus gaps > 60 s in tick `ts`.
- Add a golden test: replay the committed 10-16 tick tape under 2.0 code with `regime`/narrowing disabled and require identical trades (2 trades, +8,459.75) to the 1.0 result.

### 2.6 tick_tape size strategy (cost today: 2.7 to 8.0 GB/day jsonl + 0.19 to 0.39 GB parquet)
Principle: tape exactly what the engine consumed ("tape-what-you-consume"); put the sampler before the engine, not between engine and tape, otherwise replay stops reproducing decisions.

| option | est. per day (10-15 scale) | risk |
|---|---|---|
| A. today | 8.0 GB jsonl + 0.39 GB parquet | disk, 256-row groups, restart split |
| B. parquet only, ZSTD, 8192-row groups, flush every 5 s, drop tick/basis/level jsonl; keep a tiny jsonl (decision, order, signal, event, per-minute `rounds_per_min`/health) | about 0.2 GB (parquet re-encode expected 30 to 45% smaller than SNAPPY/256; not yet measured) | crash loses <= 5 s of ticks; mitigate with hourly part files |
| C. B + sisters/stocks at 1 Hz (engine also consumes them at 1 Hz), NIFTY 4 Hz | about 0.10 to 0.12 GB (tick rows 24 -> about 9 per second-equivalent, estimated) | sister signals update 1 Hz; sisters re-anchor every 3 min and are advisory, entries do not depend on them (no veto) |
| D. C + on-change encoding for non-NIFTY (60% of rows are exact repeats) | about 0.06 to 0.08 GB | replay must forward-fill; keep a keyframe every 10 s |

Recommendation: B now (zero behaviour change, byte-identical replay), then C once the replay golden test passes. Do not downsample NIFTY (97% of its rows change; it is the trade instrument).
Also keep one raw "rounds per minute" row per minute (`ts, rounds, instruments_present, max_gap_ms`) in a small jsonl so a gap is provable without scanning ticks.
Keep `level_tape` change-only (2.2) and drop `basis_tape` columns that duplicate tick_tape (`spot, future, basis`), which is about 30% of its bytes.
Expected jsonl removal also removes the seed/disk-hygiene coupling (write `close_snapshot.json`).

### 2.7 Housekeeping
- `tape_fsck.py`: per day, check parquet footers, `part` row counts vs MANIFEST, ts monotonic, partition date purity, enum membership (using the schema enums), `seq` continuity. Run in `verify_vajra`.
- Row validation in `append` against enums and dtypes, emit `TAPE_ERROR` instead of raising.
- Record `config_hash` and package version in `SESSION_START`; config snapshot already in STARTUP_CONFIG (1 per restart, 4 on 10-16).

## 3. Recommendations specific to the two operator asks

### A. Non-expiry productivity
- The tape can already show the cause: `level_tape.raw_half_width` / `extrinsic_*` will quantify the ITM-VWAP extrinsic per day, and `decision_tape.nearest_dist_pct_of_halfwidth` plus `entry_gate` gives the idle-time breakdown by reason. Without these, the 3/6/4 minutes within 7 pts cannot be audited from the tape alone.
- Whatever band method is chosen, log both the v1 band (`raw_*`) and the narrowed band in the same row so a replay can A/B them. Log `narrowed`, `narrow_cap_pts`, `regime` and `dte_trading` so every decision is explainable.
- Hold the "held band" rule: write `anchor_ts`/`anchor_spot` so re-centering every 20 minutes is visible. Per-day target to audit: share of NIFTY rows with `nearest_dist <= prox` at least once per 20-min window.

### B. Sounds
- Sound triggers are derived in the browser from `/api/state`; they are not on any tape, so the operator's "too many sounds" cannot be replayed or counted. Add `SOUND_POLICY`-level `ALERT` events (type, reason, ts) to event_tape (edge-triggered, one per episode) and let the dashboard play from that stream. Then a replay can count sounds per day (target a handful) without a browser.
- Feed-health states (SPOT_FROZEN, FEED_UNTRUSTED) are the one new category worth a distinct sound; they go through the same event.

## 4. Things that are fine and should NOT be changed
- jsonl-before-parquet durability concept and the `.bak` preservation instead of truncation (the intent is right; fix the naming/session semantics, not the principle).
- `ts` as int64 epoch ms on every row, `schema_version` on every row, ts monotonic on all three days (0 backsteps), no duplicate (ts, instrument) rows, no NaN-induced failures observed, round cadence p50 251 ms with max gap about 1.9 s.
- Decision tape written after the order outcome (cannot show FIRE while order_tape shows REJECT).
- `EVENT_TYPES` being declared and gate-checked in verify (keep the check, just do not raise in live).
- Never fabricating a level on a failed band (`band.ok` guard).
- Determinism: replay reproduces recorded decisions per the lead; keep deterministic float-sum order and tape-driven clocks.

## 5. Tests I would add
1. AST test: every literal event type passed to `event/_emit_event/_feed_event` is in `EVENT_TYPES`; every `Reason.X` exists; every string assigned to `recalc_trigger`/`reason_code`/`exit_reason` is in its enum (my `enum_audit.py` is the seed).
2. Restart test: two sessions, same date. Assert no data loss (union of parts equals rows written), `session_id` present, reader returns both sessions, and replay resets engine state per session.
3. Crash test: `kill -9` mid-session in a subprocess; `tape_fsck` recovers via the part files and reports lost rows <= flush interval.
4. Time-based flush test with a fake clock (`flush_interval_secs` honoured; signal/order rows reach parquet within the interval).
5. `event()` with an undeclared type does not raise and writes an UNDECLARED row; a bad row in a flush batch loses only that row and emits TAPE_ERROR.
6. Seed row ts is inside the session date; `close_snapshot.json` written at finalize and used for seeding when the jsonl is absent.
7. 1.0 -> 2.0 golden: replay the 10-16 1.0 tape under 2.0 code, identical trades (2 trades, +8,459.75); `upgrade_row` fills defaults.
8. Feed-health calibration test on the three recorded days: SPOT_FROZEN fires once per day, between 15:15:30 and 15:16:30; zero fires before 15:15; SPOT_IMPLAUSIBLE fires only after 15:20.
9. Streaming reader memory test: replay a 1M-row tape with peak RSS under 1 GB.
10. Size budget test: synthetic day -> tape bytes per round under a stated ceiling, so tick-tape bloat cannot return unnoticed.
