# 22 — Test-suite audit: teardown
Verdict: The suite is green but does not check most of the operator's rules. The engine itself does follow them (19 rule tests I wrote all pass on unmodified 2.0), so nothing blocks Monday. But 25 of 34 meaningful hand mutations that break a binding rule pass all 101 tests. 2.0's suite is slightly better than 1.0's: it keeps all 77 of 1.0's tests unchanged and adds 24 for 2.0's own features. Coverage of the operator rules is the same as in 1.0.
Score (your area): 1.0 = 3/10, 2.0 = 4/10

## Findings (most severe first)

### [HIGH] Nothing tests the 15:25 flatten (execution.py:233) — reproduced
- `test_open_position_at_close_is_force_flattened_into_the_tape` (test_entry_path.py:312) calls `finalize()`. That covers the END_OF_SESSION path, not the in-round `manage()` flatten (EXPIRY_FLATTEN), which is the path that runs live at 15:25.
- Mutation M14: flatten at "15:29" instead of `flatten_before`. Result: SURVIVED, `101 passed`.
- Mutation M15: `if False:` instead of the flatten check, so the engine never flattens. Result: SURVIVED, `101 passed`.
- Fix: add `test_1525_flatten_inside_process_round`. It is in my file and kills both mutants. It checks that a position is still open at 15:24:59 and exits with EXPIRY_FLATTEN at 15:25:00.

### [HIGH] Nothing tests the 09:21 start of the entry window end to end (orchestrator.py:673, 695) — reproduced
- The only test is `test_trade_gate_0930_and_calc_gate_0920` (test_timing.py:315). It calls `hhmm_reached(_ms(9,25), "09:30")` against string literals, so it is tautological: it checks neither the rule (09:21) nor the config.
- M05: code gate hard-coded to 09:30. SURVIVED.
- M06: yaml `trade_start: "09:30"`. SURVIVED.
- M08: the trade-start gate deleted entirely, so entries are allowed from 09:20. SURVIVED.
- The end of the window has the same gap. M07, a code cutoff of 15:00, SURVIVED. `test_no_entry_at_or_after_flatten_time` (test_v2.py:179) only checks that 15:25 is blocked, never that 15:24:59 is allowed.
- Fix: add `test_no_entry_before_0921_entry_at_0921` and `test_entry_allowed_at_1524_59` (in my file; they kill M05–M08).

### [HIGH] The trail tests check the adaptive give-back the operator reverted; the live flat 6% has no test (test_execution_risk.py:85-145) — reproduced
- Four trail tests overwrite the executor with `ex.trail_give_pct_wide, ex.trail_give_pct_mid, ex.trail_give_pct = 0.12, 0.09, 0.06`. That is the 12/9/6 scheme which the yaml says was REVERTED (vajra_config.yaml:196-204). Their docstrings still call it an "operator rule".
- So the real rule, floor = max(+7%, peak × 0.94) with the yaml values, is never exercised.
- M12: yaml set to the forbidden 12/9 tiers. SURVIVED.
- M13: give-back 5% instead of 6%. SURVIVED.
- M11: `min_profit_pct` 0.09 instead of 0.07. SURVIVED.
- M09: `hard_sl_pct` 0.10 instead of 0.12. SURVIVED. The hard-stop tests use 35.0 (−12.5%), so any stop between 10% and 12.5% passes. There is no boundary test.
- Only M10 (15%) is killed, by `test_no_breakeven_exit`.
- Fix:
  - Delete the 12/9/6 overrides, or rename those tests "tier mechanism".
  - Add yaml-driven boundary tests: `test_trail_floor_is_flat_peak_x094_with_real_yaml` (47.1 holds, 46.9 exits from a peak of 50), `test_trail_floor_just_after_arming_is_the_7pct_minimum` and `test_hard_stop_boundary_minus_12pct` (35.3 holds, 35.2 exits).

