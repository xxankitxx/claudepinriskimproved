# 03 — Freshness / re-anchor cadence reviewer (§5.4): findings

Scope: `vajra/engine/freshness_engine.py` and every call site in `vajra/orchestrator.py`
(open calc, 20-min NIFTY / 3-min sister interval, post_exit, post_cooldown, stale-inputs guard,
retry_secs, seed_from_prev_close), the `recalc_trigger` / `freshness_age_secs` tape semantics and
the dashboard freshness bar. Evidence: the three recorded days (10-14/15/16), NIFTY chains
extracted from `tick_tape.parquet`, simulations in `/home/claude/vajra2_review/scratch/03/`
(`01_recomputes.py`, `03_drift_stats.py`, `04_policies.py`, output `04_out.txt`).

Headline (confirmed by running): **re-anchor cadence is second-order; band width is the binding
constraint off expiry.** With the production formula, holding the band 20 min vs 120 min vs
"until hit" changes the 3-day approach count from 2 to 4. Drift-triggered re-anchoring with a
threshold below the half-width makes it strictly worse (0 approaches). The correct 2.0 design is
"anchor to the chain, hold until the level is hit or a long time-cap, refuse implausible inputs",
with the 20-min clock kept only for expiry day where theta actually moves the band.

---
## 1. Correctness defects (most severe first)

### [CRITICAL] post_exit / post_cooldown re-anchor accepts garbage spot — all three 15:25 anchors are fiction  (orchestrator.py:720-750, :649, :565, :573)  certainty: confirmed-by-running
- What happens: `_recompute_nifty_band` only checks `stamp_ts == ts` (same round). It never checks
  that the inputs are *plausible*. On every recorded day the cash feed froze 15:15→15:20 (286 s /
  265 s / 306 s with spot unchanged while the implied future moved 15–19 pts) and resumed with
  one-round jumps of −57/+55, +199/+81/−180, +103/−56/+111 pts. The post_exit recomputes at
  15:25:00 anchored to basis −63.2 / −74.4 / −117.0; on 10-16 the 15:22:17 post_exit anchored to
  spot 23303 (real ≈23110), basis −188, ATM 23300, `K_CE=23250@39.60` (a strike the market priced as
  140-OTM all day), producing U=23483/L=23228, then the 15:22:32 post_cooldown re-anchored to the
  same garbage, and the 15:24:47 PE "entry" was taken against L=23222.7. Chain: garbage spot →
  garbage ATM → wrong ITM strikes → garbage level → garbage entry.
- Why it matters: the levels and the P&L at 15:20–15:25 on all three days are not evidence of
  anything; the re-anchor path is the point where a single bad tick becomes a *held* reference.
- Clean-window thresholds measured on the tapes (`03_drift_stats.py` part b/c): NIFTY weekly
  |basis| never exceeded 30 pts (p99 ≤ 4 pts premium / 26 pts discount), no one-round spot jump
  >25 pts, no frozen-spot run ≥20 s before 15:15. The 15:15+ garbage violates all three by 3–10×.
- Fix (one gate, applied to EVERY anchor site: interval, open, post_exit, post_cooldown, seed):
  ```python
  def anchor_inputs_plausible(snap, br, prev_spot, frozen_secs):
      if abs(br.basis) > max(40.0, 0.002 * snap.spot): return False, "IMPLAUSIBLE_BASIS"
      if prev_spot and abs(snap.spot - prev_spot) > 0.0015 * snap.spot: return False, "SPOT_JUMP"   # ~35 pts
      if frozen_secs >= 20: return False, "SPOT_FROZEN"
      ce, pe = snap.ce(br.atm_strike - step), snap.pe(br.atm_strike + step)
      # 1-ITM options must carry at least their intrinsic: a 53-pt ITM call at VWAP 39.6 is a lie
      if ce.ltp < 0.8 * (snap.spot - ce.strike) or pe.ltp < 0.8 * (pe.strike - snap.spot): return False, "ITM_BELOW_INTRINSIC"
      return True, None
  ```
  On failure: `mark_attempt(ts)`, `_levels_stale = True`, `FEED_GAP{stage: anchor, reason}`; keep the
  held band. Additionally: no anchor of any trigger after `entry_cutoff` (15:25) — it can never be
  traded, it only pollutes the tape and the dashboard; and no post_exit anchor within 5 min of the
  cutoff when the exit reason is EXPIRY_FLATTEN. Also pick ATM for the band from `pin.atm_by_future`
  (spec §5.2 says so; `compute_band` is fed `br.atm_strike = round(spot)`): the implied future comes
  from option LTPs, a different stream, and was correct during every freeze.

