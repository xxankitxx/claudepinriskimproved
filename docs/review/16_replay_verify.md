# 16 — Replay & verification tooling reviewer: findings

Scope: replay.py, replay_browser.py, verify_vajra.py, demo_dashboard.py, `vajra/feed.py:ReplaySource`, tests/test_determinism_and_tapes.py. Whole codebase read (credentials.py read in redacted form only). Scratch: `/home/claude/vajra2_review/scratch/16/` (stream_source.py = prototype v2 reader, equiv_check.py, mem_pylist.py, bench_replay.py, recon_rounds.py, meta_*.py).

Measured facts (all confirmed by running, bounded prefixes only, no full-day replay):
- tick_tape = 660,873 / 1,913,436 / 1,379,651 rows (10-14/15/16), 24 instruments x ~26 strikes, written as **2,582 / 7,475 / 5,390 row groups of 256 rows** (Snappy).
- `ReplaySource._read_rows` → `to_pylist()` costs **17.4 KiB per row** (40k-row prefix, RSS delta 712 MB). Extrapolated: 10-14 = 11.5 GB, 10-15 = **34 GB**, 10-16 = 24 GB of Python objects. This box has 8 GB. The jsonl path builds the same dicts, so it is no better.
- Engine speed on 2,000 rounds of 10-15 (shared 2-core box, noisy): NullWriter 9.3 ms/round (~12 min/day), real TapeWriter 26.7 ms/round (~35 min/day) and **205 MB written per 2,000 rounds (~8 GB/day)**. Skipping `_tick_row` construction alone saved nothing measurable; the cost is json.dumps + per-row `fh.flush()` + parquet.
- Streaming prototype (column-wise flatten, row-group pruning) is round-for-round identical to the shipped bucketing on a 3-minute prefix (719 rounds, 0 mismatches).

## 1. Correctness defects (most severe first)

### [CRITICAL] ReplaySource cannot replay a real day: whole tape into a Python list, then sorted  (vajra/feed.py:80-110, replay.py:86)  certainty: confirmed-by-running
- Failure: `python replay.py --tape tapes/2026-10-15` materialises ~34 GB of dicts before the first round. OOM-kill on any normal machine. The operator's only working path is replay_browser.py (own streaming code), so the "tune on tape" loop in spec §9 / config comments is effectively unusable for full days from the CLI. verify gate 5 only passes because it uses a 150-round synthetic tape.
- Also `rows.sort()` is unnecessary: tapes are already ts-ordered (verified non-decreasing on all 3 days, no duplicate (ts,instrument) keys).
- Fix: single streaming `ReplaySource` (section 2.1). Keep an explicit ordering check instead of a global sort.

### [HIGH] Re-running replay.py into the same --out silently corrupts the output tapes  (replay.py:84, vajra/tape.py:55-75)  certainty: confirmed-by-running
- `TapeWriter` opens jsonl in append mode and renames the old parquet to `.bak`. Running the same replay twice (e.g. after changing a param) produced 2 jsonl lines for 1 append plus a `.bak` parquet. Any analysis reading `order_tape.jsonl` double-counts trades; parquet and jsonl disagree. The determinism gate hides this by using fresh temp dirs.
- Fix: replay_session must refuse (or wipe with `--force`) a non-empty `<out>/<date>`; better, write to `<out>/<run_id>/<date>` where run_id = hash(overrides + code rev).

### [HIGH] `--set` typos are silent no-ops; overrides are not recorded on the output tape  (replay.py:61-73, 76-91)  certainty: confirmed-by-running
- `--set trigger.pad_flor=0.15` is accepted and just adds a dead key (`{'pad_floor': 0.1, ..., 'pad_flor': 0.15}`). The operator believes he tested a variant but ran the baseline. `setdefault(p, {})` would also clobber/crash on non-dict nodes. Also replay_session never calls `write_startup_events`, so the output tape has no STARTUP_CONFIG/EXPIRY_RESOLVED/override record; a results directory cannot say what produced it.
- Fix: validate that every override path already exists in the loaded yaml (or is in an allow-list for optional keys) and abort with the closest-match suggestion; coerce type to the existing value's type; echo a baseline-vs-override diff; write a `REPLAY_CONFIG` event (source tape, overrides, git hash/file hashes of vajra/*.py, tape sha) first. Needs `REPLAY_CONFIG` added to `schema.EVENT_TYPES`.

