# 29 — Synthetic data, fixtures & scenario reviewer: findings

Scope: `vajra/synthetic.py`, `vajra/realfeed.py`, `vajra/data/`, `verify_vajra.py` inputs, tests that consume synthetic data.
Everything marked "confirmed-by-running" was executed from `/home/claude/vajra2_review/scratch/29/` (read-only import of `/home/claude/vajra2`). Nothing under `/home/claude/vajra2` was modified.

## 0. Prototype files and how to run them (all in `/home/claude/vajra2_review/scratch/29/`)

| file | what | run |
|---|---|---|
| `nonexpiry_synth.py` | `generate_rounds(cfg, date, ..., regime="nonexpiry", dte_trading=3, iv=0.12, basis_pts=-8, path="range60"\|"flat", scenario=None\|"frozen_spot_spike")`. Black-76 off the implied forward, T=(dte+frac_of_day_left)/252, running-mean VWAP (vwap != ltp), sisters/stocks on the monthly chain, deterministic (no RNG/clock). `regime="expiry"` delegates to the original. | imported |
| `print_band_widths.py` | prints v1 `compute_band` widths on the synthetic non-expiry chain (table in section 3) | `python3 -I print_band_widths.py` |
| `run_scenarios.py` | v1 Orchestrator + NullWriter on: A) range60 non-expiry (v1), B) same with the band capped at +-40/50/60 (monkeypatched `compute_band`, prototype), B0) flat path, C0/C/D) frozen-spot + garbage-spike artefact without/with a prototype `FeedGuard`. Output saved in `run_scenarios.out` | `python3 -I run_scenarios.py` (~40 s) |
| `make_real_fixture.py` | streams the 285 MB `tapes/2026-10-15/tick_tape.parquet` in batches, keeps NIFTY+BANKNIFTY+FINNIFTY, 09:41-10:15 and 15:10-15:30, first round of each second (1 Hz), zstd-9. **9,552 rows, 2.0 MB** -> `fixture/2026-10-15/tick_tape.parquet` (+ the `EXPIRY_RESOLVED` event line so `replay._read_expiry_from_events` works) | `python3 -I make_real_fixture.py [SRC_DIR] [OUT_DIR]` (works on any recorded day, incl. Mon 19 Oct once recorded) |
| `run_v1_on_fixture.py` | loads every fixture row through `feed._snapshot_from_row` (9,552/9,552 OK), runs the v1 `Orchestrator` with a null writer through `ReplaySource`, asserts the known 10:00 approach | `python3 -I run_v1_on_fixture.py` |
| `seed_from_parquet.py` | prototype prev-close seed from the parquet tail (0.1-0.2 s) | `python3 -I seed_from_parquet.py [tape_dir]` |
| `test_r29_proposed.py` | 8 proposed regression tests (all pass, 19 s) | `python3 -m pytest -q test_r29_proposed.py -p no:cacheprovider` |

**Real-fixture result (confirmed-by-running):** v1 on the 1 Hz fixture reproduces the recorded day: 09:41:56 `open` anchor (lower 22616.75 / upper 22861.75), `APPROACH_ALERT` UP at **10:00:15** (spot 22855.65, dist 6.1), `ENTRY NIFTY 22750 CE @196.0`, `TRAIL_SL` exit 10:08:19 @209.7 (+6,233.5, identical to the full tape), then the 15:20:12 garbage-spot entry `22950 CE @106` and the 15:25:00 flatten (+3,022.5, identical to the full tape). The 0.35-pt level difference vs the full tape (22862.1) and the 1 s timestamp shift come from 1 Hz sampling; tests must assert behaviour, not bytes.

---

## 1. Correctness defects (most severe first)

### [HIGH] The synthetic simulator, hence verify gates 2/5 and ~8 tests, only exercise the expiry-day regime; non-expiry behaviour has zero automated proof  (synthetic.py:8-10, 59-62, 140-178; verify_vajra.py:213-243; tests/test_determinism_and_tapes.py:26,46,70)  certainty: confirmed-by-running
- Facts: `choose_synthetic_session` always returns an *expiry* day (`is_expiry_day=True`). `_t_years` = 150-270 min to expiry. Running the original generator through v1: implied basis is **+-0.01 pt for the whole session** (r=0 put-call parity, so BasisMonitor sees nothing and `abrupt`/REVERSAL_WATCH can never fire); v1 band half-width is **10-17.5 pts** (real expiry days 25-35; real non-expiry 90-132); VWAP == unrounded price (never lags ltp); bid/ask is a fixed +-0.4 regardless of premium.
- Why it matters: complaint A is a non-expiry failure (half-width 90-132, 0-6 minutes inside the trigger zone). Any fix (band cap, sizing, new gates) would ship with every gate green and no test that fails before / passes after. The suite cannot detect a re-introduction of the idle-day bug.
- Fix: merge the `nonexpiry_synth.generate_rounds` regime switch into `synthetic.py` (default stays expiry so existing tests are untouched), add the tests in section 5 and verify gates 7-9 (section 2.3).