### [HIGH] Off expiry the band re-centres on spot every 20 min, so the level is structurally unreachable  (orchestrator.py:175-176, :305-325; vajra_config.yaml:71)  certainty: confirmed-by-running
- What happens: every re-anchor selects K_CE/K_PE relative to the *current* ATM, so the band is
  always spot ± half-width (p50 half-width 132/115/90 pts on the three days). Measured max
  excursion of spot from a 1-min-grid anchor (`03_drift_stats.py`, pooled 671 anchors, clean window):
  | hold | p50 | p90 | P(≥75) | P(≥100) | P(≥130) |
  |---|---|---|---|---|---|
  | 20 min | 29 | 51 | 0.03 | 0.01 | 0.00 |
  | 60 min | 44 | 82 | 0.13 | 0.04 | 0.02 |
  | 120 min | 59 | 105 | 0.37 | 0.14 | 0.03 |
  | 180 min | 62 | 126 | 0.43 | 0.25 | 0.08 |
  Reaching a 100-pt level inside a 20-min window is a 1-in-100 event even on the 475-pt trend day.
- Policy simulation on the real chains (`04_policies.py`, production width, 3 days, touch = within 7 pts):
  INT_20 2 touches · INT_60 2 · INT_90/120 4 · HIT (anchor once, re-anchor only on touch) 4 ·
  DRIFT_25 0 · DRIFT_50 0 · DRIFT_75 2 · DRIFT_150 4 · FIXK_20 (fixed strikes, level refreshed from
  current VWAP) 4. Of the 4 reachable touches only 1 followed through ≥20 pts in 10 min. So: a
  longer hold roughly doubles opportunities but nothing about cadence alone makes the system
  productive — the half-width must come down (R01).
- Fix: see §3A below (cadence rules). The cadence change is cheap and should ship with R01's width
  change, not instead of it.

### [HIGH] `recalc_trigger="post_cooldown"` is written but absent from `schema.RECALC_TRIGGERS`; the enum is not enforced anywhere  (orchestrator.py:565, :573; schema.py:23, :119)  certainty: confirmed-by-running
- 10-15 and 10-16 level tapes contain `post_cooldown` rows (2 and 1). `RECALC_TRIGGERS` is imported by
  nothing that validates; `verify_vajra.py` gate 2 checks column names/dtypes only. The spec §5.4/§9.2
  comment lists `{prev_close_seed|open|interval|post_exit|held}`.
- Fix: add `"post_cooldown"` (and `"retry"`, see below) to the enum, spec and README; make
  `_level_row` assert `trigger in RECALC_TRIGGERS`; add a verify-gate check that every distinct
  `recalc_trigger` value in the produced level_tape is in the enum (same pattern as the
  `UNDECLARED event types` check already in gate 2).

### [MEDIUM] The `open` trigger never fires for NIFTY; the label depends on seed availability, not on the session open  (freshness_engine.py:61-62; orchestrator.py:714)  certainty: confirmed-by-running
- `seed_from_prev_close` calls `mark_recomputed(seed_ts)`, so `_last_attempt_ms` is set and the 09:20
  (or restart) first calc is labelled `interval` with `freshness_age_secs` = 65 563 s (18 h, 10-15) or
  9 582 961 s (110 days — June seed, 10-14). `open` appears only for sisters whose *first attempt
  succeeded* (9/5/16 rows); a sister whose first band failed gets `interval` on the retry. The label
  therefore carries no information. Same for a successful retry after `mark_attempt`: labelled
  `interval`, which hides every "we were stale and recovered" episode from the tape.
- Fix: label by cause, not by clock state: `open` = first intraday calc of the session (whether or
  not a seed existed; detect via a `_had_intraday_calc` flag or `hhmm` ≥ calc_start and no prior
  intraday success), `retry` = success after ≥1 `mark_attempt`, `interval` = timer, plus
  `post_exit`/`post_cooldown`/`prev_close_seed`. Return the trigger from `update()` as today but let
  the orchestrator override to `retry` when `holding_stale` was true.