### [HIGH] Replay is not a faithful reconstruction of live: no seed, and gap rounds differ  (feed.py ReplaySource has no prev_close_snapshot; orchestrator.py:289)  certainty: confirmed-by-reading + running
- tick_tape row is only written after `br.ok`. recon_rounds.py: 3 / 3 / 4 live decision rounds on 10-14/15/16 have **no** NIFTY tick row, and 88 / 108 / 59,019 rounds carry <24 instruments (10-16: 23 is normal, one universe member never reaches the tape). In replay those NIFTY rounds hit `round_missing_nifty` (no decision_tape row), and instruments that failed basis are absent from `signals` instead of NEUTRAL, so the `instruments` vector and any gap events differ from the recorded ones. Also no prev-close seed, so level_tape labels ('open' vs 'interval') and one 'prev_close_seed' row differ.
- Why it matters: spec §9 promises "no decision may exist that is not reconstructable from the tape". Byte-comparing replay with the live decision_tape can never pass; the lead's "reproduces 2 trades +8,459.75" holds, but only at the trade level.
- Fix: record a gap marker row (or log every snapshot incl. failed-basis ones with null basis) so replay sees the same inputs; compare replay vs live on order_tape + level anchors (not on decision bytes) and report the diff explicitly in `replay.py --compare-live`.

### [MEDIUM] Two replay readers with different failure semantics  (feed.py:100-108 vs replay_browser.py:36-56)  certainty: confirmed-by-reading
- feed.ReplaySource: one malformed row raises inside `rounds()`, caught by `Orchestrator.run`'s blanket except → SESSION_ERROR and early finalize (replay silently ends, flattens position). replay_browser: `except Exception: pass` silently drops the instrument. Same tape, different outcomes, neither counted. replay_browser also supplies `prev_close_snapshot() -> None`, which prints "CRITICAL: NO prev-close seed" on every browser replay (noise that trains the operator to ignore the real warning), and uses NullWriter so none of its output is auditable; its default day is 2026-06-30 (not in tapes/) and `tick` path check happens after argv parse only.
- Fix: one reader, with counters (rows, rounds, dropped_rows, gap_rounds) surfaced in the summary and a `--strict` flag.

### [MEDIUM] verify gate 6 is fragile and misses the real default-off risk  (verify_vajra.py:327-354)  certainty: confirmed-by-reading
- Walks only `is False` leaves of dict nodes (lists skipped). Any new boolean (e.g. a `feed_sanity.enabled: false` kill-switch someone adds for v2) trips it and forces editing a whitelist keyed on the last two path parts; conversely it passes `nonexpiry_mode: observe|off`, `0`, `""`, `"off"` and the code default `nonexpiry_mode default="observe"` (config.py:107) which is precisely the spec's idle-on-non-expiry behaviour. Gate 6 would not have caught complaint A.
- Fix: replace with an explicit manifest `config/feature_manifest.yaml`: `{key_path: required_value}` for every behaviour switch (`routing.nonexpiry_mode == trade_small`, `feed_sanity.enabled == true`, ...) plus a test that every boolean/enum leaf in vajra_config.yaml is either in the manifest or in an `operator_state` list. Also assert the in-code `default=` equals the yaml value (grep `cfg.get(` defaults) so deleting a yaml key cannot flip behaviour.

### [MEDIUM] Gates 2/5 prove little: synthetic expiry-regime data only, determinism on one tape  (verify_vajra.py:213-317, tests/test_determinism_and_tapes.py:249-255)  certainty: confirmed-by-reading
- Synthetic generator is an expiry-day Black-Scholes path (`choose_synthetic_session` picks an expiry day, thin extrinsic, 150 rounds x 15 s, 13:00 start). So no gate exercises non-expiry geometry, the 15:15 frozen-cash-feed pattern, parquet read path (jsonl sidecar always exists in the synthetic run so `ReplaySource` never touches parquet), or restart days. Determinism compares only `decision_tape.jsonl`; order_tape, level_tape, event_tape and parquet are not compared, so an executor non-determinism would pass. Gate 2 checks emptiness for all tapes but tick count only; gate 1 scans `["vajra","replay.py"]` only (not verify_vajra.py, replay_browser.py, demo_dashboard.py).
- Fix: section 2.4.

