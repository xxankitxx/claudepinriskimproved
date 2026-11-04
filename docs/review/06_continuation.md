# 06 — R06 Breach/Continuation validator + per-instrument state machine: findings

Method: read the whole codebase; rebuilt NIFTY validator inputs offline (spot + held band from level_tape + chain volume from tick_tape) and re-ran the ORIGINAL `continuation_validator.py` (copy in `scratch/06/cv_orig.py`). Replica matches decision_tape NIFTY state/strength **100.0000 % on 10-14 and 10-15, 99.997 % on 10-16** (the 2 diffs are the post-exit recompute rows sharing a ts; harmless). Scripts: `scratch/06/harness.py, 04_replay_orig.py, 05_episodes.py, 06_sister_bands.py, 07_vol.py`. Certainty tags: [run] = confirmed by running, [read] = by reading.

## 0. Headline
1. The validator's NIFTY output drives **nothing** that trades (entry is `_approach_entry`; DecisionMaker's decision is overwritten). Its live roles: sister/stock confluence "voice", burst board, breach-alert + sister sound, FALSE_BREAK anomaly.
2. **Every CONFIRM state on all three days (NIFTY 339 + 505 rounds, 10-15/10-16) happened at 15:20 on the broken cash feed**; the only genuine NIFTY level episode (10-15 10:00–10:02) produced 0 confirmations and 12 false-break pulses. [run]
3. The sister voice is ~dead: levels are 1–2.5 % of spot wide, `linger` is 0.05 %. HDFCBANK/ICICI/BANKNIFTY/FINNIFTY/SBIN/AXIS... are non-NEUTRAL 0.00–0.2 % of rounds; the instruments that ARE non-neutral (BANKBARODA 25–89 %, ITC 65 %, BHARTIARTL 34 %, IDFCFIRSTB 15 %) are degenerate-band artifacts, and they are the "channels_in_agreement" recorded on 3 of 6 signals. [run]
4. The volume gate is vacuous (latch within 2 s in 95 % of cases) and mostly measures ATM-window roll artifacts. [run]

## 1. Correctness defects (most severe first)

### [HIGH] No hysteresis / no bar close: tick-level threshold flip causes FALSE_BREAK floods  (continuation_validator.py:98-108, 127-137; orchestrator.py:341-347)  certainty: confirmed-by-running
- 10-15 10:01:20–10:01:43: upper=22862.1, threshold = upper + 0.03 %·spot = 22868.96. Spot sat at 22867–22870 (tick noise p50 0.2 pt, p99 1.8 pt/round). State flipped BREACH_UP↔NEUTRAL 14 times, the first "breach" lasting ONE round (0.25 s) at +0.09 pt past threshold; each snap-back emits FALSE_BREAK (12 events; strength 0.54 even for that 1-round blip).
- Spec §5.7 says "closes beyond … on the working timeframe (default 1-min)". Implementation uses raw 4 Hz ticks: spec deviation.
- The orchestrator "edge latch" `_false_break_on` is a no-op: `false_break` is a 1-round pulse, so the latch clears on the next round (orchestrator.py:342-347).
- Fix: see §2.1 (episode object, enter/exit thresholds, min dwell, one event per episode).

### [HIGH] Dead zone between level and level+buffer reports NEUTRAL; strength non-monotone  (continuation_validator.py:98-121)  certainty: confirmed-by-running
- Spot 0…7 pts ABOVE upper (below the buffer) → `cur_dir==0`, `near_up` requires `upper-spot>=0` → NEUTRAL, dir 0. 10-15 10:01:00–10:01:19: spot +2…+6.8 pts through the level shows NEUTRAL; it shows APPROACH_UP (0.30) only once spot falls back BELOW the level (10:01:43). Sequence APPROACH(0.30) → NEUTRAL(0) → BREACH(0.54) is non-monotone, so raw_bias/BIAS_EWMA dip exactly at the crossing.
- Also contradicts the orchestrator's own definition (`_approach_entry`/`_compute_alarm`: `spot >= ul - prox` = in-zone, `dist<0` = "BREACHED") so dashboard NIFTY card says BREACHED while burst board/`breached` field says none.
- Fix: states must partition the line: approach zone → "touch" zone (level…level+buffer) → breach. Use one geometry module shared by validator, entry, alarm.