### [MEDIUM] `freshness_age_secs` has three different meanings on the tape  (orchestrator.py:329-332, :717-718, :749-750)  certainty: confirmed-by-reading + tape
- `held` rows: age of the held band (fine). `interval`/`open` rows: the *pre*-recompute age of the band
  being replaced (1200.1, or 9.58 M). `post_exit`/`post_cooldown`/`prev_close_seed` rows: hard-coded 0.0.
  An analyst cannot tell a 110-day-old seed from a fresh band without reading `recalc_trigger`, and
  the universe table shows "1200s" for NIFTY on the same round the hero panel shows "held 0s"
  (`instruments_view.freshness_age_secs` vs `_nifty_head.held_age_secs`, orchestrator.py:384 vs :402).
- Fix: on a recompute row write `freshness_age_secs = 0.0` and add a separate `replaced_age_secs`
  (or put the pre-age in the event payload); make `instruments_view` use the same post-recompute age.

### [MEDIUM] Seed rows carry another day's timestamp inside the day's partition  (orchestrator.py:716-718)  certainty: confirmed-by-running
- The 10-14 level_tape contains a `prev_close_seed` row stamped June 2026 15:30:05 (ATM 24050, 1450
  pts from the day's market); 10-15's partition has a row stamped 10-14 15:29:12. Spec §9: tapes are
  "partitioned by trade date"; sorting by `ts` puts a foreign-day row first. `STALE_PREV_CLOSE_SEED` is
  raised only for the June fallback ("STALE" in src_desc), not for a live tape that is several days
  old (long weekend / week off).
- Fix: stamp the seed row with the session's startup `ts` (keep the source ts in the event payload),
  and flag any seed whose source date is not the previous trading day per `ExpiryCalendar`.

### [MEDIUM] Interval timer re-anchors the band *mid-trade*, contradicting the stated "held band we entered on is the BIG-MONEY reference"  (orchestrator.py:306-320; spec §5.4)  certainty: confirmed-by-running
- 10-15: trade 10:00:14→10:08:19, `interval` re-anchor at 10:01:56 while in the position. The exit
  model does not read the band, so P&L is unaffected, but the dashboard, `_compute_alarm` and the
  level history all show a reference the operator did not enter on, and the post_exit clock then
  restarts from the mid-trade anchor.
- Fix: in `_process_instrument`, for NIFTY, skip the interval recompute while
  `executor.position_active` (defer; the post_exit recompute will follow anyway).

### [LOW] Post-profit cooldown (15 s) produces a second, near-identical anchor 15 s after post_exit  (orchestrator.py:571-574)  certainty: confirmed-by-running
- 10-15 10:08:19 post_exit → 10:08:34 post_cooldown (levels moved 3 pts, basis only). Harmless but
  pointless, and it resets the 20-min clock. Keep the post_cooldown recompute for the 660-s stop
  cooldown; skip it when the cooldown is shorter than, say, 120 s (or when the held band is < 60 s old).

### [LOW] `mark_attempt` after a failed post_exit flags a perfectly fresh held band as STALE  (orchestrator.py:726-743; freshness_engine.py:67-69)  certainty: confirmed-by-reading
- `holding_stale = _last_success_ms < _last_attempt_ms` is true after any failed attempt even when
  the held band is 2 min old; dashboard dims "STALE" and the retry fires every 30 s. Correct intent:
  stale = `age_secs > hold_min_secs` (or > some grace). Also `age_secs()` returns 0.0 when there has
  *never* been a success (`_last_success_ms is None`) — "never computed" reads as "fresh".
- Fix: `age_secs` → `float('inf')`/None when no success; `levels_stale` = failed-attempt AND
  age > hold (or no band). Keep the 30-s retry.

### [LOW] Two level_tape rows for NIFTY at the same `ts` on every post_exit/post_cooldown round  (orchestrator.py:329 + :749)  certainty: confirmed-by-running
- The `held` row (old band) is written in `_process_instrument`, then the recompute row at the same
  ts (2/8/6 duplicate rows). Any analysis that indexes level_tape by `(ts, instrument)` silently keeps
  one of them. Fix: when a same-round recompute happens, write the recompute row only (buffer the
  held row and drop it), or give rows a `seq` column.

### [LOW] Interval grid is anchored at engine start / last exit, not wall-clock  (freshness_engine.py:70)  certainty: confirmed-by-running
- Re-anchors landed at xx:06:07 (10-14), xx:08:34 after the 10:08 exit (10-15), xx:11:04 (10-16).
  The operator cannot predict them and cross-day replays are misaligned. Fix: snap the interval to
  a wall-clock grid from `calc_start` (09:20, 09:40, …) — `next_due = calc_start + ceil((t-calc_start)/H)*H`.

### [LOW] Dead parameter and unused argument  (orchestrator.py:720 `fallback_band`; freshness_engine.py:55 `spot`)  certainty: confirmed-by-reading
- `_recompute_nifty_band(ts, fallback_band, trigger)` never reads `fallback_band`; `update(ts, spot)`
  never reads `spot`. Remove both (or use `fallback_band` as the "keep the held band" return value).

### [LOW] Mid-day (re)start seeds from *yesterday's* close, never from today's own earlier session  (feed.py:187-188)  certainty: confirmed-by-reading
- `p.name < today` excludes today's partition; after the 3 failed connects on 10-16 (10:24, 10:28,
  10:46) the 11:10 start seeded from 10-15's close. The first round re-anchors immediately, so the
  cost is only a wrong pre-first-round display and the lost paper position (documented). If the
  operator deletes `tick_tape.jsonl` per the manual's disk advice, the seed silently falls back to
  the June recording (10-14). Fix: also read `level_tape.jsonl`/parquet of today for a same-day
  restart; fall back to the previous *trading* day's `level_tape` last row (tiny file) rather than
  the 6-GB tick jsonl.