### [HIGH] The cooldown tests bypass both the wiring and the duration (test_determinism_and_tapes.py:62, 155) — reproduced
- The tests set `o1._last_stop_ms = fire_ts - 10_000` and `_last_profit_ms` directly. They never produce a real SL_HIT or a profitable exit.
- They read the window length from the same yaml they are meant to check (`cool_ms = cfg.get(...)`), so the duration check is circular. The docstrings still say "180s" and "120s".
- `test_code_defaults_match_operator_yaml` (test_v2.py:298) only checks the code fallback after deleting the yaml keys. It never asserts the yaml values.
- M18: `post_stop_cooldown_secs: 60`. SURVIVED.
- M19: `post_profit_cooldown_secs: 120`. SURVIVED.
- M20: an SL_HIT exit no longer starts the 11-minute cooldown (orchestrator.py:792). SURVIVED.
- M21: a profitable exit no longer starts the 15-second cooldown (orchestrator.py:798). SURVIVED.
- Fix: add `test_sl_hit_starts_exactly_11min_cooldown` (a real −20% tick, blocked at +659 s, entry at +660 s) and `test_profit_exit_starts_exactly_15s_cooldown` (a real TRAIL_SL exit, blocked at +14 s, entry at +15 s). Both are in my file and kill M18–M21.

### [HIGH] "No daily trade cap / no loss stop" is only checked by method name (test_v2.py:237) — reproduced
- `test_no_daily_limits_anywhere` asserts `capital_fraction == 1.0` and `not hasattr(o, "_check_standdown")`. A cap written under any other name passes.
- M03: `elif self._entries_today >= 2: gate = ...`. SURVIVED. The real-fixture test only needs `>= 2` entries, and verify gate 7 only needs `>= 1` (verify_vajra.py:392).
- M04, a 3% loss stop, was killed only by accident: `test_decision_tape_records_reject_reason_not_fire` happens to set capital = 1,000.
- Nothing tests compounding across trades end to end. `test_capital_updates_on_close` only asserts `capital > 100_000`.
- Fix:
  - Add `test_no_trade_cap_and_no_loss_stop_after_many_losers`: six hard stops in a row, every entry still fires, and capital ends below ₹50k.
  - Add `test_all_in_compounds_through_the_day`: after a loss, the second entry's qty = floor((1e5 + pnl) / (fill × 65)) × 65.
  - Raise gate 7 and the fixture test to the exact recorded count, and assert each entry's qty is all-in.

### [HIGH] Nothing tests the re-anchor cadences through the orchestrator (orchestrator.py:401-427, freshness_engine.py:70) — reproduced
- `test_sister_levels_reanchor_faster_than_nifty` only reads the `hold_min_secs` attribute. Its docstring says "3 min -> 10 min" while the test asserts 180.
- The FreshnessEngine tests use 1800 s, not the operator's 1200 s.
- M25: NIFTY interval re-anchor disabled in the orchestrator. SURVIVED.
- M35: the engine treats 1200 as 1800. SURVIVED.
- M26: engine threshold floored at 600 s, so sisters re-anchor every 10 min instead of 3. SURVIVED.
- M23: the post-exit re-anchor deleted. SURVIVED.
- M24: the re-anchor when the profit cooldown ends deleted. SURVIVED.
- Fix: add `test_nifty_reanchors_every_20min_when_flat` (anchors exactly at 09:20, 09:40, 10:00), `test_sisters_reanchor_every_3min` (BANKNIFTY at 09:20, 09:23, 09:26), `test_post_exit_reanchor_is_written` and `test_profit_cooldown_end_reanchors`.

### [MEDIUM] The confluence no-veto rule and the 7-pt proximity have no test (orchestrator.py:693-712, 1064) — reproduced
- M29: an extra gate that blocks the entry when `confluence.opp_side` is non-empty. SURVIVED. All entry tests pass an empty `signals={}`, so the confluence logic is never exercised in an entry test.
- M28: `proximity_pts: 5`. SURVIVED. The approach tests use 4 pts and 3 pts, and "mid band" uses 8 pts, so anything from 4 to 7.99 passes.
- Fix: add `test_opposite_confluence_never_vetoes` (five sisters breaking against the trade, entry still fires) and `test_proximity_boundary_7pts` (no entry at 7.1 pts, entry at 7.0).

### [MEDIUM] Nothing asserts paper-only `LIVE_ORDERS: false` (verify_vajra.py gate 6 excludes it) — reproduced
- M33: `LIVE_ORDERS: true`. SURVIVED.
- In live mode a Breeze client is passed to the executor (app.py:133 → execution.py:77), so this one-character yaml change would route real orders.
- The startup banner prints "(paper)" whatever the flag says (app.py:150).
- Fix: assert `LIVE_ORDERS is False` in a test and in a verify gate. `test_yaml_carries_the_operator_rules` in my file pins every binding yaml value; it also kills M31, a `pad_floor` change in the locked expiry formula, which the golden tests miss because they pass `pad_floor=0.10` explicitly.