### [HIGH] Volume ratio is dominated by option-window roll artifacts; latch is effectively always on  (orchestrator.py:201-214; continuation_validator.py:143-144)  certainty: confirmed-by-running
- `_vol_ratio` sums cumulative volume over the ATM±6 window rebuilt from spot every round (feed.py:361-379). When ATM changes, one strike leaves and one enters: engine delta jumps by millions (10-14 median at rolls 6.1 M; 10-16 29 M) versus a true per-round delta of ~18–41 k. **71 % / 82 % / 91 % of all volume the engine "sees" is roll artifact.** Rolls: 146/352/491 per day, i.e. exactly when spot moves.
- Per-round delta is 0 in 25–32 % of rounds; P(ratio≥1.2) per round = 0.32; P(latch within 2 s of a breach) = 0.95. So "volume ≥ 1.2×" never discriminates. The 15:20 vr_max values (29,331 and 6,937) are the garbage +185/+240-pt spot jump rolling the window, which is what "confirmed" the bogus CONFIRM_UP.
- After a roll the inflated 10-round mean suppresses real spikes for 2.5 s.
- Fix: §2.2 (futures/underlying volume, per-minute bars, common-strike deltas).

### [HIGH] Garbage-feed / feed-gap blindness: validator confirms on stale or jumped data  (orchestrator.py:265-283, 334-339)  certainty: confirmed-by-running (15:20 CONFIRM) + read (gap clock)
- No plausibility input: +185 pt one-tick spot jump → BREACH→CONFIRM within 0.75 s (ext ≥ 25 pts instantly + roll-driven vol latch). On 10-15 and 10-16 this CONFIRM ran 96 s/128 s.
- When `compute_basis` fails the orchestrator emits a NEUTRAL signal without calling `update`, so validator episode state (and `_breach_ts`, `last_total_vol`) survives the gap: elapsed time and accumulated volume across the gap count as persistence/volume → immediate confirm on recovery.
- Fix: validator gets `valid: bool`; on invalid input, freeze AND mark episode `STALE`; require N fresh rounds before any state above APPROACH; reset `last_total_vol` baseline on gaps.

### [MEDIUM] Re-anchor silently ends episodes and is reported as FALSE_BREAK / flips state  (orchestrator.py:306-339)  certainty: confirmed-by-reading + 10-15 10:01:55 (upper 22862.1→22932.4)
- Band is re-centred on current spot every 20 min (NIFTY) / 3 min (sisters); `update` re-tests spot against the NEW levels, so any live breach vanishes (false_break=True if unconfirmed) or a NEUTRAL spot becomes a "new" breach. For sisters (3 min) a CONFIRM needs the breach to begin in the first 60 s of the 180 s window.
- Fix: validator takes `level_version`; a change closes the episode with `LEVEL_RESET` (not FALSE_BREAK) and re-evaluates from APPROACH.

### [MEDIUM] CONFIRM has no memory; breakout-retest destroys it  (continuation_validator.py:104-108)
- Any dip below level+buffer for one tick calls `_reset_episode()`; confirmed state, `_max_ext`, `_vol_latch` are lost. A normal retest of a broken level returns to NEUTRAL and needs a fresh 120 s/+25 pts. Fix: confirmed episodes exit only below `level - exit_band` (hysteresis) or after `retest_grace` secs.

### [MEDIUM] Sister/stock levels are structurally unreachable or degenerate  (trigger_engine.py via orchestrator sisters; config linger 0.05 %)  certainty: confirmed-by-running (10-15 level_tape)
- Median half-width % of spot: BANKNIFTY 1.09, FINNIFTY 1.16, HDFC 1.38, ICICI 1.45, SBIN 1.31, AXIS 1.75, IDFCFIRSTB 5.0; linger = 0.05 %, buffer 0.03 %, so these never leave NEUTRAL. They use MONTHLY chains (extrinsic carries ~2 weeks of time value).
- Degenerate: ITC half-width 0.000 % (upper==lower) and outside band 60 % of the day; BHARTIARTL (step 20 on a 1859 spot) outside 34 %; BANKBARODA outside 18 %. These produce 24–89 % "CONFIRM" and ring `sister_breach`, and are recorded as the confluence "agreement" (signal_tape channels: BANKBARODA ×3, BHARTIARTL ×2). TCS has weight 0.
- Fix: validate bands (`upper>lower+min_width`, spot inside at anchor, else instrument = INVALID/excluded); derive sister thresholds from the sister's own realised volatility, not % constants.

### [MEDIUM] Strength is not what the spec says  (continuation_validator.py:152-161)
- Spec: strength = cleanliness (distance, volume, continuation). Code: BREACH 0.45+0.30·max(r_ext,r_time) ∈[0.45,0.75], CONFIRM 0.80+0.20·max ∈[0.8,1], APPROACH 0.10–0.30; volume ignored, time-based strength rises even while price retreats toward the level, CONFIRM saturates to ~1.0 immediately for extension-based confirms, 0.30→0.45 jump at the buffer. A 0.09-pt breach gets 0.54.
- Fix: continuous strength = clamp( a·ext/ATRband + b·persistence + c·volz ) with documented weights; strictly monotone through the zones.