---
## 2. Improvements / design proposals (ranked)

1. **Hold-until-hit with a time cap and a plausibility gate** (replaces the fixed 20-min clock off
   expiry). Rationale from the tapes: theta compresses a 1-ITM band by ≈2 pts/hour three days out
   (vs the whole ~30-pt extrinsic dying on expiry day), so the physical reason for a 20-min re-anchor
   does not exist off expiry; the writers' breakeven (K + 1.1·VWAP) does not move because 20 min
   passed. Pooled excursion stats show P(reach 75 pts) rises from 3 % (20 min) to 37 % (120 min).
2. **Scale the hold with days-to-expiry**, not a single constant: `hold = 20 min × clamp(dte, 1, 6)`
   is a reasonable first cut (expiry 20, T-1 40, T-2 60, T-3 … 120), measured on tape by the lead.
3. **Drift-triggered re-anchor only as an outward safety valve**: re-anchor when spot has moved
   *away* from both levels by > half-width (i.e. the band is now entirely on one side of spot's path
   and can only be reached by a reversal). Never re-anchor on a drift smaller than the half-width —
   DRIFT_25/50 produced zero approaches on every day.
4. **Do not use fixed strikes with a per-round VWAP refresh** (FIXK_1): the level chases spot (fixed
   deep-ITM VWAP rises as spot rises) and yielded 0–3 touches; FIXK_20/60 ≈ HIT. Not worth the
   complexity.
5. **Session-open / VWAP-reference anchoring** is already what HIT does in effect (the 09:20 band is
   held). Anchoring the *strikes* to the open ATM while refreshing VWAP is FIXK — rejected above.
6. **Interaction with R01's narrowed band (measured)**: with the half-width halved, every cadence gives
   ≈88 touches / 23 with ≥20-pt follow-through over 3 days, and on 10-15 71 of them are consecutive
   re-touches 09:47–10:05 as the re-centred band hugs the trend. A narrow band + re-centre-on-touch
   (post_exit) = chain re-entries along a trend. That is the behaviour the operator liked on expiry
   (54 trades, 06-30); off expiry it is whipsaw (26 % follow-through). Therefore the post_exit
   re-anchor off expiry should NOT re-centre on spot: re-anchor the *touched* side only after a
   hard stop (the level failed), and after a profitable exit keep the opposite level and move the
   touched level one strike outward (K ± step) instead of recomputing both around the new spot.
7. **Label and tape hygiene**: `open`/`retry`/`interval`/`post_exit`/`post_cooldown` by cause; one
   row per (ts, instrument); `freshness_age_secs` = post-recompute age; seed rows stamped with
   session ts; enum enforced in `_level_row` and in verify gate 2.
8. **Dashboard**: the freshness bar should show *stale* only when `age > hold` (not after any failed
   attempt), show `next re-anchor at HH:MM` (wall-clock grid), and when R03's hold-until-hit is on,
   show "held since HH:MM · until hit / cap HH:MM".

---
## 3. Recommendations specific to the operator asks

