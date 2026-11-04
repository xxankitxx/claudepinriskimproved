# 21 — R21 Test-suite reviewer: findings

**Evidence base.** `pytest` = 77 passed (60–68 s on this box, not 12 s). `coverage --branch`: **73 % total**
(orchestrator 86, execution 87, feed 58, breeze 35, app 28, dashboard 0, realfeed 0). I also built and ran
(all under `/home/claude/vajra2_review/scratch/21/`, nothing under `vajra2/` touched):
- `extract_fixture.py` + `fx/` : two **real** NIFTY-only replay fixtures cut from `tapes/2026-10-15` (1.3 MB + 0.23 MB).
- `parity_check.py` : replays them with current code and compares to the recorded tapes -> **bit-exact** (orders, anchors, 6,479 + 3,804 per-round decision/reason pairs, P&L +6,233.50 and +3,022.50).
- `proposed_tests/` : `test_r21_v1_gaps.py` (57 tests: **49 pass, 8 strict-xfail = confirmed v1 defects**) and `test_r21_v2_spec.py` (38 pass against a reference prototype `ref_impl.py`). `cd proposed_tests && python3 -m pytest -q` -> `89 passed, 8 xfailed` in 11 s.
- `feed_stats.py`, `sanity_fp_scan.py`, `cap_replay_probe.py`, `anchors.json` : the measurements behind every number below.

---
## 1. Correctness defects (most severe first) — product defects the tests exposed, then suite defects

### [HIGH] NaN VIX anywhere in `latest_state` makes `/api/state` return 500 -> cockpit "frozen"  (breeze.py:106,268; feed.py:355; dashboard.py:32)
certainty: **confirmed by running** (`test_api_state_survives_nan_vix`, strict-xfail)
- `_normalize_tick` runs `normalize_vix(None)` for a VIX tick with no `last` -> `nan`; `_build_snapshot` guards `is not None` (nan passes); `instruments_view["vix"]`/`nifty.vix` carry the nan; starlette `JSONResponse` is `allow_nan=False` -> `ValueError`. `_on_ticks` carry-forward never replaces a nan (`not nan` is False).
- Fix: `normalize_vix` returns `None` for None/non-finite; `_build_snapshot` uses `math.isfinite`; dashboard serialises with a `_clean()` that maps non-finite -> None.

### [HIGH] Previous close leaks in as LTP  (breeze.py:268 `last or ltp or close`)
certainty: **confirmed by running** (`test_tick_without_last_must_not_use_prev_close_as_ltp`, strict-xfail)
- A quote tick lacking `last` (or `last==0`, i.e. option not yet traded) becomes `ltp = close` = yesterday's close. For the NIFTY cash tick that is a fake spot; for an option it is a fake premium used for the band, basis and the entry fill. Combined with A2 this is a plausible contributor to the garbage-feed trades.
- Fix: never fall back to `close`; treat `last<=0` as "no tick"; keep `close` in its own field.

### [MEDIUM] Zero bid/ask is "carried forward" -> stale ask used as the entry fill  (breeze.py:257-260; execution.py:70-76)
certainty: confirmed by running (`test_zero_depth_must_not_resurrect_stale_ask`, strict-xfail)
- `if not norm.get(fld) and prev.get(fld)` cannot tell "field absent" from "book empty (0)". `OrderWrapper.place` fills at `q.ask`. Fix: carry forward only when the key was *absent* from the payload; store a per-key `recv_ts` (also needed for stale-option detection, §3C).

### [MEDIUM] Only `holidays_2026` exists; every 2027 session silently resolves with *no* holidays  (config yaml, app.py:19-28)
certainty: confirmed by running (`test_calendar_refuses_or_flags_years_without_a_holiday_list`, strict-xfail). Tue 26-Jan-2027 is Republic Day -> engine would say "expiry Tuesday". Fix: `build_calendar` raises/emits HOLIDAYS_UNVERIFIED when `session_date.year` has no list; add the 2027 NSE list.
(All of 2026 is *fine*: I swept 365 days: 52 expiry days, exactly one per trading week, 5 holiday-Tuesday Mondays [30-Mar, 13-Apr, 19-Oct, 9-Nov, 23-Nov], 12 monthlies ending 29-Dec; the shipped tests only touched September.)