### [MEDIUM] confirm_secs=120 vs confirm_points=0.1 % (≈23–25 pts) inconsistent with a narrow regime
- Breach needs +6.9 pts, confirm +23 pts or 120 s unbroken. With tick noise 1 s range ≈0.6 pt, 60 s range ≈6.5–8 pts, 120 s unbroken beyond level+7 is nearly impossible unless spot is already >15 pts beyond, while 23 pts is ~3 min-ranges. If v2 levels are ±30–40 pts, buffer+confirm (7+23) ≈ the half-width itself.
- Fix: scale all three from band half-width W and realised vol (§3).

### [LOW] APPROACH zone (0.05 % ≈ 11.4 pts) ≠ entry proximity (7 pts) ≠ dashboard bands (≤2 critical, ≤5 approach, ≤7 universe table)  
- Four different "near" definitions; 10-15 shows APPROACH_UP flapping for 130 s between 09:59:21–10:00:33 as spot oscillated 11 pts below level. Unify.

### [LOW] Other
- `update()` None-level branch reports FALSE_BREAK on a data gap (:92-96); effectively unreachable today but wrong.
- `signal_tape` always writes `continuation_confirmed=False, confirm_secs_elapsed=0` (orchestrator.py:961-969): §9.5 columns are vacuous.
- `instruments_view["breached"]` and sister sound derive from validator state, so the validator's dead zone/flicker directly causes repeated `sister_breach` rings (sound.js: `nb.forEach(... if !_breached.has(i))` re-fires on each flicker). Hysteresis in the validator fixes much of complaint B for sisters.
- Tests: only 2 validator tests; none for flips, gaps, re-anchor, dead zone, noise.

## 2. Improvements / design proposals (ranked)

### 2.1 State machine v2 (per instrument)
States: IDLE → NEAR(side) → TOUCH(side) → BREACH(side) → CONFIRMED(side) → (RETEST) ; plus INVALID. Parameters in points, from W = band half-width and σ = realised 1-min range (median of last 20 one-minute high-low).
- Thresholds with hysteresis: enter NEAR at d ≤ near_in = max(7, 0.20·W) , leave at d ≥ 1.4·near_in; enter BREACH when a **5-second bar close** (or ≥ 3 consecutive 1-s medians) is beyond level + buf_in, buf_in = max(2, 0.5·σ_tick_1s-range≈1 pt, 0.08·W); leave BREACH only when bar close < level + buf_out, buf_out = level − max(2, 0.5·buf_in)... i.e. enter +buf_in, exit −buf_in/2 (2:1 Schmitt band ≈ 3 pts at current noise).
- `min_episode_secs = 3` before an episode may be reported as FALSE_BREAK; shorter excursions are logged as `TOUCH_BLIP` at DEBUG (not tape-alerted).
- **One FALSE_BREAK per episode**: episode id = (level_version, side); emit when a breach with dwell ≥ min_episode_secs and max_ext < confirm_pts ends beyond exit band; then lock same-side re-breach for `refractory_secs = 20` unless ext > 2·buf_in. 10-15 would give 1 event (the 10:01:41 reversal) instead of 12.
- CONFIRM: dwell ≥ T_c = clamp(0.5·W-time, 20, 120 s) with all bar closes beyond level+buf_in **or** ext ≥ c·W (c≈0.5) with volz ≥ 1; confirmed state exits only at level − buf_in·2 or after retest_grace 30 s.
- LEVEL_RESET and INVALID are first-class transitions; on INVALID (basis gap, spot-vs-implied-future |gap|>k, frozen spot >5 s) emit no breach/confirm for ≥ 10 s after recovery.
- Emit transitions as events with episode id (dashboard/sound subscribe to transitions, not levels).

### 2.2 Volume baseline
- Use the NIFTY **future** (or underlying-index volume if present) — per-instrument cumulative `ttq` — not the option window sum. If options must be used: fixed strike set for the day (±6 around the 09:20 ATM, widened to ±10) or common-strike deltas only (my script: `true` delta = Σ clip(v_t − v_{t−1}) over strikes present in both rounds; this is ~100–1000× smaller than the engine value at rolls).
- Aggregate to **1-minute bars**; ratio = vol(last 60 s) / median(vol per minute over the previous 20 minutes), time-of-day normalised once ≥ 5 days of tape exist (intraday U-shape). Gate at ≥1.3 on that ratio; make it a strength term, not a latch. For sisters: weight by notional, skip if instrument has <N ticks/min.
- Sanity test on tape: P(ratio≥1.3) should be ~10–15 %, and should not rise at ATM rolls.