### [MEDIUM] Six DecisionMaker tests check a code path that no longer decides entries (test_engine_modules.py:80-141) — read in code
- `test_fire_on_nifty_confirm_plus_bias`, `test_reversal_freeze_blocks_fire`, `test_nonexpiry_gate_blocks` and similar call `DecisionMaker.evaluate`.
- In 2.0 (and 1.0) the orchestrator always overrides `dr.decision` with its own `decision_word` (orchestrator.py:1006). Entries come from `_approach_entry` plus the gate list at orchestrator.py:693-712, which has no reversal gate.
- So `test_reversal_freeze_blocks_fire` gives false assurance about a behaviour the live engine does not have.
- Fix: mark these as display-only tests or delete them, and put orchestrator-level gate tests in their place.

### [LOW] Stale or contradictory docstrings and names
- test_timing.py:275 and :315 are named "30min" and "0930".
- test_determinism_and_tapes.py docstrings say 180 s and 120 s.
- test_execution_risk.py:1 says "15% min".
- test_entry_path.py:100 says "3 min -> 10 min".
- These mislead anyone reading the tests to learn the rules. The rules live in the yaml; the tests should name the operator values.

### [LOW] The code default for the sister cadence is 600 s, not 180 s (orchestrator.py:234) — read in code
- `test_code_defaults_match_operator_yaml` was added to stop code fallbacks drifting from the yaml, but it omits `sister_hold_min_secs`.
- If that yaml key goes missing, sisters re-anchor every 10 min.
- Fix: set the default to 180 and add the key to that test.

### [LOW] The cooldown-end re-anchor only fires if some round lands inside the cooldown window (orchestrator.py:660-666) — reproduced
- The edge tracker `_profit_cooldown_active` is only set True by a round inside the window.
- My first version of `test_profit_cooldown_end_reanchors` jumped straight from the exit to +15 s and got no `post_cooldown` anchor.
- With live 4 Hz rounds this is harmless; it only matters for sparse replays or feed gaps longer than 15 s. The post-exit re-anchor still happens.

### Notes on monkeypatching
- `_fire_open` and `test_decision_tape_records_reject_reason_not_fire` (test_entry_path.py:272-310) monkeypatch `compute_band`, `_expiry_gate_ok` and `_approach_entry` together. They are valid tests of tape consistency, but they do not test the entry trigger, and they are the only orchestrator tests that open a position on an expiry day.
- `test_post_stop_cooldown_recomputes_fresh_levels_when_it_ends` turns `feed_sanity` off to stay green. That is acceptable, and the reason is documented in the test.

## Mutation testing (hand, 36 mutants, one rule broken per copy)
- Harness: `/home/claude/teardown/scratch/22/mutate.py`. Results: `/home/claude/teardown/scratch/22/mutants.jsonl` for the existing suite, and `mutants_t22.jsonl` in the same folder for my extra test file.
- Method: each mutant is a fresh copy of the snapshot with one exact-match edit, then the full suite runs with `pytest -x`.
- Equivalent mutants: M16 (a hard stop after the trail arms can never be reached) and M34 (in-trade hold over "open"). I count M22 as near-equivalent.
- Of the 34 meaningful mutants the existing suite **killed 9 and missed 25**:

| Mutant (rule broken) | 2.0 suite | + T22 tests |
|---|---|---|
| M01 half capital / M02 sizing ignores compounding | killed | killed |
| M03 cap of 2 trades/day | **survived** | killed |
| M04 3% daily loss stop | killed (incidental) | killed |
| M05/M06/M08 trade start 09:30 (code/yaml) / no start gate | **survived** | killed |
| M07 entry cutoff 15:00 | **survived** | killed |
| M09 hard stop 10% / M10 hard stop 15% | **survived** / killed | killed |
| M11 min profit 9% | **survived** | killed |
| M12 adaptive 12/9 give-back / M13 give-back 5% | **survived** | killed |
| M14 flatten 15:29 / M15 never flatten | **survived** | killed |
| M17 breakeven exit | killed | (killed by existing) |
| M18 stop cooldown 60 s / M19 profit cooldown 120 s | **survived** | killed |
| M20 SL doesn't arm cooldown / M21 profit doesn't arm cooldown | **survived** | killed |
| M23 no post-exit re-anchor / M24 no cooldown-end re-anchor | **survived** | killed |
| M25 no NIFTY interval re-anchor / M35 20→30 min / M27 hard 1800 | **survived** / **survived** / killed | killed |
| M26 sisters every 10 min | **survived** | killed |
| M28 proximity 5 pts / M29 confluence veto | **survived** | killed |
| M30 no breach-through entry | killed | (existing) |
| M31 pad 0.12 in locked formula / M32 expiry band via v2 | **survived** / killed (incidental) | killed |
| M33 LIVE_ORDERS true | **survived** | killed |
| M36 no 09:20 calc gate | survived | survived (LOW: trade gate still holds) |