### [MEDIUM] Schema enums are documentation only; two are already wrong
certainty: confirmed by reading + by recorded tapes (`level_tape` on 10-15 contains `post_cooldown` x2; `decision_tape.reason_code` can be `PREMIUM_TOO_THIN/INSUFFICIENT_CAPITAL/NO_QUOTE/ORDER_PLACEMENT_FAILED`, orchestrator.py:613).
- `RECALC_TRIGGERS` lacks `post_cooldown`; `schema.Reason` lacks the 4 execution reject reasons. Only `event_type` is enforced in `TapeWriter.event`; `append` never checks enum columns. Fix: add the values + validate enum columns in `TapeWriter.append` (raise in tests, log in live). Test: `test_every_written_enum_value_is_declared` (strict-xfail now).

### [MEDIUM] Code-literal defaults disagree with the yaml (a typo'd/removed key silently changes behaviour)
certainty: confirmed by AST scan. `trade_start` 09:30 vs 09:21 (orchestrator.py:92), `post_stop_cooldown_secs` 180 vs 660 (:95), `post_profit_cooldown_secs` 120 vs 15 (:97), `trail_give_pct_wide/mid` 0.12/0.09 vs 0.06 (execution.py:118-119), `nonexpiry_mode` observe vs trade_small (config.py:107), `poll_interval_secs` 1 vs 0.25. Fix: single source of defaults (load yaml defaults, no literals in code). Tests: `test_every_config_read_in_code_has_a_yaml_key`, 28 parametrised `test_operator_locked_config_values_are_pinned`.

### [MEDIUM] Profit-cooldown "window ended" recompute only fires if a round was seen *inside* the window  (orchestrator.py:567-574)
certainty: confirmed by running (my first draft of the profit test jumped past the window and got no `post_cooldown`). The edge flag `_profit_cooldown_active` is set on the round *after* the exit; a feed gap > `post_profit_cooldown_secs` skips the fresh-levels guarantee. Fix: derive "recompute due" from `_last_profit_ms + window <= ts and not _recomputed_for_exit`.

### Suite defects (what the 77 tests do NOT protect) — coverage-proven
Never executed by any test (orchestrator.py unless stated): `_expiry_gate_ok` non-expiry branches **223-228** (tests monkeypatch it, e.g. test_entry_path:262,295); SL_HIT -> `_last_stop_ms` + POST_STOP_COOLDOWN event **636-642** (the cooldown tests *inject* `_last_stop_ms` by hand); profit-cooldown recompute **573**; REVERSAL_WATCH emission **522-525**; FALSE_BREAK emission **343-345**; EXECUTION_CAUTION_WINDOW **531-535**; `EXIT_REJECTED` **633**; PREMIUM_TOO_THIN banner **1019-20**; seed-success path **713-717**; stale `manage_quote` FEED_GAP **627**. execution.py: **15:25 `EXPIRY_FLATTEN` via `manage()` (227)**, live-order path 78-85, 1-ITM strike mode 141. `feed.py` 58 % (`VWAPTracker` 40-51, `_snapshot_from_tick_row`, `prev_close_snapshot`, `_build_snapshot` 352-378, the live `rounds()` loop 381-430 except 1 test), `breeze.py` 35 % (`_on_ticks`, `_normalize_tick`, `snapshot_raw`, orders), `app.py` 28 %, `dashboard.py` 0 %, `realfeed.py` 0 %.
- **Every integration test is an expiry-day test.** `_orch()` uses 2026-06-23 (Tue expiry); `choose_synthetic_session` *always* returns an expiry day; synthetic spot path is built for a thin expiry-day band. There is no orchestrator test on a non-expiry day — the structural root of complaint A. My `test_nonexpiry_mode_gate_end_to_end` is the first.
- **No feed-failure tests at all** (frozen spot, garbage jump, stale option, NaN VIX, zero ticks). Recorded A2 happened on all 3 days without a single red test.
- **No real-data test.** Nothing reads `tapes/`. `test_replay_is_deterministic` compares replay-vs-replay of a synthetic day (a tautology for any deterministic function); *recorded-vs-replayed* parity — the property the lead relies on — is untested (now: `test_replay_parity_real_entry_1000`).
- `monkeypatch`ed-away subject under test: `test_decision_tape_records_reject_reason_not_fire` and `_fire_open` patch **both** `compute_band` and `_approach_entry` (so only the glue is tested); `test_instrument_exception_is_isolated_not_fatal` patches `compute_basis` (ok: that *is* the injected fault); `test_recompute_refuses_stale_inputs` patches `compute_band` to raise (ok).
- **Stale/brittle**: `test_timing.py` hard-codes 1800 s and "09:30" (production is 1200 s / 09:21) — it tests the generic mechanism but its names/comments are wrong and `test_trade_gate_0930_and_calc_gate_0920` only tests `hhmm_reached`, i.e. it would pass if `in_session` were deleted; `test_post_profit_cooldown_blocks_reentry_silently` uses a fixed 10 s offset (`fire_ts - 10_000`) so it passes only while the config value is > 10 s (15 s today); the three cooldown tests locate "first entry" by scanning a synthetic day (order-dependent; breaks if the synthetic path changes); `test_golden.py` duplicates verify_vajra gate 3; `test_nonexpiry_gate_blocks` and 6 of 8 DecisionMaker tests exercise a path the orchestrator no longer uses to trade (the entry is `_approach_entry`; `DecisionMaker` output is audit/confluence only — see orchestrator.py:665 "dead DecisionMaker HOLD").
- Suite takes ~60 s because 4 tests each run a full synthetic session through parquet; make the recorded session a `scope="session"` fixture (-> ~15 s).

