# 18 — Tape schema & compatibility: teardown
Verdict: Ready for Monday. The 2.0 tapes are type-correct and readable, and 1.0 tapes replay through 2.0. The remaining problems are cosmetic or affect mixed 1.0/2.0 analysis. 2.0 is slightly better than 1.0 here (background parquet writer, jsonl flushed once per round, typed gate reasons, no live crash on an undeclared event type).
Score (your area): 1.0 = 7/10, 2.0 = 8/10

## Findings (most severe first)

### [MEDIUM] Mixed 1.0/2.0 tape roots silently lose the new columns in dataset-style readers (schema.py:121-183, `__init__.py:12`) — reproduced
- Setup: a root with a 1.0 day (10-16 decision_tape, 12 cols, schema_version 1.0.0) and a 2.0 day (decision_tape, 15 cols).
- `pyarrow.dataset.dataset(files)` and `pd.read_parquet(<root dir>)` take the first (alphabetically oldest, i.e. 1.0) file's schema. Both returned `[..., 'expiry_gate_flag','decision','reason_code','schema_version']`. `regime`, `feed_ok` and `nearest_level_dist` were dropped with no error.
- `pd.concat` of per-day frames keeps the columns, but `feed_ok` becomes dtype `object` with 2000 NaN for the 1.0 rows. `df[df.feed_ok]` then fails or mis-filters.
- level_tape is affected the same way (+5 columns: regime, band_method, legacy_upper, legacy_lower, narrowed).
- Fix: tell the operator to read per day, or pass an explicit 2.0 schema to `ds.dataset(schema=S.DECISION_TAPE)`. Optionally fill the missing columns on read.

### [LOW] `nearest_level_dist` is signed, but the comment says "pts to the nearer held level" (schema.py:181, orchestrator.py:807) — reproduced
- The code computes `min(upper-spot, spot-lower)`, which goes negative once spot is beyond a level.
- On the 10-15 fixture replay (non-expiry), 1250 of 3184 rows were negative; min -229.45, median +7.5.
- By reason: `APPROACH_ENTRY` was 6.1 to 7.0 and `NO_APPROACH` was at least 7.4, as expected. `POSITION_ACTIVE`, `AFTER_LAST_ENTRY` and `FEED_UNHEALTHY` were mostly negative. The -229 values occur during FEED_UNHEALTHY, when levels are stale and spot is garbage.
- An analysis like `dist < 7` would sweep in every breached or stale row.
- Fix: document it as "signed penetration depth", or add an absolute-distance column.

### [LOW] NaN is written as a bare `NaN` literal in jsonl (tape.py:`json.dumps`, orchestrator.py:1010) — reproduced
- `nearest_level_dist` is NaN only before the first band exists or when NIFTY is absent (never occurred in any of my runs: 0 NaN rows).
- Event payloads can also carry NaN, for example the NONFINITE_SPOT event.
- Python `json`, `pd.read_json` (reads `v: NaN` as None) and jq 1.7 all accept it. A strict parser (`parse_constant` raising) rejects it, and so do JS `JSON.parse` and some tools.
- In parquet the value is stored as NaN, not null (`null_count` 0, `isna()` 1). SQL `IS NULL` misses it.
- Same behaviour as 1.0 for the other float fields. Fix: pass `allow_nan=False` after mapping NaN to None, and store null in parquet.

### [LOW] `exit_reason` and reject reasons are not declared as an enum (schema.py, execution.py:26-29,295)
- `Reason` covers decision_tape `reason_code`, but `order_tape.exit_reason` values (SL_HIT, TRAIL_SL, EXPIRY_FLATTEN, END_OF_SESSION, NO_QUOTE_AT_FLATTEN) are not in schema.py.
- `NO_QUOTE_AT_FLATTEN` is not in `Reason`; the other reject reasons are.
- The vocabulary is unchanged from 1.0, so the operator's xlsx analytics (TRAIL_SL, BREAKEVEN, SL_HIT, EXPIRY_FLATTEN) are not broken.

### [LOW] Dead declarations (schema.py:51-62,246)
- `Reason.RISK_STANDDOWN` and the `RISK_STANDDOWN` event type are never emitted (grep finds no use outside schema.py), because max_trades and the daily loss stop are 0.
- Several 1.0 reasons (`REVERSAL_FREEZE`, `BIAS_*`, `EXPIRY_GATE_BLOCKED`, `FIRE_CONFIRM`, ...) are likewise rarely or never produced.
- Harmless. This agrees with the operator rule of no stand-down.

### [LOW] `tools/latency_bench.py:27` is a syntax error — reproduced
- `pq.ParquetFile("str(Path(...) / "tapes" ...")")`; `py_compile` fails with `SyntaxError`.
- It is a benchmark tool only, not on the live path, and it does not exist in 1.0.

### [LOW] jsonl sidecars are very large (tape.py, unchanged design from 1.0)
- In the 1.0-tape replay, the 25-minute window produced a tick_tape.jsonl of about 455 MB or more against a 30 MB parquet.
- Extrapolated to a full day, that is roughly 5 GB or more on D:. The recorded 1.0 tape dirs contain no jsonl for tick, so the operator seems to prune them.
- This is not worse in 2.0. The parquet footer is only written on `close()`. A kill mid-session leaves the parquet unreadable and the jsonl is the only record. That is also the same as 1.0.