### [HIGH] Entry strike is taken from the *anchor-time* ATM, not the live ATM  (orchestrator.py:596-602 -> execution.py:184 `select_strike(direction, band.atm_strike, step)`)  certainty: confirmed-by-running (real fixture)
- On the real 10-15 tape the band was anchored at 09:41 (ATM 22750). At 10:00:15 spot was 22855 (ATM 22850) and the engine bought **22750 CE @196**: a 105-pt ITM option (premium ~196 vs ~120 for ATM), using an ATM up to 20 minutes stale. The cockpit (`_compute_alarm`, orchestrator.py:1009) shows `BUY NIFTY <live ATM>` (22850) while the paper fill and `APPROACH_ALERT.buy` use the stale ATM (22750): the operator is told one strike, the tape books another. Same on expiry day, just hidden by thin extrinsic.
- Fix: pass the current ATM (`int(round_to(snap.spot, step))`) to `executor.enter`; keep `band.atm_strike` only as level provenance. Fixture test: after the 10:00 approach `order.strike == 22850`.

### [HIGH] Gate-2/5 synthetic session ends at 13:37 and never reaches any late-session code path  (synthetic.py:140-141 defaults `n_rounds=150, dt_secs=15, start_hhmm="13:00"`)  certainty: confirmed-by-reading
- 150 x 15 s = 37.5 min. Never reached: 14:00 basis-weight decay, 14:30 `EXECUTION_CAUTION_WINDOW`, the 15:15-15:20 artefact window, the 15:25 `entry_cutoff` / `EXPIRY_FLATTEN` (only a unit test covers flatten, by monkeypatching). Round cadence is 15 s vs the live 0.25 s (4 Hz), so `confirm_secs`, cooldowns and EWMA time constants are validated at a cadence production never runs.
- Fix: allow `start_hhmm="09:20"`/`"14:10"` and `dt_secs` 1-5 s; keep the 15 s fast path for unit tests. Scenario C in `run_scenarios.py` runs 14:10-15:30 at 5 s in ~10 s.

### [MEDIUM] Frozen-cash-feed artefact is reproducible and unguarded, with a *second* contamination (post-exit re-anchor on garbage)  (orchestrator.py:649, 720-750)  certainty: confirmed-by-running (scenario C0)
- C0 (synthetic, v1, no guard): spot frozen 15:15:00-15:20:00, then +240 garbage with options flat -> `ENTRY 24100 CE @123.26` at **15:20:00**, flatten 15:25:00: same shape as all three recorded days. After the exit `_recompute_nifty_band` re-anchors on the garbage snapshot (basis -240): level-history half-widths jump 120 -> 162.5 (`post_exit` + `post_cooldown`). A later trade would use a nonsense band.
- Real evidence for a threshold (basis_tape, NIFTY IMPLIED): before 15:14 the basis was within **[-29.8, +16.5]** on all three days (p1..p99 = -26..+4); after the freeze it ranged -283..+116. A bound of |basis| <= 45 pts has zero false positives on 3 days and catches every garbage tick. Longest unchanged-spot runs were 286 s / 265 s / 306 s (starting 15:15:02-15:15:46), so a 60 s frozen-spot detector ("ATM option mid moved >= 3 pts while spot unchanged") fires ~1 min into the freeze.
- Fix (prototype `FeedGuard` in `run_scenarios.py`, result D: 0 entries): (a) flag NIFTY untrusted when |implied basis| > 45, or spot frozen >= 60 s while options move; (b) **do not drop the whole round**: the prototype's "drop NIFTY" makes `process_round` skip `manage()` and `finalize` flatten on the last good round (stale exit). Implement inside the orchestrator as `spot_trusted=False` that blocks *entries and re-anchors* only, while the open position keeps being managed from option quotes; (c) one edge-triggered `FEED_ARTEFACT` event; (d) never re-anchor while untrusted; (e) a last-entry cutoff (e.g. 15:00) separate from the 15:25 flatten.