### 2.3 What is this module for in v2?
v1: nothing for NIFTY. Proposal: it becomes (a) the **regime classifier / trade-quality tag** for the anticipatory entry: tag every entry with state at entry (NEAR/TOUCH/BREACH, volz, W, σ) and log; (b) the **exit/failure monitor**: while in a trade, a CONFIRMED→reversal or FALSE_BREAK against the position is the early-warning input for the (operator-locked) exits only as a log until measured; (c) the sister voice only from instruments passing band validation and with W/σ sane. Do not gate entries on it (operator rule) until tape evidence (needs expiry-day + narrowed-band tape) shows CONFIRM-tagged entries beat unconfirmed ones; the tags make that measurable.

### 2.4 Feeding a non-expiry regime with narrower levels
- Make the machine band-relative: all thresholds from W and σ (above), so ±30-pt and ±100-pt bands behave consistently. With the current %-constants, W=35 gives buffer 6.9 (20 % of W), linger 11.4 (33 %), confirm 23 (66 %): breach and confirm zones overlap the band itself.
- Narrow bands re-centred every 20 min on spot will produce many LEVEL_RESETs; treat re-anchor as episode boundary and do not re-anchor while spot is within NEAR or in an episode (defer ≤ 60 s) — otherwise the level jumps away from the trade.
- Feed state to the vector as discrete + continuous: dir from state, strength = continuous score; APPROACH contributes ≤0.2 only if closing speed toward the level > 0.
- Sisters: use per-instrument spot-vs-VWAP or opening-range ± k·σ levels (not monthly ITM VWAP) in non-expiry mode; otherwise exclude them from confluence.

## 3. Recommendations for the two operator asks
**A (non-expiry)**: if the band is narrowed to W≈25–40 pts (other reviewers), use: near_in = 7 (keep entry proximity; make it min(7, 0.25·W)), buf_in = 2.5 pts, buf_out = 1.2 pts (≈ 2× / 1× the 1-s range p50 0.5–0.65), min_episode 3 s, refractory 20 s, confirm_pts = 0.5·W (≈15–20) or 45 s unbroken beyond level+buf_in, confirm gate volz ≥ 1.3 on 1-min bars. Position size small by tag (CONFIRM > BREACH > NEAR) only after measured. Replay measurement protocol: run `cv_orig` vs v2 on the three tapes with synthetic ±W bands anchored at the real anchor times (harness supports it); report episode count, false-break count (target ≤ 2 per 100 episodes of dwell<3 s), time-to-confirm, forward 60/180/300 s signed move after CONFIRM vs after unconfirmed breach.
**B (sounds)**: drive sounds from v2 *episode transitions only*: NIFTY NEAR (one sound per side, refractory 60 s), ENTRY, EXIT win/loss; drop sister_breach, or make it fire only on CONFIRMED sister with weight ≥ 0.08 and a 5-min per-instrument cooldown. Never from raw state level.

## 4. Fine / do not change
- Per-instrument stateful machine with a typed state alphabet and its tape representation (decision_tape vector) — keep the alphabet; add INVALID and LEVEL_RESET as event types.
- Latching volume per episode rather than requiring it on the confirm tick (idea is right, input is wrong).
- Edge-trigger philosophy for events (just implement it at episode level).
- Deterministic tape-ts clock (my replica reproduced 100 % exactly) — keep, it is what makes the proposals measurable.
- Operator rule "entry ignores confirmation" — consistent with data: the one genuine entry (10-15 10:00:14 CE, +6,233) profited while the validator was calling false breaks.

## 5. Tests to add
1. Noise-around-threshold: 500 ticks jittering ±1 pt about level+buf → exactly ≤1 episode, 0–1 FALSE_BREAK.
2. Dead-zone monotonicity: sweep spot from level−15 to level+15; strength and state non-decreasing, no NEUTRAL between APPROACH and BREACH.
3. Re-anchor mid-episode → LEVEL_RESET, no FALSE_BREAK.
4. Feed gap of 5 min mid-breach → no CONFIRM on recovery; volume baseline reset.
5. +185 pt one-tick jump with implied-future move <10 → INVALID, no BREACH/CONFIRM (15:20 regression on 10-14/15/16 tapes: zero CONFIRM rows).
6. ATM-roll volume test: constant true flow + spot crossing a strike boundary → volume ratio ≈1 (current code: ≫100).
7. Confirmed breakout retest within grace stays CONFIRMED.
8. Sister band validation: ITC-like upper==lower band → instrument INVALID, excluded from confluence/sounds.
9. Golden-tape test: replica of `05_episodes.py` output for 10-15 10:00–10:02 asserts event counts.