### [LOW] Event sink thread-safety (feed.py:157-165, app.py:143) — plausible
- `_emit_event` is passed to the feed as `event_sink`. If feed events were ever fired from a websocket thread, `event_tape` writes and `_recent_events` would not be locked.
- I did not verify which thread calls `_feed_event`; it appears to be on the round-generating path. It is the same as 1.0.

### [INFO] Recorded 1.0 tapes lack order_tape.parquet and signal_tape.parquet
- The recorded 1.0 tapes have only order_tape.jsonl and signal_tape.jsonl.
- 2.0 also creates no parquet when a tape has zero rows (the replay printed `parquet=None` for signal and order). A consumer that requires `order_tape.parquet` must fall back to the jsonl or tolerate a missing file.

## Checks that came out fine
- **Value types**: every row of every tape in 5 sessions validated against `schema.TAPE_SCHEMAS` (types recursed into the struct and list fields; extra and missing columns checked).
  - Result: 0 type errors, no extra or missing columns. Parquet schema equals the declared schema, with 0 nulls.
  - Sessions: 3 synthetic expiry, 1 synthetic forced non-expiry, and the real 10-15 non-expiry fixture. The non-expiry runs exercised mtm bands, `post_cooldown`, REJECT rows and ENTRY_REJECTED.
- **Enums**: every `reason_code`, `recalc_trigger`, `regime`, `band_method`, `decision` and `action` written is declared. Reason codes seen: NO_APPROACH, APPROACH_ENTRY, POSITION_ACTIVE, POST_PROFIT_COOLDOWN, FEED_UNHEALTHY, AFTER_LAST_ENTRY, PREMIUM_TOO_THIN. `post_cooldown` is declared and written.
- **Event types**: every event type seen is declared (REGIME, FEED_HEALTH, APPROACH_ALERT, ENTRY_REJECTED, ANOMALY, ...).
  - An AST regex over the code found no undeclared literal other than non-reason constants.
  - `test_every_event_type_used_in_code_is_declared` guards the literals.
- **Undeclared event type**: `tape.event("BOGUS_TYPE", {...})` is stored as `ANOMALY` with `payload.undeclared_event_type="BOGUS_TYPE"`. It is logged and does not raise. 1.0 raised `ValueError` inside the round loop, which is a live-session risk, so this is the safer behaviour. The downside is that a typo is quietly recategorised, so an analyst filtering on a specific type misses it. The AST test guards the literals.
- **1.0 tapes replay through 2.0**: I ran a 25-minute window (140,385 rows) of the recorded 10-14 tick_tape via `replay_session` with the 2.0 config, and it completed with exit code 0.
  - It produced 134,548 tick rows, 5,877 decision rows and 121,307 level rows, all schema-equal.
  - The tick schema is unchanged between versions, and `EXPIRY_RESOLVED` from the 1.0 event_tape is parsed fine.
  - `ReplaySource` now streams batches of 20,000 instead of loading the whole day.
  - The replay took 10 minutes of wall time on the loaded box.
- **pandas and pyarrow**: `pd.read_parquet`, `pq.read_table` and `pd.read_json(lines=True)` all read the 2.0 tapes. `bool` columns stay bool in the 2.0-only frames.
- **Tests**: `tests/test_v2.py` and `tests/test_determinism_and_tapes.py` give 33 passed, including determinism and `test_api_state_survives_nan`.
- **Existing analysis**: the operator's xlsx results use trade-level fields (date, side, strike, lots, entry, exit, reason). order_tape rows and exit reasons are unchanged from 1.0 (execution.py differs only by `capital_fraction` and the `atm_strike` argument), so those tools are unaffected.
- **Same-day restart**: the `.bak` collision-safe rename is retained.

## What 2.0 does better than 1.0 (evidence)
- Parquet conversion runs on a background thread, with a queue join on `flush()` and `close()`. The jsonl is flushed once per round instead of once per row.
- An undeclared event type no longer kills the loop (above).
- The new columns give an audit trail: `regime`, `band_method`, `legacy_upper`, `legacy_lower`, `narrowed`, `feed_ok`.
- Typed "why no trade" gate reasons give a real diagnosis: 4 gate reasons plus cooldowns in the fixture, where 1.0 mostly gave NO_APPROACH.
- `schema_version` is "2.0.0" in rows (config and `__init__` agree). 1.0's config said 1.0.0.

## What 2.0 does worse than 1.0 (evidence)
- It adds columns to level_tape and decision_tape, which brings the mixed-schema risk in the first finding. The recorded 1.0 tapes and new 2.0 tapes will share one root.
- Rows after a worker-thread failure are dropped from parquet in batches of 256 (jsonl keeps them). 1.0 had the same batching, but errors are now logged from another thread.
- A broken `tools/latency_bench.py` was added.

## Tests / checks you ran
- `/home/claude/teardown/scratch/18/validate.py` and `run2.py`: record a session and validate every jsonl row recursively against the schema. They also compare the parquet schema, count nulls and check the enums.
- `win.py`: cut a window from the 1.0 tape. The replay through 2.0 used `replay_session` and was validated with `validate.validate`.
- NaN and undeclared-event probe: pandas, pyarrow, jq and strict JSON parsing.
- Mixed-version dataset probe.
- pytest on `tests/test_v2.py` and `tests/test_determinism_and_tapes.py`: 33 passed.