### [MEDIUM] demo_dashboard.py is dead code in this tree and mislabelled  (demo_dashboard.py:23-28)  certainty: confirmed-by-reading
- Uses `RealSessionSource` which needs `vajra/data/real_session_*.jsonl`; only `real_nifty_day.csv` exists → FileNotFoundError at start. Docstring says "synthetic", code says real MYTHOS June data. Loops forever creating a new TapeWriter on the same tmp dir each pass (jsonl grows unbounded, `.bak` per pass), startup events stamped ts=0, `rounds[-1]` crashes if empty. replay_browser.py supersedes it with real VAJRA tapes.
- Fix: delete it, fold `--loop` into replay_browser. Same dependency also breaks `LiveBreezeSource.prev_close_snapshot` fallback #2 (returns None → "NO prev-close seed") on a fresh machine.

### [LOW] tick_tape written as 256-row row groups  (tape.py:39,96)  certainty: confirmed-by-running
- 2,582-7,475 row groups per day, Snappy; flush every 256 rows also means a crash loses nothing only because of the jsonl sidecar. Reading is slower and files ~2x larger than needed. Fix: keep the 256 flush for durability, but add a post-session `tapes compact` step (row_group_size 64k, zstd-6, sorted) and delete the multi-GB jsonl sidecar after a successful compact+verify.

### [LOW] Misc
- `replay_session` hard-codes `shifted_from_holiday=False` when rebuilding ExpiryResolution from events (replay.py:47); `_read_expiry_from_events` reads the first EXPIRY_RESOLVED only (after a restart across a config change later ones are ignored) and `session_date` is taken from the directory name.
- Level_tape's first row is the `prev_close_seed` stamped with the PREVIOUS day's ts (15:29:12 on 10-15 etc.); anything grouping by date or assuming session-bounded ts mis-assigns it. Document or stamp it with the session date.
- Three copies of a NullWriter exist (replay_browser, test_entry_path, test_feed_live); put `NullTape`/`RecordingTape` in vajra/tape.py.

## 2. Improvements / design proposals (ranked): v2 tooling

### 2.1 One streaming ReplaySource (vajra/replay_source.py)
- API: `ReplaySource(dir, ts_lo=None, ts_hi=None, instruments=None, strict=True)` yielding `(ts, {inst: MarketSnapshot})`; exposes `.stats` (rows, rounds, dropped, out_of_order, gap_rounds).
- Parquet: `iter_batches(batch_size=16384, row_groups=pruned_by_ts_stats)`; flatten the `strikes` list column once per batch (`pc.list_value_length` + `.flatten()` + per-field `to_pylist()`), build `StrikeQuote` directly, no per-row dict. Memory is O(batch) (~20 MB). jsonl fallback line-streams through the same bucketing generator. Prototype: scratch/16/stream_source.py (verified identical rounds).
- No global sort. If a ts decreases: raise in strict mode, else fold into the current round and count it. Handle `*.parquet.<ms>.bak` explicitly (`--include-bak` stitches multi-session days in time order).
- replay_browser and replay.py both import it; delete StreamReplay.