### Per-test map (77 tests -> behaviour protected; V = verdict)
| file (n) | behaviour protected | V |
|---|---|---|
| test_breeze_codes (4) | `ws_expiry` locale-safe; VIX x100 fix; index codes; connect retries + clean error | good |
| test_expiry (4) | holiday-Tue->Mon; monthly ACTUAL; monthly holiday shift; Wed -> next week (all Sept only) | good, thin |
| test_golden (5) | round-half-up trap; implied future/basis; band premium/discount; ACTUAL future | good (dup of verify gate 3) |
| test_timing (4) | FreshnessEngine 1st calc + interval; seed forces re-anchor; failed re-anchor no phantom refire; `hhmm_reached` | mechanism ok; **stale constants; last = tautology** |
| test_engine_modules (13) | basis weight decay/noise/abrupt; continuation breach->confirm, false-break; DecisionMaker fire/hold/freeze/divergence/gate | good for modules; DM tests = dead path; `test_nonexpiry_gate_blocks` = **old philosophy** |
| test_entry_path (23) | seed never writes 0.0 level; 7-pt approach/breach-through/no-signal (synthetic 24100 snapshots); sister cadence 1200/180 pin; confluence absolute pull; reversal direction; NaN/exception isolation; in-trade confluence direction; premium floors; capital reject; expiry ITM fallback; decision_tape reject reason; finalize flatten; stale-input recompute; event-strip RLE | strongest file; but monkeypatches `_approach_entry`/`compute_band`/`_expiry_gate_ok` in 4 |
| test_execution_risk (13) | force_flatten no phantom; lot 65; full-capital sizing; trail arm +10 %/min +7 %; 3 give-back tiers (they *override* the instance attrs to 12/9/6 — yaml is 6/6/6, so the shipped flat-6 config is never what is tested); tier never loosens; no breakeven; hard stop; capital update | good logic, **config-divergent** |
| test_feed_live (7) | weekly-vs-monthly expiry per instrument; step retry; grid correction; unmapped name tape-durable; missing weight tape-durable | good (all fake-breeze, no real payloads) |
| test_determinism_and_tapes (9) | breach_ts; stop/profit cooldown; seed crash; levels_stale recovery; parquet backup; all tapes populated; replay==replay | good; synthetic-only, expiry-only |