### A. Non-expiry levels — WHEN/HOW to re-anchor (R01 owns the width; these rules assume R01 narrows it)
Concrete VAJRA 2.0 cadence rules, all measurable on the three tapes with the lead's harness:
1. `freshness.mode: expiry → fixed_interval(20 min)`; `nonexpiry → hold_until_hit` with
   `time_cap_min = 20 × clamp(days_to_expiry, 1, 6)` (T-1 40, T-2 60, T-3 120 is the first grid to sweep).
2. Re-anchor triggers off expiry, in priority order: (a) plausibility-gated **post hard-stop** (the level
   failed → recompute both sides); (b) **post profit-exit** → recompute only the opposite side, move the
   hit side outward one strike (no re-centre); (c) **time cap**; (d) **outward drift**: spot farther than
   half-width from the *midpoint* in the direction away from both levels for > 10 min; (e) **feed
   recovery** after a stale episode (30-s retry, labelled `retry`).
3. Never re-anchor: while a position is open; after `entry_cutoff`; on inputs failing the gate in §1
   (|basis| > 40, one-round spot jump > 0.15 %, frozen spot ≥ 20 s, 1-ITM LTP < 0.8·intrinsic).
4. Choose ATM for the strikes from the implied future (`pin.atm_by_future`), not raw spot.
5. Snap interval anchors to the wall-clock grid from 09:20; sisters keep 3 min (their bands only feed
   the advisory confluence; nothing in the tapes argues for changing it).
6. Expected effect (production width, 3 days): approaches 2 → 4; with R01's narrower band the
   cadence choice is neutral on count but rule 2(b) is what stops the 71-touch trend-hugging loop.

### B. Sounds — outside this role's scope beyond one coupling: a re-anchor must never make a sound.
With hold-until-hit the band changes rarely; a quiet "levels re-anchored" visual (history row +
bar reset) is sufficient. No audio on `interval`/`retry`/`post_cooldown`.

---
## 4. Things that are fine and should NOT be changed
- The two-clock design (`_last_success_ms` for age, `_last_attempt_ms` for re-trigger) and the 30-s
  `retry_secs` cadence: correct, tested (`test_failed_reanchor_does_not_phantom_refire`), and it
  prevents the old every-round phantom recalc. Keep it; fix only the stale *label* (§1 LOW).
- The stale-inputs (`stamp_ts != ts`) guard: correct and necessary; it is just not sufficient (§1 CRITICAL adds content plausibility on top).
- "Levels are NOT recomputed at entry" and "recompute after each exit" as *principles*: keep; §3A only changes what the post-exit recompute does off expiry.
- 20-min fixed interval **on expiry day**: justified by theta and tuned on the 06-30 tape; keep it.
- Pre-open seed from the previous close (operator rule a): keep, with the timestamp/staleness fixes.
- Sister 3-min cadence: the tapes show it running exactly (gap 180 s min/max); leave it.
- `FreshnessEngine` being a pure function of `ts` (deterministic replay): keep; the plausibility gate
  must also be a pure function of the snapshot stream so replay stays byte-identical.

---
## 5. Tests I would add
1. `test_anchor_refuses_implausible_inputs`: snapshot with basis −120 / one-round spot jump of 180 /
   1-ITM call LTP below intrinsic → no band change, `mark_attempt` called, `FEED_GAP{stage:anchor}`.
   Replay the 10-16 15:20–15:25 slice through the orchestrator and assert zero `post_exit` rows.
2. `test_no_reanchor_after_entry_cutoff`: round at 15:25:00 with an open position exiting → no
   `post_exit` level row.
3. `test_no_interval_reanchor_while_in_position`: open a position at 10:00, advance 21 min → no
   `interval` row until after the exit.
4. `test_recalc_trigger_values_in_enum`: run the synthetic session, assert every distinct
   `recalc_trigger` ∈ `RECALC_TRIGGERS` (verify-gate counterpart too).
5. `test_first_intraday_calc_is_labelled_open_even_when_seeded`; `test_retry_after_failed_attempt_is_labelled_retry`.
6. `test_seed_row_is_stamped_with_session_ts_and_old_seed_is_flagged` (seed source date ≠ previous trading day → ANOMALY).
7. `test_freshness_age_is_zero_on_every_recompute_row` and `age_secs is None/inf before any success`.
8. `test_one_level_row_per_ts_per_instrument` on a post_exit round.
9. `test_hold_until_hit_mode` (new): no `interval` recompute before the cap; recompute after a
   touch/exit; cap honoured; expiry day still uses the 20-min clock.
10. `test_wall_clock_grid`: engine started 13:26 re-anchors at 13:40, 14:00, … not 13:46.