- Existing suite: **9/34 killed (26%)**.
- Existing suite plus `test_t22_operator_rules.py`: **33/34 killed** (M36 is the only survivor).

## Missing tests, by priority
All of these exist in `/home/claude/teardown/scratch/22/test_t22_operator_rules.py` (19 tests, about 9 s). **All 19 pass on unmodified 2.0**, which confirms the engine follows each rule. Adopt them into `tests/`.
- **P0, before Monday:**
  - the 15:25 EXPIRY_FLATTEN inside `process_round`
  - 09:20:59 blocked / 09:21:00 entry
  - 15:24:59 entry allowed
  - an SL_HIT exit starts exactly 660 s of cooldown
  - a TRAIL_SL exit starts exactly 15 s of cooldown
  - the yaml rule-pin test (LIVE_ORDERS false, 0.12 / 0.10 / 0.07 / flat 0.06, 660 / 15, 09:21 / 15:25, 7 pts, 1200 / 180, pad 0.10)
  - an expiry-day (2026-10-19) NIFTY band equals K ± round(1.1·VWAP) − basis through the orchestrator
- **P1:**
  - the flat-6% trail and the +7% floor at their exact boundaries using the real yaml
  - the −12% hard-stop boundary
  - all-in compounding across trades
  - six losses in a row and still trading (no cap, no loss stop)
  - NIFTY 20-min and sister 3-min anchors at exact timestamps
  - post-exit and cooldown-end re-anchors
- **P2:**
  - opposite confluence never vetoes
  - the 7.0 / 7.1-pt proximity boundary
  - no hard stop once the trail is armed
- **Not written:** an SSE `/api/stream` delivery test and a dashboard sound-dedupe test. No pytest covers the "ticks without delay / no extra sounds" rule; only `tools/latency_bench.py` and the backend `SoundBus` unit tests do.

## What 2.0 does better than 1.0 (evidence)
- 24 new tests, from 77 to 101 collected (`pytest --collect-only`: 1.0 = 77). They cover the regime, MTM narrow-only bands, feed-sanity, the SoundBus rate limit and the approach hysteresis, NaN-safe `/api/state`, and the event schema.
- One real-data regression: `test_real_1015_fixture_v2_behaviour` (also verify gate 7).
- `test_regime_trading_dte_and_expiry_day` pins Monday 19 Oct as EXPIRY with T-3 / T-1 counted in trading days.
- `test_position_is_managed_even_when_band_fails` closes a real 1.0 gap.
- No 1.0 test was deleted. Two were edited:
  - `feed_sanity` is turned off in one cooldown test.
  - `test_feed_live` now drops two stocks from the step table so the runtime path is still exercised.

## What 2.0 does worse than 1.0 (evidence)
- The operator-rule tests are byte-identical to 1.0's (`diff` of test_execution_risk.py, test_entry_path.py and test_timing.py shows only one docstring change). Every gap above already existed in 1.0; 2.0 did not close any of them.
- `test_no_daily_limits_anywhere` gives a false sense of coverage (M03 survives it).
- Gate 7 only needs ≥1 entry, against a claimed 4.
- The 12/9/6 trail tests still present the reverted scheme as an operator rule.

## Tests / checks I ran
- Full 2.0 suite on a scratch copy: `python3 -B -m pytest -p no:cacheprovider -q` → `101 passed, 1 warning in 146.84s`.
  - Wall time is 130–265 s on the shared box. The slowest are `test_replay_is_deterministic` at 49 s and the fixture test at 34 s.
- `python3 -B verify_vajra.py` → `ALL GATES PASS`: 7/7 gates, gate 7 = "4 non-expiry entries".
- 1.0 suite: `--collect-only` gives 77 tests. I diffed each test file between 1.0 and 2.0.
- 36 hand mutants against the full suite, then against my new rule tests (see table).
- `test_t22_operator_rules.py` on unmodified 2.0 → `19 passed in 9.20s`.