---
## 2. Improvements / design proposals (ranked)
1. **Commit the two real fixtures** (`tests/fixtures/real_2026-10-15/{entry_1000,frozen_1520}`, 1.5 MB, built by `extract_fixture.py`) + `expected.json`. Start every fixture on a *recorded anchor round* so the first replayed band equals the recorded one (only difference: first anchor's label `open` vs `interval`). Add a third from 10-16 (post_stop path if one exists) and, once recorded, an expiry-day one.
2. **Scenario harness instead of monkeypatching** — `r21_helpers.py` (`chain(spot, du=, dl=, atm_ce=)` builds a snapshot whose §5.3 band lands exactly `du`/`dl` points from spot; `CapWriter`; `make_orch(day, **cfg_overrides)`; `feed()`). 12 of my tests drive real `process_round` end-to-end with it; no patching.
3. **Coverage gate**: `coverage --branch --fail-under` per file: orchestrator >= 90, execution >= 95, expiry/engine >= 95, feed >= 80, breeze >= 80 (needs the raw-tick tests), dashboard contract tests. Exclude `credentials.py`, `realfeed.py`, `__main__` blocks explicitly.
4. **Raw-tick recorder**: `BreezeWrapper._on_ticks` should append the first ~50 raw payloads per routing key (and 1 in 500 afterwards) to `tapes/<d>/raw_ticks_sample.jsonl`. Today there is **no real websocket payload anywhere in the repo** (`logs/websocketLogs.log` holds only subscribe acks), so every normaliser test I wrote uses SDK-documented shapes (certainty: plausible). Replace them with captured payloads after one session.
5. **Validate enums at write** (`TapeWriter.append`) and make `verify_vajra` Gate 2 run a *non-expiry* synthetic day too.
6. **One funnel for band construction.** `compute_band` is called from 3 sites (`_process_instrument` 311, `seed_from_prev_close` 703, `_recompute_nifty_band` 732). Any v2 band transform (cap) must live in one `_make_band()`; test every site (T-A9 below).
7. Session-scope the synthetic recording fixture; add `-p randomly`-style order independence check; `hypothesis` is not installed — I used seeded loops (`test_cap_never_widens_idempotent_and_keeps_spot_inside`, 5,000 cases).
8. Dashboard: node 22 is present here, `node --check` on the inline JS passes (43 KB); add that test + the `/api/state` contract test + "every sound file referenced exists and is served" (all in v1_gaps, passing).

---
## 3. Recommendations for the two operator asks — the VAJRA 2.0 test plan
Legend: **[R]** runnable today (in `proposed_tests/`, validated against `ref_impl.py` or v1); **[O]** orchestrator-level, needs v2 code, expected values given.

### A. Non-expiry band narrowing  (parameters + golden numbers)
**Hard evidence the band is asymmetric about spot** (so "half-width around spot" must be a *per-side* clamp, not `(U-L)/2`): at anchors, distance spot->L / spot->U was 66.8/208.2 (10-14 13:26), 108.4/156.6, 173.6/71.4 (10-15 10:01, trend day), 76.0/89.0 (10-16 15:11). Source: `anchors.json` (level_tape + basis_tape).
**Proposed rule (what the tests pin):** `cap = round_1pt(min(60, 0.25 % * spot))`; `U' = min(U, spot+cap)`, `L' = max(L, spot-cap)`; non-expiry days only; never widens; applied inside the single band funnel at every (re)anchor incl. post_exit/post_cooldown/seed; level_tape keeps raw levels (`upper_raw/lower_raw`) + `cap_applied`. Do **not** round the cap to the 5-pt step: that makes it jump 55->60 at spot 23,000 (I hit this in the golden test). Config validation: reject `cap < 2*proximity_pts` (14) or `cap <= proximity + break_buffer`.
| id | name | fixture | assertion |
|---|---|---|---|
| A1 [R] | `test_cap_golden_on_real_anchors` (7 rows) | real (spot, L, U) | e.g. 10-14 13:26: spot 22584.20, v1 L 22517.4 / U 22792.4 -> cap 56, L' 22528.20, U' 22640.20; 10-15 10:01: 22861.05 -> cap 57, L' 22804.05, U' 22918.05; 10-16 15:11: 23095.55 -> cap 58, L' 23037.55, U' 23153.55 |
| A2 [R] | `test_cap_only_the_wide_side_moves` | 24110, 40/20 vs 140/20 away | narrow side untouched, flags `lower_capped/upper_capped` correct |
| A3 [R] | `test_cap_is_noop_on_expiry_day_and_when_disabled` | wide band | levels identical |
| A4 [R] | `test_cap_pct_vs_pts_crossover` | spot 20k/24k/30k | cap 50 / 60 / 60 |
| A5 [R] | `test_cap_never_widens_idempotent_and_keeps_spot_inside` | 5,000 seeded random bands | `U'<=U, L'>=L, L'<spot<U'`, 2nd application is identity |
| A6 [R] | `test_cap_config_validation_rejects_cap_inside_the_entry_zone` | (10,0.25), (60,0.01) | `ValueError` |
| A7 [O] | `test_cap_disabled_replays_v1_bit_exact` | fx `entry_1000` with `band_cap.enabled=false` | same assertions as `test_replay_parity_real_entry_1000` (guards the regression-parity of old tapes) |
| A8 [O] | `test_cap_replay_characterisation_entry_1000` | fx `entry_1000`, cap on | measured with `cap_replay_probe.py`: ENTRY 09:47:33 NIFTY 22750 CE fill 153.25 -> TRAIL_SL 09:53:03 +7,442; ENTRY 10:01:00 22800 CE 173.6 -> END_OF_SESSION +702 (v1: single trade 10:00:14 @196.0, +6,233.5). **One window, in-sample: pin it as a characterisation test, not as evidence of edge.** |
| A9 [O] | `test_cap_on_every_band_site` | `chain(du=200, dl=200)`, non-expiry day; drive seed, interval, post_exit, post_cooldown | every non-`held` NIFTY `level_tape` row has `upper-spot<=cap` and `spot-lower<=cap`; `upper_raw`>`upper` recorded |
| A10 [O] | `test_expiry_day_band_unchanged_on_orchestrator` | `make_orch(day=EXPIRY_DAY)` | level_tape equals v1 |
| A11 [O] | `test_sister_levels_not_capped_by_nifty_points` | BANKNIFTY chain | sisters use pct only (or are untouched) — decide & pin |
| R1 [R] | `test_regime_golden` (8 rows) | `ExpiryCalendar` from yaml | tdte: Wed 14-Oct=3 `T2_3`, Thu 15=2, Fri 16=1 `T1`, Mon 19=0 `EXPIRY`, Wed 21-Oct=4 `T4P`, Thu 2-Apr=2 (Good Friday inside window), Fri 10-Apr=1 (Tue 14-Apr holiday -> Mon 13), Fri 27-Mar=1 |
| R2 [R] | `test_regime_counts_down_by_exactly_one_per_trading_day_within_a_week_of_2026` | whole 2026 | `tdte==0 <=> is_expiry_day`; consecutive trading days share an expiry -> tdte decreases by 1 |
| R3 [O] | `test_regime_event_and_dashboard_badge` | orchestrator | `EXPIRY_RESOLVED` payload carries `tdte`, `regime`; `/api/state.expiry.tdte` (the JS computes it client-side today from calendar days: wrong across weekends/holidays — "T-5" shown for Wed->Mon) |

### C. Feed-sanity gates  (thresholds are outside every legit value in 3 days, proved by full-day scans)
Measured on all 3 days, 09:21–15:15 (`feed_stats.py`): longest identical-spot run **16.0 s** (10-16); largest legit `|basis|` **28.5** (10-16); largest per-round `|dbasis|` **15.8** (10-15); longest ATM CE&PE volume-unchanged **20.4 s** (10-15). The 15:15 freeze: **264.7 s** frozen at 22939.95, ATM CE changed 193 times during it, then spot 23138.85 / basis -204.2 in one round (10-15). Proposed: `FROZEN_SPOT` = spot unchanged >= 45 s **and** >= 3 option-price changes; `IMPLAUSIBLE_BASIS` = `|basis|>45`; `BASIS_JUMP` = `|dbasis|>40` in one round; `STALE_OPTION` = ATM CE&PE volume unchanged >= 60 s while spot moved >= 10; un-latch only after **90 s** clean **and** `|basis|<=25` (30 s left an 8 s hole on 10-14 between two garbage episodes). **Result of `sanity_fp_scan.py` over the full recorded days: zero flags before 15:15 on all three days; each day trips only inside the 15:15+ window and stays latched to EOD (10-14: single span 15:16:00–15:28:21).**
| id | name | fixture | assertion |
|---|---|---|---|
| C1 [R] | `test_sanity_trips_on_the_real_1515_freeze_and_stays_latched` | fx `frozen_1520` | first not-ok in 15:16:30–15:16:50 with `FROZEN_SPOT`; never ok again |
| C2 [R] | `test_sanity_blocks_the_very_round_of_the_garbage_jump_even_without_a_freeze_history` | same + cold-start gate | 15:20:11 round has `IMPLAUSIBLE_BASIS` and `BASIS_JUMP`; cold start on (23138.85, -204.2) is not ok |
| C3 [R] | `test_sanity_zero_false_positives_on_a_clean_real_window` | fx `entry_1000` | no non-ok verdict in 6,479 rounds |
| C4 [R] | `test_sanity_thresholds_are_outside_every_legit_value_seen_in_3_days` | constants | margin >= 1.5–2x on the four legit maxima |
| C5 [R] | `test_stale_option_needs_spot_to_have_moved` | synthetic 130 s | stale only if spot moved >= 10 |
| C6 [R] | `test_sanity_unlatches_only_after_clean_period_and_basis_normal` | synthetic | blocked while basis -60; released exactly after `clear_secs` |
| C7 [O] | `test_frozen_spot_then_garbage_jump_must_not_trade` (**exists now, strict-xfail on v1**) | fx `frozen_1520` (+`execution.starting_capital=106233.5`) | no ENTRY in the window; `FEED_GAP{reason=FROZEN_SPOT}` once; decision_tape reason `FEED_UNSAFE`; open positions are *managed* but not entered; flatten still at 15:25 |
| C8 [O] | `test_sanity_does_not_change_recorded_trades_before_15_15` | fx `entry_1000` | parity test A7-style still passes (gate off-path) |
| C9 [O] | `test_nifty_stale_vs_chain_gate_blocks_entry` | `chain()` + ATM volume frozen 60 s | decision `HOLD/STALE_OPTION`, no ENTRY, ENTRY_REJECTED event once |
| C10 [O] | `test_gate_events_are_edge_triggered` | 4 Hz for 5 min of freeze | exactly 1 FEED_GAP on trip + 1 on release |

### Last-entry cutoff (~15:10)
`test_last_entry_edge` **[R]** already reads `execution.last_entry_before` (falls back to `flatten_before`), asserts entry at cutoff-1 s and none at cutoff (passes on v1 at 15:25 today; flips automatically when the key appears). Add **[O]**: `test_position_opened_before_cutoff_is_managed_until_flatten` (open 15:09:59, still trails/stops until 15:25:00 `EXPIRY_FLATTEN`), `test_entry_blocked_between_cutoff_and_flatten_is_logged` (decision reason `AFTER_LAST_ENTRY`, once per episode, not per round). Evidence for 15:10 over 15:25: all three recorded 15:20 "trades" were fiction (A2); legit-feed trades' P&L after 15:10 are not in the recorded days, so the cutoff value itself is a lead call, the test only pins the mechanism.

### trade_small sizing
Today `nonexpiry_mode: trade_small` only opens the gate (`_expiry_gate_ok`) and `Executor.compute_lots` sizes at 100 % of capital (execution.py:174). Proposed `size_mult = {EXPIRY:1.0, T1:0.5, T2_3:0.35, T4P:0.25}` (operator to set).
| id | name | assertion |
|---|---|---|
| D1 [R] | `test_size_multiplier_golden_100k_ask_60_5` | 25 / 12 / 8 / 6 lots (floor of 25.43, 12.71, 8.90, 6.36) |
| D2 [R] | `test_size_is_monotone_in_regime_and_never_exceeds_capital` | cost <= capital*mult; non-decreasing toward expiry |
| D3 [R] | `test_size_never_forces_a_lot_it_cannot_afford` | 0 lots -> REJECT `INSUFFICIENT_CAPITAL` (v1 rule preserved) |
| D4 [O] | `test_trade_small_actually_sizes_down_end_to_end` | `feed()` on 14-Oct, ENTRY qty == `lots_for(...)*65`; **v1 characterisation: qty = floor(100000/(60.5*65))*65 = 1,625** (documents that `trade_small` does not size down today) |
| D5 [O] | `test_mult_is_taken_from_session_regime_not_wall_clock` | replay same fixture with `session_date` forced to EXPIRY -> 4x lots |

### B. Sounds — move the policy to the backend so it is testable (the JS at dashboard.py:454-474, 968-989 has **zero** tests; the `ting-ting` comes from `a.lean`/`state` flipping at the 7-pt boundary and per-instrument `nb` sets)
Proposed policy (4 kinds; operator trims): `ENTRY`, `EXIT_PROFIT`, `EXIT_LOSS`, `CRITICAL` (feed unsafe / exit rejected / stale levels while in trade). Approach and sister-breach sounds are dropped (visual only). Orchestrator owns `SoundQueue`; `/api/state.sounds = {seq, events[{seq,kind,key,ts,text}]}`; JS plays only events with `seq > lastSeq`; first load sets `lastSeq = seq` (a page reload never replays history).
| id | name | assertion |
|---|---|---|
| B1 [R] | `test_sound_once_per_episode_until_ended` | same (kind,key) emitted once; re-armed after `end_episode` |
| B2 [R] | `test_sound_per_kind_and_global_rate_limit_but_critical_bypasses` | 2 s global, 10 s per kind, CRITICAL exempt, `suppressed` counter |
| B3 [R] | `test_sound_flicker_flood_makes_exactly_one_sound` | 1,351 REVERSAL edges -> 1 sound |
| B4 [R] | `test_sound_client_cursor_semantics` | `since(seq)` returns only newer; `since(seq_now)==[]` |
| B5 [R] | `test_sound_queue_is_bounded` | ring of N, `seq` keeps counting |
| B6 [O] | `test_one_entry_sound_per_trade` | `feed()` open + 3 more rounds -> `ENTRY` x1; the post-stop re-entry later -> a 2nd |
| B7 [O] | `test_exit_sound_kind_follows_realised_pnl` | TRAIL_SL pnl>0 -> `EXIT_PROFIT`; SL_HIT -> `EXIT_LOSS`; `EXPIRY_FLATTEN` by sign |
| B8 [O] | `test_false_break_flood_replay_makes_no_sound` | fx `entry_1000` (12 FALSE_BREAK in 23 s) | only the ENTRY + EXIT sounds exist (2 total) |
| B9 [O] | `test_api_state_sounds_contract` | TestClient | keys/types; JSON-clean; `seq` monotone across rounds |
| B10 [O] | `test_critical_sound_on_feed_trip_once_per_episode` | C7 fixture | exactly 1 CRITICAL for the 15:16–15:28 episode |
| B11 [R] | `test_every_sound_file_referenced_by_the_page_exists_and_is_served` | passing today; update the set when files change |
| B12 [R] | `test_dashboard_inline_js_parses_with_node` | passing today |

### Event-flood hysteresis (A3)
Rule: edge + clear-hysteresis (condition False for `clear_secs=30`) + `min_gap_secs=120` for REVERSAL_WATCH; FALSE_BREAK `min_gap 30 s`. **Measured on the recorded `event_tape`s: REVERSAL_WATCH 1,351 -> 335/189/101/42 at gap 30/60/120/300 s (10-15); 821 -> 184/102/54/22 (10-16); FALSE_BREAK 12 -> 1 at every gap >= 30 s.** Caveat: the fixture replay cannot pin REVERSAL counts (NIFTY-only fixture: 60 vs 168 recorded; sister-driven `bias_ewma` differs) — pin REVERSAL via the event-tape timestamps or the debouncer unit, but pin FALSE_BREAK on the fixture (it reproduces **exactly**: 12 events at 10:01:20…10:01:43).
| id | name | assertion |
|---|---|---|
| F1 [R] | `test_debounce_swallows_flicker_but_not_a_new_episode` | 20 s of 4 Hz flicker -> 1 fire; after 60 s clean but < min_gap -> 0; after >=120 s and re-armed -> 1 |
| F2 [R] | `test_debounce_on_recorded_reversal_watch_timestamps` | 1351 -> 335/189/101/42 (reads the real tape; skipped if absent) |
| F3 [R] | `test_false_break_12_in_23s_collapses_to_one` | real timestamps -> 1 |
| F4 [O] | `test_false_break_flood_is_reproduced_on_real_data` (**exists, pins v1 = 12**) -> v2 flips to `<=2` | fx `entry_1000` |
| F5 [O] | `test_event_tape_keeps_suppressed_count` | the binding tape records `suppressed=N` on the next emitted event (do not silently drop audit data; the strip-only RLE of v1 keeps all rows) |

### Schema enum completeness
`test_every_written_enum_value_is_declared` [R, strict-xfail on v1]: drives profit exit + post_cooldown + thin-premium reject through the real orchestrator and asserts `recalc_trigger ⊆ RECALC_TRIGGERS`, `reason_code ⊆ Reason`, `exit_reason ⊆ {SL_HIT,TRAIL_SL,EXPIRY_FLATTEN,END_OF_SESSION} ∪ Reason`. v2 additions that must be declared at the same time: `AFTER_LAST_ENTRY`, `FEED_UNSAFE`, `STALE_OPTION`, `FROZEN_SPOT`, new tape columns. Add **[O]** `test_every_tape_value_on_recorded_days_is_declared` (scans the 3 real `level_tape/decision_tape/event_tape` — cheap, parquet column read) so an old-tape drift is caught.

### Replay-vs-recorded parity (the extraction script)
`scratch/21/extract_fixture.py <tapes/day> <out_root> <name> <anchor HH:MM:SS.mmm> <end HH:MM:SS> [starting_capital]`:
reads `tick_tape.parquet` with row-group filters (`ts` window + `instrument='NIFTY'`; **never** `.to_pylist()` the whole file — I OOM'd a 3-day scan doing that; use `iter_batches`), writes a tiny parquet in the original schema, copies the recorded `EXPIRY_RESOLVED` event (otherwise `replay.py` falls back to the calendar), and snapshots `expected.json` = recorded orders + NIFTY non-held anchors + per-round `(decision, reason_code)`.
Two rules that make parity exact: (1) **start on a recorded anchor round**; (2) **carry the day's capital** (`--set execution.starting_capital=106233.5` for the 15:20 fixture, else qty 910 vs recorded 975).
Delivered fixtures: `entry_1000` (09:41:55.975–10:09:00, 6,479 rounds, 1.31 MB: ENTRY 10:00:14.501 22750 CE @196.0 x455 -> TRAIL_SL 10:08:19.124 @209.7 +6,233.50, post_exit 10:08:19 + post_cooldown 10:08:34 anchors) and `frozen_1520` (15:08:35.767–15:26:00, 3,804 rounds, 0.23 MB: ENTRY 15:20:11.758 22950 CE x975 -> EXPIRY_FLATTEN 15:25:00.327 +3,022.50). `test_replay_parity_real_entry_1000` [R, passes] asserts orders, P&L, anchors, all 6,479 decisions.
Add **[O]** `test_replay_parity_after_v2_with_all_v2_features_disabled` (must equal the same expected.json) and `test_replay_parity_v2_enabled_is_deterministic` (twice, byte-equal `decision_tape` — v2's debouncer/sanity/sound queue must be pure functions of tape ts; **no `time.time()`** inside them).

### Other v1-gap tests I wrote (all in `test_r21_v1_gaps.py`, passing)
`test_no_entry_one_second_before_trade_start_entry_at_trade_start` (09:20:59.750 vs 09:21:00.000, edges read from yaml) · `test_pre_calc_start_rounds_are_ignored_entirely` (09:19:59.9: no tape rows) · `test_nonexpiry_mode_gate_end_to_end[trade_small|observe|off]` + `test_expiry_day_ignores_nonexpiry_mode` · `test_sl_hit_arms_post_stop_cooldown_and_recomputes_twice` · `test_profit_exit_is_silent_and_recomputes_when_window_ends` · `test_flatten_before_close_books_EXPIRY_FLATTEN_with_live_quote` · `test_flatten_with_dead_quote_holds_then_finalize_rejects_loudly` · 3 `VWAPTracker` tests (feed preferred, delta-weighted computed, volume-reset clamp; `test_vwap_computed_is_session_vwap_only_since_process_start` documents that a mid-session restart yields a since-restart VWAP tagged "computed" — the operator restarts mid-day [A4], and the band is built from this VWAP) · normaliser tests (option tick key `NIFTY|option|22900|CE`, VIX x100 only on VIX, unmapped name once) · calendar sweeps and golden tables · `test_api_state_shape_is_stable_and_json_clean`.

---
## 4. Things that are fine and should NOT be changed
- Determinism design (tape-ts decay, sorted instrument order, no wall clock in the decision path): **proved** on real data — 27-min and 17-min NIFTY-only replays reproduce the recorded day bit-exactly. Keep `replay.py` semantics; v2 must stay pure.
- The existing good tests: phantom-0.0 flatten guard, seed-never-writes-0.0, NaN/exception isolation, in-trade confluence direction, `test_failed_reanchor_does_not_phantom_refire`, trail tier "only tightens", parquet-backup-on-restart, monthly-vs-weekly expiry per instrument. Keep and extend.
- Operator-locked values (20 min / 3 min / 09:20 / 09:21 / 7 pts / 660 s / 15 s / +7 % +10 % 6 % / -12 % / 45 & 30 / lot 65 / paper-only): pinned in `test_operator_locked_config_values_are_pinned` so any change is a deliberate two-file edit.
- `test_*` using `_NullWriter`/`_CapWriter` rather than disk: fine; keep.
- Calendar logic for all of 2026 is correct (swept).

---
## 5. Tests I would add — checklist for the dev team (order of work)
1. Copy `scratch/21/{extract_fixture.py,fx/,proposed_tests/}` into `tests/` (set `R21_FIXTURES`, `VAJRA_ROOT`); run -> expect `89 passed, 8 xfailed`.
2. Fix the 8 xfail defects one by one; each fix makes its strict-xfail XPASS-fail, forcing removal of the marker (self-cleaning): NaN VIX, `close`-as-ltp, zero-ask carry, 2027 calendar, enum drift, code-default drift, `/api/state` NaN, frozen-spot trade.
3. Implement v2 behind `v2api.py`'s seam; unit tests A1-A6, R1-R2, C1-C6, D1-D3, B1-B5, F1-F3 are already green on the reference — they become the acceptance spec. Then write the **[O]** tests (A7-A11, R3, C7-C10, D4-D5, B6-B10, F4-F5) using `make_orch/chain/feed`.
4. Coverage gate + session-scoped synthetic fixture; delete/repoint: `test_nonexpiry_gate_blocks` (old binary-gate semantics), the 30-min/09:30 constants in `test_timing.py`, and the monkeypatched `_expiry_gate_ok` in `test_entry_path.py`.
5. Add the raw-tick recorder, capture one session, replace SDK-shaped payloads in the normaliser tests with real ones.

Files: `/home/claude/vajra2_review/scratch/21/{extract_fixture.py,parity_check.py,feed_stats.py,sanity_fp_scan.py,cap_replay_probe.py,anchors.json,fx/}`, `/home/claude/vajra2_review/scratch/21/proposed_tests/{r21_helpers.py,test_r21_v1_gaps.py,test_r21_v2_spec.py,ref_impl.py,v2api.py}`.