### 2.2 replay.py v2: `--no-tick-tape --summary`
- Writer wrapper `FilteredTape(inner, drop={"tick_tape"})`, default drop for replays; `--tapes none|lite|full`. (Measured: tick_tape is most of the 205 MB/2,000 rounds and the 2.9x slowdown. Dropping the *build* of the row gains nothing, so no orchestrator change is needed, just drop at append.) With `--tapes none` use a null tape: ~9 ms/round, ~12 min/day single core vs ~35 min.
- Add `--from HH:MM --to HH:MM` (row-group pruned; warm-up of `--warmup 30m` before `--from`, since EWMA/RollingStats need history) and `--rounds-stride` for quick looks.
- `--summary` (stdout + `summary.json` in run dir): trades table (entry/exit time, strike, premium in/out, reason, P&L), total P&L, win rate; level anchors (time, trigger, lower/upper, half-width, ITM VWAPs, basis); per-minute distance-to-nearest-level percentiles and minutes within proximity_pts; idle diagnostics (counts of decision reason_codes: NO_APPROACH / EXPIRY_GATE_BLOCKED / PREMIUM_TOO_THIN / cooldown, first/last approach, longest idle gap, rejects); event counts by type/kind (REVERSAL_WATCH, FALSE_BREAK floods); feed-quality block (gap rounds, frozen-spot seconds, spot-vs-implied-future divergence maxima); reader stats; wall time per phase.
- `--profile`: cProfile-free coarse timers (reader / engine / tape) in the summary plus peak RSS (`resource`/psutil); `--profile=full` dumps top-25 cProfile.
- `--compare-live`: diff replay vs recorded order_tape + level anchors; non-zero exit if trades differ (this is the real fidelity check, section 1 HIGH).

### 2.3 sweep.py: parameter grids over recorded days
- `python sweep.py --days 2026-10-14,15,16 --grid trigger.itm_depth_strikes=1,2 --grid entry.proximity_pts=5,7,10 --workers 2 --out sweeps/<name>`.
- Cartesian product of `--grid key=v1,v2` (same path validation as replay), run `(day, combo)` jobs in `ProcessPoolExecutor` (each job streams its own tape, no tick/other tapes, only collects trades and anchors), cache keyed by (tape sha, combo, code hash) so unchanged cells are free, deterministic ordering.
- Output table (CSV + markdown): rows = combo, columns per day: trades, wins, P&L, max DD, avg half-width, minutes-in-zone; plus Total and "worst day". Mark the baseline row; show delta vs baseline; flag combos with <N trades as low-evidence. Warn loudly that 3 days / 1-2 trades each is not statistically meaningful (operator tunes on tape, so also report per-trade, not only P&L, and avoid picking the top cell).
- Cost: 3 days x 12 min = ~36 CPU-min per combo on this class of box, so for grids use `--rounds-stride 2` or NIFTY-only mode (`--instruments NIFTY` skips 23 instruments: ~20x cheaper, valid whenever the question is band/entry/exit since entry is NIFTY-only and confluence is display-only). Make NIFTY-only the default for sweeps; full-universe runs are for decision_tape work.