### [MEDIUM] Prev-close seed reads only `tick_tape.jsonl` and the **last** NIFTY row, which on all three days is post-garbage  (feed.py:187-213)  certainty: confirmed-by-reading + running
- `days = [... if (p/"tick_tape.jsonl").exists()]`. The delivered tape folders hold only `tick_tape.parquet` (sidecars pruned), so the "own live tape" seed branch is silently skipped the day the sidecar is archived; the fallback needs `real_session_*.jsonl`, which **does not exist in the repo** (see the realfeed finding) -> `prev_close_snapshot()` returns None -> `SESSION_ERROR ... CRITICAL`. Independently, the last NIFTY row of 10-15 has spot 22,997 and of 10-16 23,200: the corrupted spot, so the seed band carries the garbage basis.
- Impact is bounded (seed levels live only until 09:20; the first intraday round always re-anchors because the seed's `mark_recomputed` is a day old), so operationally LOW; it explains the 10-14 `STALE_PREV_CLOSE_SEED` noise.
- Fix: `seed_from_parquet.py` reads the last <=40 row groups (0.1-0.2 s), skips rows with |basis| > 45, prefers the last row before 15:14. Drop the jsonl dependency.

### [MEDIUM] `ReplaySource` materialises the whole day: `table.to_pylist()` of ~1.9 M rows with nested strike lists, then sorts  (feed.py:90-97)  certainty: plausible (not run; full-day replays are forbidden)
- Several GB of Python dicts for a 285 MB parquet; the jsonl branch is worse. The review box has 8 GB and my own naive `to_pandas()` of a 57 MB tape beside it was OOM-killed once. The lead's streaming harness exists, but `replay.py`/`ReplaySource` (what the gates use) is not streaming.
- Fix: `pf.iter_batches(batch_size=4096)`, group consecutive equal `ts` without a global sort (sort only inside a round by instrument; keeps byte-identical determinism). `make_real_fixture.py` shows the pattern.

### [LOW] `realfeed.py` and `vajra/data/` are dead/broken in this repo  (realfeed.py:16-166, synthetic.py:25-34, app.py:96-99, demo_dashboard.py:17-25)  certainty: confirmed-by-running
- `vajra/data/` holds only `real_nifty_day.csv` (187 rows). **Nothing reads it**: synthetic.py defines `_DATA`, imports `csv`/`Path` and its docstring claims calibration to it, but never opens it. `real_session_*.jsonl` (needed by `RealSessionSource`, `--mode real`, `demo_dashboard.py`, feed.py seed fallback) is absent -> `FileNotFoundError` for `--mode real`/`demo_dashboard.py`, `None` from the seed fallback. `RealSessionSource` is referenced only at feed.py:218-224 (seed fallback), app.py:97 (`--mode real`, not in `run_vajra.ps1`'s ValidateSet) and demo_dashboard.py:17.
- Other dead code: `RealSessionSource._chain`'s Black-Scholes sister/stock branch (contradicts its own docstring "we do not invent a chain"; unreachable because `rounds()` yields only NIFTY); `realfeed.bs_price` duplicates `synthetic.bs_price` with a different `r` (0.065 vs 0); `verify_vajra._run_synthetic` (verify_vajra.py:213) is never called; config keys `feed.real_round_stride` and `feed.real_session_date: "2026-06-16"` (a date with no data).
- **Proposal: retire `realfeed.py`, `real_nifty_day.csv`, `--mode real`, the two config keys and the seed fallback branch.** Point `demo_dashboard.py` at `ReplaySource(tapes/<day>)` or the committed 2 MB fixture (same `Orchestrator` path). Remove `_DATA`/`csv`/`Path` from synthetic.py and fix its docstring. Net -166 lines, one fewer path that degrades silently.

### [LOW] Determinism audit of the synthetic path: clean, two fragilities  certainty: confirmed-by-running
- `synthetic.py` is pure: no RNG, no wall clock, no set iteration; `_phase()` is an `ord()` hash (not Python `hash()`, so PYTHONHASHSEED-independent). My strike-level determinism test passes (two generations incl. vwap equal) and `test_real_fixture_replay_is_deterministic` passes.
- Fragility 1: session choice depends on `vajra_config.yaml` holidays (`choose_synthetic_session(cal, date(2026,6,1))`); editing holidays silently changes the synthetic day, expiry flag and every test's data. Pin the date.
- Fragility 2: `_snapshot_from_row` does `float(row.get("vix", 0.0))`, which raises `TypeError` if a parquet row has `vix = NULL` (key present, value None). Use `float(row.get("vix") or 0.0)`.

### [LOW] Synthetic sister/stock chains use the weekly T; live they quote the monthly chain  (synthetic.py:156-157 vs feed.py:140-147)  certainty: confirmed-by-reading
- Live: only NIFTY has a weekly; BANKNIFTY/FINNIFTY/stocks are monthly-only. Synthetic prices all 23 instruments on the same thin-extrinsic T, so sister bands (3-min re-anchor) are unrealistically tight and the confluence logic sees signals more eager than real ones. `nonexpiry_synth` uses T = dte+6 trading days for non-NIFTY.

---

## 2. Improvements / design proposals (ranked)

1. **Commit the real fixture** (`tests/fixtures/2026-10-15/tick_tape.parquet`, 2.0 MB, plus the one `EXPIRY_RESOLVED` line) as the primary correctness gate: binding data beats synthetic. It contains a legitimate approach + TRAIL_SL win (10:00-10:08), FALSE_BREAKs (10:01), a re-anchor, and the full 15:10-15:20 frozen-cash artefact incl. the garbage entry: both a happy path and the A2 failure. After Mon 19 Oct (first live *expiry* tape) run `make_real_fixture.py` on it and commit an expiry fixture the same way.
2. **Regime-aware synthetic** (`regime`, `dte_trading`, `scenario=`): prototype `nonexpiry_synth.py` (~110 lines). Expiry stays default so the 77 existing tests remain identical.
3. **Scenario library** (each = generator + assertion): `nonexpiry_range60` (must trade once the band is narrowed), `nonexpiry_flat` (must stay idle), `frozen_spot_spike` (0 entries, 1 `FEED_ARTEFACT`, no re-anchor on garbage). Suggested additions: `trend_day_475pt` (one entry, trail books), `whipsaw_at_level` (the 12x FALSE_BREAK flood: <=1 event per episode), `nifty_gap_during_position` (position still managed).
4. **Verify-gate additions** (2.3 below). 5. Streaming `ReplaySource` + parquet-tail seed. 6. Retire `realfeed.py`.

### 2.3 Proposed verify_vajra.py gates
- **Gate 7 real-tape fixture:** `ReplaySource(tests/fixtures/<day>)` -> NullWriter Orchestrator; first entry CE at 10:00:1x, anchors 09:41/10:01, identical decision rows across two runs, parquet < 5 MB. Replaces the "recorded or synthetic" ambiguity in spec 11.2 with a concrete binding input.
- **Gate 8 non-expiry productivity:** `regime="nonexpiry", path="range60"` -> >= 1 entry and every entry's `|level - spot_at_anchor| <= cap`; `path="flat"` -> 0 entries; `is_expiry_day` False in the resolved calendar.
- **Gate 9 feed artefact:** `scenario="frozen_spot_spike"` -> 0 entries after the freeze starts, a `FEED_ARTEFACT` event, max |basis| of every anchored band < 60, and any open position still managed and flattened at 15:25.
- Housekeeping: delete `_run_synthetic`; gate 1 must not scan `vajra/data`.

---

## 3. Recommendations specific to the two operator asks

### A (non-expiry levels): evidence from the synthetic chain (`print_band_widths.py`)
v1 `compute_band` at 09:40 on the synthetic non-expiry chain (spot ~24155). REAL lead numbers: 10-14 (3 tdays) 132, 10-15 (2) 115, 10-16 (1) 90, expiry 25-35.

| dte (trading days) | IV 0.12 / 0.13 / 0.14: lower-spot | upper-spot | half-width |
|---|---|---|---|
| 3 | -164 / -174 / -189 | +116 / +131 / +141 | **140 / 152 / 165** |
| 2 | -144 / -154 / -164 | +96 / +106 / +116 | **120 / 130 / 140** |
| 1 | -119 / -129 / -134 | +71 / +76 / +86 | **95 / 103 / 110** |
| 0 (expiry morning, IV 0.12) | -89 | +36 | 62 |

Default IV 0.12 with T=(dte+frac_left)/252 reproduces the real 90-132 band. The band is also *asymmetric around spot* on a drifting day (running VWAP lags: ITM-CE VWAP < price while rising, ITM-PE VWAP > price): upper +116, lower -164 at dte 3. Recentring on the implied future rather than the VWAP-based level would help; the cap below sidesteps it.

Scripted path `range60` stays within +-58 pts for 2 h (09:20-11:20, 15 s rounds), about 55 pts per 20 min, a quiet drift well below the 475-pt trend day:

| run | anchor half-widths | entries |
|---|---|---|
| **A. v1 uncapped** | 137.5-140 | **0** (idle-day bug reproduced; asserted) |
| B. cap 40 | 40 | 3 (09:33 CE, 10:07 PE, 10:34 CE) |
| B. cap 50 | 50 | 3 (09:35 CE, 10:06 PE, 10:35 CE) |
| B. cap 60 | 60 | 2 (09:39 CE, 10:38 CE) |
| B0. flat path (+-3 pts), cap 50 | 50 | 0 (idle, correct) |

Cap prototype: `upper = min(upper, spot+cap)`, `lower = max(lower, spot-cap)` at anchor time (monkeypatched `compute_band`, no repo edit). **Recommended default: cap = clamp(0.21% x spot, 45, 60), about 50 pts on NIFTY 24k**, symmetric around *spot* (levels live in spot space, `dist_up = upper - spot`), natural v1 band kept when narrower. On expiry day the natural half-width is 10-35, so the cap is inert and expiry behaviour stays bit-identical. Rationale: with proximity 7, +-50 needs a 43-pt move inside the 20-min window, which the recorded days (190/475/303-pt ranges) supply often; +-60 already cost a trade on this path; +-40 sits so close that the 10:01 FALSE_BREAK flood (12 events) gets worse.

**Honest caveat:** every B trade above was an `SL_HIT` loss (-8.9k to -12.3k on Rs 1 L). `range60` is a mean-reverting oscillation, the worst case for breakout anticipation; it proves the *gate opens*, not that an edge exists. The edge must be measured on the three real tapes by the lead's harness. Pair the cap with real non-expiry sizing: config says `trade_small`, yet `deploy_full_capital: true` sizes all-in at ~Rs 160 premium, so one stop is about -12% of capital (outside my scope, visible in these results).

### B (sounds)
Out of scope. One test-data note: the synthetic generators emit zero REVERSAL_WATCH (basis +-0.01), so a sound policy keyed on that event cannot be demoed from `demo_dashboard.py`; the 2 MB fixture can (10-15 had 1,351 of them).

---

## 4. Things that are fine and should NOT be changed
- Determinism discipline: no RNG/clock in `synthetic.py`, fixture replay byte-stable over two runs. Keep it a hard rule for new generators (`sin` sums, `ord()` phases; never `random`/`hash()`).
- `_snapshot_from_row` loads all 9,552 real fixture rows without error; the tick_tape parquet schema (per-row strike list incl. `vwap_source`) is a good replay contract.
- The synthetic *expiry* generator stays the default for existing tests (cooldown, breach_ts, flatten tests rely on its 3-trade script). Do not retune it; add a regime beside it.
- Real tape as the only *binding* evidence (operator rule); the fixture strengthens it.

## 5. Tests I would add (implemented in `test_r29_proposed.py`, 8 pass; "documents" = passes on v1 today and flips when the fix lands)
1. `test_synth_nonexpiry_is_deterministic`: two generations equal at strike level incl. vwap.
2. `test_synth_nonexpiry_band_is_wide_like_real`: v1 half-width in 120-160 / 100-140 / 80-115 for dte 3/2/1.
3. `test_v1_idle_on_nonexpiry_range60_path` (documents): flip to >= 1 entry when the cap lands.
4. `test_cap50_trades_on_range60_but_not_on_flat`: target behaviour of the cap.
5. `test_artefact_unguarded_v1_enters_on_garbage_documents_A2` (documents): entry exactly at 15:20:00.
6. `test_artefact_guard_blocks_entry_and_post_exit_reanchor`: 0 entries, no anchored basis < -60.
7. `test_real_fixture_loads_and_v1_reproduces_1000_approach`: CE 22750 at 10:00:1x, fixture < 5 MB (expect 22850 after the stale-ATM fix).
8. `test_real_fixture_replay_is_deterministic`.
Not yet written: `test_entry_strike_uses_live_atm` (fixture), `test_parquet_tail_seed_skips_garbage_basis`, `test_replaysource_streams_in_bounded_memory` (peak RSS < 500 MB), `test_no_orphan_data_files` (every file in `vajra/data/` is referenced), a gate test that gates 8/9 fail when the cap/guard is removed.
