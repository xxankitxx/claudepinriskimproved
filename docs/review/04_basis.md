# 04 — Basis engine + basis-change monitor (§5.1 / §5.5 / §2.4): findings

Scope: `basis_engine.py`, `basis_monitor.py`, `timeutil.basis_signal_weight`, `orchestrator._is_reversal` / REVERSAL_WATCH, reversal freeze in `decision_maker`.
All numbers below come from scripts in `/home/claude/vajra2_review/scratch/04/` (cached NIFTY extracts in `scratch/04/cache/`). Nothing under `vajra2/` was modified. I re-ran the production `BasisMonitor` + `_is_reversal` offline over the tapes and reproduced the recorded flags and events exactly (abrupt flags 0 mismatches on all 3 days; REVERSAL_WATCH 1,351 / 821 / 0, identical timestamps). So the bench is faithful and any redesign can be measured on it (`repro_monitor.py`, `repro_reversal.py`).

## 0. Headline
1. REVERSAL_WATCH is noise, and it has **zero effect on trading**: `reversal_freeze_flag` / `abrupt_against_bias` only reach `decision_tape` and the cockpit flags row. `fire` in `orchestrator.py:577` never reads them (the operator's no-veto rule made the spec §6(4) gate dead code). The freeze flag is nonetheless True on 99.1% (10-15) and 89.3% (10-16) of rounds before 13:17.
2. The detector measures the lag between the spot tick and the option LTPs, with the wrong sign, not a futures-premium shift (section 1.2).
3. Proposed 30-s-bar level-shift detector with persistence, hysteresis and refractory: **0 episodes in the clean session on all three days** (09:41–15:14), vs 0 / 1,351 / 821 events today. At 15:20 it fires only on the feed corruption, which a sanity gate should classify as FEED_GAP first.
4. The 15:15–15:20 cash-feed corruption has an unambiguous signature (spot frozen 245–300 s vs ≤16 s max in clean sessions; basis deviation ≥28 pts vs ≤~12 pts clean). Gate thresholds and typed reasons are in section 3.

## 1. Correctness defects (most severe first)

### [HIGH] ABRUPT test fires on spot-tick/option-lag noise, ~50% of rounds that pass the noise floor  (basis_monitor.py:60-80)  certainty: confirmed-by-running
- Mechanism (all measured, NIFTY, clean window):
  - Per-round |Δbasis| p50 = 0.20–0.25 pt; p90 ≈ 1.0; the 1.0-pt `noise_floor_pts` sits at the ~90th percentile, so ~10% of rounds (9.7% / 10.5% / 9.8%) pass it. At 4 Hz that is one candidate every ~2.5 s.
  - `d_eff` is 0 for ~90% of rounds, so the 30-sample window (7.5 s at 4 Hz) is mostly zeros and `rolling_std` is tiny. A lone ±m tick in an otherwise-zero window has std = m/√30 = 0.18m, so the 2.5σ bar (0.46m) is below m. Measured: P(abrupt | |Δ|≥1) = 0.52 / 0.51 / 0.56.
  - `D_BASIS_EWMA` (5-min halflife on a per-round delta) is ≈0 (it is the mean drift per 250 ms), so `|d − ewma| ≈ |d|`. The "baseline" adds nothing.
  - Parameter tweaks do not fix it: k=5 still gives 208 / 536 / 397 abrupt rounds; window 240 (1 min) gives 1,251 / 4,076 / 3,364 (vs 1,391 / 4,247 / 3,307 at the default). Needs a different design, not retuning.
- Root cause of the noise: BASIS = F − S where F = ATM + CE_ltp − PE_ltp moves with the options (LTP changes in only ~25–30% of rounds) while S ticks continuously. corr(ΔBasis, ΔSpot) = −0.62…−0.76 at 0.25 s, −0.44…−0.62 at 1 s, ≈0 by 30 s. About 40–55% of per-round basis variance is just the spot tick, and the sign is inverted (spot up → options lag → "basis collapse" → abrupt_dir = −1 → "bearish"). `_is_reversal` then calls a with-trend move a reversal.
- Std of basis change over horizon (clean window): 0.25 s 0.63–0.69, 1 s 1.0–1.2, 5 s 1.5–1.85, 30 s 2.3–2.75. Short horizons are noise; only minute-scale bars are usable.
- Fix: section 2.1.

### [HIGH] `_is_reversal` treats dust-level bias as an "established bias"  (orchestrator.py:251-256, decision_maker.py:149)  certainty: confirmed-by-running
- Only `_sign(prev_bias) != 0` (|bias|>1e-9) is required. A decaying EWMA never reaches exactly 0 for ~28 half-lives (84 min), so a bias exists almost always: prev_bias != 0 on 49,341 of 49,765 weight≥0.5 rounds on 10-15.
- Median |bias_ewma| at the abrupt rounds = 0.030 (10-15) / 0.024 (10-16) against `bias_threshold` 0.25. Max |bias| before 13:17 was 0.109 (10-15) and 0.051 (10-16); it never reached half the threshold (0.125). So "against the prevailing bias" is a coin flip on the sign of noise.
- Edge-trigger does not help: 1,370 reversal rounds produced 1,351 rising edges (every pulse is one round long).
- Freeze: each reversal round re-arms `_freeze_until_ms` for 2 min, hence freeze True 99.1% / 89.3% of rounds. Cosmetic today, a total trading block if the veto is ever reinstated.
- Fix: require `|bias_ewma| >= 0.5*bias_threshold` AND a confirmed episode from section 2.1. On the 3 tapes this gate alone also yields 0 events.

### [HIGH] No staleness / quality gate on any leg or on spot; LTP fallbacks can inject yesterday's price  (basis_engine.py:50-58, breeze.py:268, 257-261, feed.py:352-377)  certainty: confirmed-by-reading (+ measured spot freeze)
- `_normalize_tick`: `ltp = last or ltp or close`. An option that has not traded today gets **previous close** as LTP. `_on_ticks` carries forward any falsy field forever, and `StrikeQuote` has no per-quote timestamp, so a leg stale for 20 minutes is indistinguishable from a live one.
- `_build_snapshot` keeps a strike whenever `ltp is not None`, so `ltp == 0.0` passes and `F = atm + (0 − pe)` gives a garbage basis (no gate).
- IMPLIED basis has no range check. The recorded NIFTY basis reached −283 / −252 / −66 pts at 15:20+ and was used as is, including by the 15:21:55 interval re-anchor on 10-15 and the 15:25 post-exit anchor on 10-14 (basis −63).
- Spot freeze: NIFTY spot was unchanged for 1,117 / 981 / 1,200 consecutive rounds (279 / 245 / 300 s) from 15:15; in clean sessions the longest unchanged run was 3.8 / 10.0 / 16.0 s.
- Fix: section 3 (gates) plus per-quote age fields in the snapshot and tick_tape.

### [MEDIUM] Mode selection is right for NIFTY but wastes the best estimator for everything else, and ACTUAL has no fallback  (orchestrator.py:216-218, expiry.py:117-127)  certainty: confirmed-by-reading
- NIFTY: ACTUAL iff the active weekly expiry is the monthly (same-expiry future exists) — correct. All three tapes are IMPLIED; I had no ACTUAL-mode day to test.
- BANKNIFTY / FINNIFTY / stocks are monthly-only: their option expiry always equals the near-month future expiry (feed.py:140-147 already encodes this). A same-expiry traded future therefore exists every week, yet `_basis_mode_for` gives indices the NIFTY session mode and stocks always IMPLIED. The traded future is more liquid than any ATM stock option leg.
- Evidence the stock/sister implied basis is poor: clean-session median basis (bps of spot): NIFTY −3.7, BANKNIFTY +28, FINNIFTY +27, HDFCBANK +29, ITC +31, KOTAKBANK 0.0, RELIANCE +43, IDFCFIRSTB +16.5 (10-14). Expected carry for ~13 days to the 10-27 monthly is ~+20 bps less dividends; the cross-section spread (0 to +44 bps, i.e. ±3 pts on a 1,400 stock) is estimator error, not carry. These feed `LEVEL = RAW − BASIS` for every sister/stock band.
- ACTUAL failure mode: `future is None` → `NO_FUTURE_PRICE` → `_process_instrument` returns `{}` → **NIFTY has no band/entry at all** for the whole monthly week if the future tick is missing. There is no fallback to IMPLIED, even though the same expiry's option chain is subscribed. Also `future_traded` is the carried-forward LTP with no age check (same stale problem).
- Fix: `mode = ACTUAL if (fut age ≤ 3 s and fut expiry == chain expiry) else IMPLIED`, for every instrument; always record the traded future in `tick_tape` (new `fut_ltp`, `fut_age_ms` columns). In IMPLIED mode the traded future is currently discarded, so I could not calibrate implied vs traded on the tapes. Emit typed `ACTUAL_FUT_STALE_FALLBACK` once per episode.

### [MEDIUM] IMPLIED formula: LTP vs mid, ATM selection, structural negative level  (basis_engine.py:50-58)  certainty: confirmed-by-running
- LTP vs mid: on NIFTY ATM, mid-based basis gives **no noise reduction** (std of Δ at 0.25 s 0.67 vs 0.69; at 1 s 1.18 vs 1.18; at 30 s 2.64 vs 2.75; share |Δ|≥1: 0.082 vs 0.099). ATM spreads are only 0.15–0.45 pt, and LTP lies outside [bid, ask] in ~50% of rounds (ltp<bid 22–25%, ltp>ask 25–28%), so quote and LTP fields are not time-aligned in the recorded stream (check the breeze `bPrice/sPrice` mapping and carry-forward). Mid is still the right principle (it cannot be stale-by-trade), but it is not the cure for the flicker; bar-level aggregation is. Do not oversell the mid change.
- Stale leg, NIFTY ATM: LTP unchanged vs previous round in 70–75% of rounds; longest unchanged run 7 / 11 / 17 s, so a 60-s leg-age gate would never have fired on a healthy NIFTY ATM. Stocks need their own limits (untested here).
- ATM switch: 157 / 378 / 503 ATM changes per day; mean |Δbasis| at a switch 2.1 / 2.6 / 1.3 vs 0.4 otherwise, but they explain only ~2% of |Δ|≥1 rounds. Minor. Prefer the strike nearest the forward (as `pin_locator` does) or average K−1/K/K+1.
- Structural level: clean-session NIFTY basis is persistently negative (median −3.7 bps 10-14, down to −14 pts mean on 10-16) although carry for 1–3 days should be about +3 to +5 bps. Levels shift by −basis, i.e. up by 5–14 pts. Cause is probably put-rich parity, not carry; nothing here validates that the "forward→spot" translation is meaningful off-expiry (flag for the strategy reviewer).

### [MEDIUM] Basis time-weight is a time-of-day schedule, not a time-to-expiry schedule  (timeutil.py:29-51, orchestrator.py:84)  certainty: confirmed-by-reading
- `reversal_weight_floor 0.5` makes REVERSAL_WATCH impossible after 13:17:23 (matches last events 13:16:57 / 13:17:12; zero on 10-14 which started 13:26). The spec's "convergence kills the basis edge" applies on expiry day. On a T-1..T-3 day there is no end-of-day convergence. For the non-expiry-day goal make the weight a function of trading-time-to-expiry (1.0 until ~14:30 when T ≥ 1 day).

### [LOW] Other
- `noise_floor_pts=1.0` is absolute: absurd for stocks (IDFCFIRSTB ~75, basis ≈ 0.1 pt) and trivial for BANKNIFTY. Use bps of spot. Non-NIFTY flags are unused, but are written to `basis_tape`.
- `RollingStats` window is sample-count based (30 samples = 7.5 s at 4 Hz, 30 s at 1 Hz): behaviour changes with feed rate. Use event-time.
- REVERSAL_WATCH payload (`d_basis`, `dir`) lacks basis level, spot, bias, episode id; the cockpit strip key omits `dir`, so all collapse into one "×N" row.
- Sign convention `LEVEL = RAW − BASIS` with `BASIS = F − S`: **correct and consistent** (level_tape: `upper − (raw_upper − basis)` max abs error 0.0 on all three days; golden tests cover premium and discount). It is only wrong when S or F is stale: LEVEL = RAW − F + S then drifts opposite to F while S is frozen.
- Anchor-time basis noise is small in normal sessions: |basis used − 60-s median| mean 1.2 / 0.9 / 1.2 pts, max 2.4 / 3.6 / 3.8 (44 anchors). Level error from basis noise is not what blocks entries (the 7-pt trigger is); the exception is anchors on a corrupted feed (15:21:55 on 10-15, 15:25 on 10-14).
- `Reason.STALE_CHAIN` / `MISSING_TICK` exist but are never emitted; an instrument absent from the round bucket (ITC had 1,013 basis rows on 10-16) raises no FEED_GAP.

## 2. Design proposals (ranked)

### 2.1 Robust ABRUPT detector ("BasisShiftMonitor") — prototype in `scratch/04/detector.py`
Input: gated basis samples only (section 3 gates pass), at the existing 4 Hz.
1. 30-s event-time bars: bar value = median of samples in the bar (need ≥20 of ~120, else bar is NaN and counted as blind time).
2. Level-shift statistic `L_t = median(last 2 bars) − median(previous 10 bars)` (1-min level vs 5-min baseline). Replaces the meaningless per-round delta and its EWMA.
3. Threshold `thr = max(k·σ, floor_pts, floor_bps·spot)`, σ = 1.4826·MAD of L over the previous 60 bars (≥10 needed). Defaults: k=4, floor_pts=6, floor_bps=2.5 (≈6 pts at 23,000).
4. Persistence: |L| ≥ thr with the same sign on 2 consecutive bars (≥60 s) before ARMED→ABRUPT.
5. Hysteresis/latch: leave ABRUPT only when |L| < 0.5·thr for 2 consecutive bars and the episode has lasted ≥ 4 bars (120 s = the existing `reversal_cooldown_min`).
6. Refractory: same-direction re-trigger blocked for 10 bars (5 min); opposite direction allowed immediately and tagged `flip`.
7. Emit exactly one `REVERSAL_WATCH` per episode (payload: episode_id, dir, shift_pts, basis_level, spot, start_ts, thr); `episode_end` as an ANOMALY. Interpret as a reversal only if `|bias_ewma| ≥ 0.5·bias_threshold` or a position is open in the opposite direction.
8. `basis_tape` new columns: `basis_robust` (last bar median), `L`, `abrupt_state` (−1/0/+1), `episode_id`, `basis_quality`.

Episodes on the recorded tapes (30-s bars, clean = before 15:14):

| setting | 10-14 | 10-15 | 10-16 | events (clean window) |
|---|---|---|---|---|
| production | 0 | 1,351 | 821 | (09:43–13:17 only) |
| k=4, floor 6 pts (proposed) | 0 | 0 | 0 | none |
| k=3, floor 4–5 pts | 1 | 2 | 0 | 10-14 ~13:xx +6 (14:19 10-15 +6, 15:11:30 +7) |
| k=5, floor 8 | 0 | 0 | 0 | none |

Full-day with the proposed settings: 10-14: 2 episodes (15:21:30 −28, 15:28:00 +34), 10-15: 1 (15:20:30 −152), 10-16: 1 (15:20:30 −185), all inside the corrupted-feed window and to be suppressed by the gate. Caveat, stated plainly: three days with ~0 genuine events gives no information on predictive value. Recommend logging every episode with forward 5/15-min spot return so 2.0 accumulates evidence before any veto is attached. Expect ≤2–3 episodes per session at k=3/floor 4–5; use those for sensitivity, k=4/floor 6 for production.

### 2.2 Basis estimator for levels
Use `basis_robust` (60-s median of gated samples) for `LEVEL = RAW − BASIS` instead of the instantaneous value; keep sign convention. Prefer the traded same-expiry future (fresh) and fall back to a 3-strike parity average; the mid change is cheap and principled but gives no measurable gain on these tapes.

### 2.3 Snapshot/tape additions (needed for deterministic replay of any gate)
Per-quote age (`age_ms`) for spot, future, ATM CE/PE; `fut_ltp`; `quality` flags. Today replay can only see value-equality, not arrival time.

## 3. Sanity gate (spot vs implied future) and typed FEED_GAP reasons
Evidence: 15:15:00–15:20:0x all cash instruments frozen; NIFTY basis then −15→−60 (10-14), −185 (10-15), −243 (10-16); spot converges toward F only slowly and on 10-16 was still ~90 pts off at 15:25. At 15:20 the median |bps| across instruments was 120–350 for several heavyweights (AXISBANK 143, BAJAJFINSV 274, BHARTIARTL 346, INFY 328, LT 256, RELIANCE 301). Clean-session NIFTY basis deviation from its own 5-min median stayed ≤ ~12 pts.

Gates (evaluate per instrument per round, before `basis_mon.update` and before any (re)anchor):
1. `SPOT_FROZEN`: spot arrival age > 30 s (NIFTY/indices; value-unchanged run > 120 rounds as fallback when age is unavailable) while the implied future moved ≥ 3 pts cumulatively. Margin: clean max 3.8–16 s vs frozen 245–300 s; trips at 15:15:30. Stocks: use 120 s and require the same condition for ≥ 50% of instruments to classify as feed-wide (`SPOT_FROZEN_ALL`).
2. `SPOT_FUTURE_DIVERGENCE`: |basis − median(basis over previous 5 min)| > max(20 pts, 8 bps of spot) for indices (stocks: 40 bps) on the 30-s-bar median, or |basis| > 0.20% of spot absolute. Trips at ~15:20:00 on all three days (shift ≥28 pts). Latch: release only after |dev| < 0.5·threshold for 60 s; if the 5-min median itself is poisoned, hold the last good basis and also require two consecutive agreeing bars.
3. `STALE_LEG`: ATM CE or PE age > 60 s (healthy NIFTY max ≈ 17 s), `ltp ≤ 0`, or `ltp == previous close` fallback hit. Fall back to neighbouring strike parity; if none, `MISSING_ATM_QUOTE` as today.
4. `ACTUAL_FUT_STALE_FALLBACK` (section 1).
Action when any of 1–3 trips for NIFTY: no new entry, no re-anchor (keep held levels, set `levels_stale`), no basis_mon update, one typed `FEED_GAP {instrument, reason, stage:"sanity", spot, implied_future, deviation, age_ms}` per episode (edge latch like `_basis_gap`). Add the new strings to `schema.Reason` and use `RECALC_TRIGGERS` additions as other reviewers specify. Cheap belt-and-braces: hard last-entry cutoff 15:10 (feed-integrity cutoff belongs to the execution reviewer).

## 4. Fine, do not change
- Sign convention `LEVEL = RAW − BASIS`, `BASIS = F − S`, golden tests and discount mirror.
- Deterministic tape-ts decay (TimeEWMA) and replay determinism; the exact reproduction above relies on it.
- NIFTY weekly / monthly ACTUAL-vs-IMPLIED rule and the feed's weekly-vs-monthly chain expiry split.
- Basis as advisory context (no veto) given the evidence; do not attach a veto until section 2.1 has accumulated forward-return statistics.
- The edge-latch pattern for FEED_GAP (`_basis_gap`) is a good template for the new gates.

## 5. Tests to add
1. 4-Hz flicker test: feed the recorded-noise-like series (uniform ±1.2 pt per-round jitter, spot-correlated) into the new monitor for 1 h; assert 0 episodes. The current test uses 30-s spacing and never exercises 4 Hz.
2. Step test: +12 pts level shift held 3 min → exactly one episode, direction correct, ends after release; 20-s blip → none.
3. Hysteresis/refractory: oscillating around thr → one episode; same-direction retrigger within 5 min blocked.
4. `_is_reversal` with |bias| = 0.03 and threshold 0.25 → False.
5. Frozen-spot replay: spot constant 300 s while F ticks → `SPOT_FROZEN` once, no re-anchor, no entry.
6. Divergence: +240 spot jump in one round → `SPOT_FUTURE_DIVERGENCE`; recovery latch.
7. Stale leg: `ltp=0`, `ltp=close` fallback, 90-s-old leg → gap, never a basis.
8. ACTUAL mode with `future_traded=None` or stale → IMPLIED fallback + typed event; NIFTY keeps a band.
9. Replay regression on `tapes/2026-10-15` asserting REVERSAL_WATCH count ≤ 3 and no anchor between 15:15 and 15:30.
10. Level-anchor test: anchor uses `basis_robust`; injected 10-pt LTP glitch at anchor time moves levels < 2 pts.

## Scripts
`scratch/04/`: `extract_nifty.py` (cache), `repro_monitor.py`, `repro_reversal.py`, `flagrate.py`, `diag_basis_noise.py`, `diag_basis_minute.py`, `diag_quotes.py`, `detector.py`, `sens.py`, `misc.py`.