### 2.4 New verify gates (fixtures, not synthetic)
Build two committed fixtures under `tests/fixtures/` (NIFTY only, columns: all 10 tick_tape columns incl. the full `strikes` list since band, basis and pin need the ±6 chain; drop nothing else, drop all 23 other instruments; write with zstd-9, row_group_size 64k, ts-sorted, plus the matching `event_tape.jsonl` EXPIRY_RESOLVED row so `_read_expiry_from_events` works):
1. `nonexpiry_open_20261015`: 09:41:55-10:06 of 2026-10-15 = 5,761 NIFTY rows = **1.11 MB** (measured). Starts at the recorded session start so no warm-up mismatch, contains the 10:00 approach (the one legitimate approach that day), so it exercises anchor, entry, exit. 
2. `feed_freeze_20261015`: 15:12-15:27 of 2026-10-15 = 3,274 rows = **0.19 MB** (measured). Contains the real freeze (spot stuck at 22939.95 15:16-15:20, then +280 jump while implied future moved 0.5, basis -185.8). Same pattern on 10-14 and 10-16; add 15:12-15:27 of each only if size allows (all three together < 2 MB). Total budget stays far below 5 MB.
Gates (each replays with `--tapes none` in-process in a few seconds):
- G7 narrowed band: replay fixture 1 with v2 defaults; assert every NIFTY re-anchor has half-width <= the v2 cap (value from the A-side reviewers; today's 115-132) and that `nonexpiry` cap logic was applied (level_tape row flag / event); assert expiry-day synthetic still gives the old tight band (no regression).
- G8 productivity: fixture 1 yields >= 1 ENTRY (today: 0 trades before 15:20) and, when sized by `trade_small`, qty <= the configured fraction.
- G9 feed sanity: replay fixture 2; assert a FEED_SANITY (frozen spot / spot-vs-future divergence) event is emitted between 15:16 and 15:20, **zero ENTRY rows at or after 15:15**, and no APPROACH_ALERT from the post-jump garbage.
- G10 fidelity: replay fixture 1 and assert the NIFTY level anchors (ts, upper, lower) equal the recorded level_tape rows (extracted into `expected_levels.json`, tiny). This is the live-vs-replay check; trades compared with a tolerance list.
- G11 parquet+jsonl parity: replay fixture as parquet and as jsonl, assert identical order_tape/level_tape (covers the parquet path no test exercises).
- G12 determinism v2: hash order_tape, level_tape, event_tape, decision_tape (not only decision bytes) across two runs and across `--workers 1/2`.
- G13 manifest (replaces gate 6, section 1) and G14 reader memory: stream a 200k-row generated tape and assert peak RSS < 300 MB (psutil), so the OOM regression cannot return.
- Make gates data-driven (`GATES=[...]`, each returns (ok, detail), `--only 9`, `--json`), and scan all `*.py` in gate 1. Add `--profile`: per-gate wall time and peak RSS table, fail if total > a budget (e.g. 90 s).

### 2.5 Tests to add in pytest (cheap)
- ReplaySource parquet path, batch-boundary round straddling, ts-order violation, jsonl/parquet parity, `.bak` handling.
- `_apply_overrides`: unknown key rejected, type coercion, non-dict node.
- replay_session into a non-empty out dir refuses; two sequential replays give identical order_tape.
- recon test: record synthetic with a gap round (NIFTY basis failure) and assert replay emits the same gap marker.
- `verify_vajra` gates as functions importable and run in CI (currently only `python verify_vajra.py` manually).

## 3. Recommendations specific to the two operator asks
A (non-expiry levels): make the verification loop able to *measure* it before shipping: sweep over `{cap or shrink factor on ITM-VWAP extrinsic, proximity_pts, itm_depth}` on the three days NIFTY-only (about 4 CPU-min per combo with NIFTY-only + null tape instead of ~36), report trades, minutes-in-zone and P&L per day, and gate the chosen default with G7/G8 on fixture 1. Do not let sweep results alone pick the value: with 3 days and ~1 trade/day any cell is noise; require the cap to be justified by geometry (half-width vs spot move per 20 min) and use sweeps for sanity/regret only. Fixture 2 must stay in the suite so loosening the band cannot reintroduce the 15:20 phantom trades.
B (sounds): not in my scope; for replay_browser note the dashboard's sound button and `marketPhase()` use browser wall-clock, so replays at off-hours show the "muted during market hours" pulsing state wrongly; key on `round_ts`.

## 4. Things that are fine and should NOT be changed
- Determinism design (tape-ts-driven EWMA/freshness, sorted instrument iteration, Decimal HALF-UP `round_to`): replay-vs-replay determinism is real and valuable.
- Jsonl sidecar written before parquet (crash-safe) and the `.bak` rename on same-day restart.
- Per-instrument try/except isolation in `process_round`; no-fabrication rule (None-level band never recorded).
- Gate 1 (stub scan), 3 (golden formulas incl. discount mirror) and 4 (expiry mapping) are good and cheap; keep as-is.
- replay_browser's safety properties (separate port, null writer, never touches real tapes) are right; just move to the shared reader.

## 5. Tests I would add
See 2.4 (G7-G14) and 2.5. Highest value first: (1) reader memory bound, (2) parquet-vs-jsonl parity, (3) fixture-2 feed-sanity no-entry-after-15:15, (4) override validation, (5) replay-into-nonempty-out refuses, (6) live-vs-replay anchor fidelity.

Housekeeping note for memory/lead: this review box has 8 GB RAM and 2 cores shared with other reviewers; timings above are indicative (+-30%).
