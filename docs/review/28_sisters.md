# 28 — R28 Cross-instrument universe (BANKNIFTY, FINNIFTY, 21 stocks): findings
Scripts + outputs: `/home/claude/vajra2_review/scratch/28/` (01 spot1s, 02 geometry, 03/04 lead-lag, 05/07 chain liquidity, 06 v2 sister sim, 08 true strike grid, `nifty_weights_v2.csv`).
Evidence: 3 tapes (basis_tape spot, level_tape anchors, decision_tape vectors, tick_tape chain samples, event_tape), 3 NSE factsheet PDFs (pdftotext). **All lead/lag statistics exclude 15:15 onward.** I build on 06/07 (monthly-chain width, born-breached, weights, junk bias) and do not repeat them.

## 0. Decisions (the answers)
1. **Did any sister/stock ever lead NIFTY?  No.** Measured below (section 3): peak cross-correlation at lag 0 s; lead-minus-lag CIs include 0 for every instrument tested; at 1-2 s NIFTY slightly leads BANKNIFTY/FINNIFTY/HDFCBANK/ICICIBANK. Power is low (3 days, ~684 one-minute returns, ~14 event windows) so "no detectable lead", not "proven none".
2. **Universe for 2.0: 15 live instruments, not 24.** NIFTY (full chain), BANKNIFTY (spot + near-month future; chain shadow-only), FINNIFTY (spot only), 12 NIFTY-50 voters on CASH spot (chains shadow-only and only where the liquidity gate passes). Drop 9: FEDERALBNK, INDUSINDBK, AUBANK, BANKBARODA, IDFCFIRSTB, BSE (not NIFTY-50 members), SHRIRAMFIN, BAJAJFINSV, SBILIFE (members but 0.7-1.2%, illiquid chains with `computed` VWAP). That removes ~9 chains, all 8 false step corrections and the ITC black-out (section 1).
3. **Sister bands are shadow/advisory only; sister events NEVER make a sound** (default `sound.sister: off`, and no sister entry in the sound table at all). Re-open only after the shadow calibration in 07 §3 passes on >=60 entries.
4. **Weights:** NIFTY-50 weights only; table in section 2.1 (sum of 12 voters = 57.12%, renormalised to 1.0 in code). Bank/Fin sub-index weights are never used.
5. **Sister level formula, 1-min state, liquidity gate, strike-step table and config knobs:** section 2.

## 1. Correctness defects (most severe first)

### [HIGH] STRIKE_STEP_CORRECTED widens the step beyond the true grid: 8 of 17 corrections in 3 days were WRONG  (feed.py:317-332)  certainty: confirmed-by-running
- Rule: adopt the streamed spacing when `obs >= 2*cur` for 3 consecutive rounds, `obs = min diff of strikes that have ticked`. Right after a (re)start only some strikes have ticked, so `obs` is a multiple of the real step. Truth from the finest grid that ever ticked with all 13 strikes present (`08_true_grid.py`, 14:00 snapshots): AXISBANK 10, BAJFINANCE 10, AUBANK 20, SBILIFE 20, INDUSINDBK 10, FEDERALBNK 5, BANKBARODA 5, ITC 5, KOTAKBANK 5.
- Wrong corrections (true step in brackets): 10-14 AUBANK 10->40 [20], INDUSINDBK 10->20 [10], FEDERALBNK 5->10 [5]; 10-16 (the restart day) BAJFINANCE 10->20 [10], AXISBANK 10->20 [10], SBILIFE 20->40 [20], AUBANK 10->40 [20], BANKBARODA 2.5->10 [5] at 11:15:56. Right ones: BANKBARODA/ITC 2.5->5, BAJFINANCE/SBILIFE/SHRIRAMFIN 5->10/20/20, AUBANK 10->20 (10-15).
- Consequence: K_CE/K_PE become 2-ITM strikes (`atm -/+ 1*step` with a doubled step), `atm = round_to(spot, step)` snaps coarser, the subscribed window halves in strike count, and the old (finer) grid stays subscribed forever (never unsubscribed). level_tape shows both steps per instrument (`02_geometry.py`: AXISBANK [10,20], SBILIFE [20,40], AUBANK [20,40], BAJFINANCE [10,20], INDUSINDBK [10,20], BANKBARODA [5,10], FEDERALBNK [5,10]) - a step flip changes the band by a whole strike mid-day.
- Fix: stop inferring steps from a live window. v2 ships a verified `strike_step` table (section 2.3, measured) and validates it at startup against `breeze.option_step` computed around the SPOT (not the chain centre, see next). Corrections only ever go FINER-to-COARSER if the coarse grid is confirmed on >=80% of strikes present for >=60 s AND >=11 of 13 strikes ticked; otherwise emit `STEP_MISMATCH` and keep the table value. Persist `state/steps_<date>.json` + `STEP_TABLE` event (tape-durable, replay reads it).

### [HIGH] Instrument silently dropped for 4 h 11 min: ITC on 10-16 had NO basis/tick/level row from 11:11 to 15:21  (feed.py:323-329, orchestrator.py:278-286)  certainty: confirmed-by-running (symptom) / plausible (mechanism)
- basis_tape: ITC first row 15032 s after its first FEED_GAP (10-16); BANKBARODA 310 s, BSE 88 s. `MISSING_ATM_QUOTE` is edge-latched so the tape holds ONE event; nothing says the instrument stayed dead. Recovery came only at 15:21:10 when spot 268 made `round_to(spot, 2.5)=270` land on a real 5-step strike. Cause: resolved step 2.5 (true 5): `atm=round_to(spot,2.5)` is a non-listed strike half the time; the correction needs 3 consecutive "ok" rounds and any flicker resets the counter.
- Fix: per-instrument liveness watchdog: instrument with no basis row for >60 s after warm-up -> `INSTRUMENT_DEAD` event (repeats every 5 min, not edge-latched) and a cockpit chip; ATM must be chosen from the LISTED strikes (nearest listed to spot), never `round_to(spot, guessed_step)`.

### [HIGH] Decision weight falls back to bank/fin sub-index weights  (config.py:41-49; nifty_weights.csv)  certainty: confirmed-by-reading (extends 07)
- `w = nifty or bank or fin`: 9 of 21 stocks carry sub-index weight as if it were NIFTY weight. TCS has no row. 07 gave the diagnosis; fix here: v2 `decision_weight = nifty_weight_pct/100` only; a stock with `role=voter` and no weight is a startup ERROR (not a stderr warning); `role=context` rows return 0. Reject the file if voter weights sum outside [0.45, 0.65] (guards the sub-index mix-up).

### [MEDIUM] `option_step` picks the "chain centre" by strike COUNT, not by spot  (breeze.py:200-206)  certainty: confirmed-by-reading + events
- `mid = ks[len(ks)//2]`; far-wing strikes are finer/coarser than the ATM region, so the resolver returned 2.5 for ITC and BANKBARODA (true 5), 2.5 for KOTAK (comment says real 5), AUBANK 10 (true 20), and failed `no_chain` for 6-7 stocks on every start (retry then succeeds 15-20 s later, so these are start-up REST failures, not pre-market ones). Fix: pass `spot` in; take the modal diff of the 11 strikes nearest SPOT. With the static table this is only a cross-check.

### [LOW-MEDIUM] FEED_GAP `basis` x24 at every start is a warm-up artefact, not a gap  (feed.py:360-361, 336-342; orchestrator.py:277-285)  certainty: confirmed-by-running
- First round: `_ensure_option_subscriptions` subscribes ATM+/-6 only inside `_build_snapshot` on the first round, so the first round has no option ticks for any of the 24 instruments -> 24 `MISSING_ATM_QUOTE` events within 1 s of each start (10-14 13:26:06, 10-15 09:41:55, 10-16 11:11:02). Time from that event to the first basis row: median 6.1 s / 1.9 s / 5.3 s, p75 12.3 / 4.2 / 14.7 s, max 22-28 s (BAJFINANCE, SBILIFE) except the dead ones above. NIFTY itself had no ATM quote for the first ~1-6 s. They pollute event_tape and, per 12/20, would trigger sounds.
- Fix: (a) subscribe the initial ATM+/-n window for every instrument in `_ensure_base_subscriptions` as soon as its first cash tick arrives (poll <=5 s); (b) per-instrument warm-up: suppress `FEED_GAP basis` until `warmup_secs=45` after the first cash tick, and emit ONE `STARTUP_WARMUP {ready:n, total:m, secs}` summary at +45 s listing laggards; (c) after warm-up, any gap is a real event. `FEED_GAP step_resolution` likewise folds into the summary.

### [LOW] tick_tape writes all 24 instruments every 4 Hz round  (orchestrator.py:289; tape sizes 150-285 MB/day)  certainty: confirmed-by-reading
- Stocks' cash prints change in only 3-18% of 4 Hz rounds (AUBANK 2.7%, BANKBARODA 4-7%, HDFC 12-14%, RELIANCE 14-18%; NIFTY 82-88%, BANKNIFTY 58-69%, FINNIFTY 60-69%), so >80% of stock rows duplicate the previous one. v2: sister rows at 1 Hz and chains only for shadow-tier instruments; expect ~5-8x smaller tape and ~60% less per-round CPU (not measured).

### [LOW] Display lies / dead fields touching sisters  (orchestrator.py:352-353; 07 covers the bloc 2x)
- `instruments_view.weight` for BANKNIFTY/FINNIFTY shows 0.35 each, DecisionMaker applies 0.175. Remove with the bloc.

## 2. Improvements / design proposals (ranked)

### 2.1 Weight table (exact from the three factsheets dated 2026-05-29)
NIFTY 50 (ind_nifty50.pdf, top-10): HDFCBANK 10.56, ICICIBANK 8.32, RELIANCE 8.27, BHARTIARTL 5.20, LT 4.43, INFY 3.77, SBIN 3.71, AXISBANK 3.42, KOTAKBANK 2.62, ITC 2.56 (sum 52.86). Sectors: Financial Services 35.15, IT 8.48, Telecom 5.20.
NIFTY BANK (top-10): HDFC 17.93, ICICI 13.63, AXIS 10.28, KOTAK 9.81, SBIN 9.07, FEDERAL 6.38, INDUSIND 5.40, AU 4.87, BANKBARODA 4.47, IDFCFIRSTB 4.27. NIFTY FIN (top-10): HDFC 17.86, ICICI 13.58, AXIS 10.28, KOTAK 9.39, SBIN 9.04, BAJFINANCE 8.06, BSE 5.62, SHRIRAMFIN 4.40, BAJAJFINSV 3.42, SBILIFE 2.73. Betas/correlation to NIFTY: BANK 1.13 / 0.92 (1y), FIN 1.18 / 0.93. **None of these two sub-index tables is a NIFTY weight; only the NIFTY column is used.**
**TCS is in none of the three PDFs.** Bounds from the factsheet: TCS < ITC 2.56 (it is not top-10) and TCS+HCLTECH+TECHM+WIPRO = IT 8.48 - INFY 3.77 = 4.71. Point estimate 2.06 is from the 2026-06-11 NIFTY constituent list (fetched through a summarising tool; I did not read that PDF myself) where TCS 2.06, HCL 1.10, TECHM 0.88, WIPRO 0.48 sum to 4.52 (vs 4.71 on 05-29, consistent with 12 days of drift). Treat as UNVERIFIED +-0.15; materiality is tiny (normalised voice 0.036). Same source and caveat for BAJFINANCE 2.20 (a NIFTY-50 member above TCS, already wired in the universe).
Cross-check of that source against the factsheet: it differs on the top-10 by 0.01-0.56 pts (HDFC 10.74 vs 10.56, SBI 3.92 vs 3.71) - normal 2-week drift - so it is fit for a 3rd-decimal-free voter table, not for anything finer.

v2 `nifty_weights.csv` (also at `scratch/28/nifty_weights_v2.csv`, includes comments, step, chain_mode, source, asof columns):
```
instrument,type,nifty_weight_pct,role,chain_mode,strike_step,beta_vs_nifty,source,asof
NIFTY,index,100.00,execution,full,50,1.00,config,
BANKNIFTY,index,,context,shadow,100,1.13,factsheet_2026-05-29 (beta),2026-05-29
FINNIFTY,index,,context,none,50,1.18,factsheet_2026-05-29 (beta),2026-05-29
HDFCBANK,stock,10.56,voter,shadow,10,,factsheet_2026-05-29,2026-05-29
ICICIBANK,stock,8.32,voter,shadow,10,,factsheet_2026-05-29,2026-05-29
RELIANCE,stock,8.27,voter,shadow,10,,factsheet_2026-05-29,2026-05-29
BHARTIARTL,stock,5.20,voter,shadow,20,,factsheet_2026-05-29,2026-05-29
LT,stock,4.43,voter,none,50,,factsheet_2026-05-29,2026-05-29
INFY,stock,3.77,voter,shadow,20,,factsheet_2026-05-29,2026-05-29
SBIN,stock,3.71,voter,shadow,10,,factsheet_2026-05-29,2026-05-29
AXISBANK,stock,3.42,voter,none,10,,factsheet_2026-05-29,2026-05-29
KOTAKBANK,stock,2.62,voter,shadow,5,,factsheet_2026-05-29,2026-05-29
ITC,stock,2.56,voter,none,5,,factsheet_2026-05-29,2026-05-29
BAJFINANCE,stock,2.20,voter,none,10,,constituents_2026-06-11_UNVERIFIED,2026-06-11
TCS,stock,2.06,voter,shadow,20,,constituents_2026-06-11_UNVERIFIED,2026-06-11
```
Voters sum 57.12 -> normalised: HDFC .1849, ICICI .1457, RELIANCE .1448, BHARTI .0910, LT .0776, INFY .0660, SBIN .0650, AXIS .0599, KOTAK .0459, ITC .0448, BAJFINANCE .0385, TCS .0361.
Known residual bias: financials are 54% of this panel vs 35.15% of NIFTY (only 12 names, no auto/FMCG/pharma beyond ITC/RELIANCE/INFY/TCS). v2.1 candidates to add as CASH-only voters once their ws `stock_name` is captured live (needs `WS_NAME_TO_INSTRUMENT` + isec code, not in the repo): M&M (2.53 on 06-11), MARUTI 1.62, HUL 1.79, SUNPHARMA 1.79, TITAN 1.57 (06-11 list, unverified). Do not hard-code; same validator.
Loader rule set (replace config.py:41-49): `decision_weight = nifty_weight_pct/100 if role==voter else 0`; startup ERROR if (voter without weight) or (non-voter stock subscribed) or sum(voters) not in [45,65]%.

### 2.2 Who stays live and at what tier
| tier | instruments | feed | purpose |
|---|---|---|---|
| execution | NIFTY | cash + weekly chain + future | trades |
| context index | BANKNIFTY | cash + near-month future; monthly chain SHADOW (ITM notional Rs 59-103 cr, spread 0.3%) | `bank_excess5` shadow feature |
| context index | FINNIFTY | cash + future only; chain REJECTED (ITM notional Rs 0.33-0.57 cr, ITM CE day-volume median 15.8 k vs NIFTY 136 M, spread 1.1-1.3%) | highest explanatory power for NIFTY (R2 0.755 vs BANKNIFTY 0.586) at zero option cost |
| voters (12) | HDFC ICICI RELIANCE BHARTI LT INFY SBIN AXIS KOTAK ITC BAJFINANCE TCS | cash spot; chain only if gate passes (marked shadow in the table) | `breadth5` (price based, 07 §3) |
| dropped (9) | FEDERALBNK INDUSINDBK AUBANK BANKBARODA IDFCFIRSTB BSE SHRIRAMFIN BAJAJFINSV SBILIFE | none | zero NIFTY weight or <1.2% and unusable chains |
FINNIFTY vs 07's "drop as redundant": BANKNIFTY vs FINNIFTY 1-min corr is 0.827, FINNIFTY vs NIFTY 0.869, BANKNIFTY vs NIFTY 0.765; R2 NIFTY~BANKNIFTY+FINNIFTY 0.761 vs 0.755 for FINNIFTY alone. As spot-only inputs they cost nothing; keep both but never as chain/band instruments. Neither has a lead (section 3).
Chain liquidity measured (`07_chain_summary.py`, 1/min samples 11:15-15:15, 10-15 + 10-16, ITM 1-strike CE/PE day-volume x LTP, Rs crore, min of CE/PE): TCS 5.98, RELIANCE 4.08, HDFCBANK 4.02, INFY 3.82, BSE 3.62, BHARTIARTL 2.26, SBIN 1.80, ICICIBANK 1.52, KOTAKBANK 1.31, BAJFINANCE 0.92, FEDERALBNK 0.83, INDUSINDBK 0.82, ITC 0.68, LT 0.53, AXISBANK 0.43, BANKBARODA 0.41, FINNIFTY 0.33, SHRIRAMFIN 0.19, BAJAJFINSV 0.19, IDFCFIRSTB 0.17, AUBANK 0.01, SBILIFE 0.00; BANKNIFTY 58.7, NIFTY 647. ITM spread medians: all 0.2-1.9% except AUBANK 8.8%, SBILIFE 7.8%, ITC 2.2%, BANKBARODA 1.5%. `vwap_source=computed` (no feed VWAP) on 56% of AUBANK and 42% of SBILIFE ITM samples. Day-VWAP/LTP deviation of the ITM CE, p90: NIFTY 33%, BHARTI 34%, ICICI 29%, RELIANCE 24%, TCS 23% - a cumulative day-VWAP is a stale price once the underlying has moved, which is why monthly/stock bands lag.
**Chain acceptance gate (all must hold at the instant of anchoring, else band = NONE and state = NEUTRAL, reason logged once):** ITM CE and PE both two-sided (bid>0, ask>0); spread <= 2.5% of mid (5-min median); `vwap_source == feed`; cumulative day notional (volume x LTP) of each ITM strike >= Rs 1.0 cr x min(1, minutes_since_09:15/120); VWAP within [intrinsic, intrinsic + 1.5 x ATM_straddle/2] (catches the FINNIFTY 427->643 glitch from 07); >= 11 of the 13 subscribed strikes have ticked. Passing set on these tapes: BANKNIFTY, TCS, RELIANCE, HDFCBANK, INFY, BHARTIARTL, SBIN, ICICIBANK, KOTAKBANK.

### 2.3 Strike-step table (measured; replaces inference)
NIFTY 50, BANKNIFTY 100, FINNIFTY 50, HDFCBANK 10, ICICIBANK 10, RELIANCE 10, SBIN 10, AXISBANK 10, BAJFINANCE 10, KOTAKBANK 5, ITC 5, INFY 20, TCS 20, BHARTIARTL 20, LT 50 (the other 9 are dropped). Step as % of spot: NIFTY 0.22, BANKNIFTY 0.18, FINNIFTY 0.20, stocks 0.7-1.9% (HDFC 1.4, SBIN 1.0, ICICI 0.7, INFY 1.8, LT 1.4, KOTAK 1.1, ITC 1.9). This is the structural reason a stock's 1-ITM strike is 4-9x further from spot than NIFTY's, so its band half-width is >= 1 strike (0.7-1.9%) before any time value - independent of the monthly extrinsic in 06/07. Validate monthly: table vs `option_step(spot-centred)`; mismatch -> `STEP_MISMATCH`, trust the table, never auto-widen.

### 2.4 How sister levels must be computed (v2 `compute_sister_band`)
Principles: (i) no rounding that can move a level across spot; (ii) strip monthly extrinsic the same way NIFTY will; (iii) cap in % of spot scaled to the instrument's own volatility; (iv) reject, never repair, a bad chain.
```python
def sister_band(snap, step, k):                 # k = per-instrument cfg row
    F   = snap.spot + basis                     # forward; basis from parity/actual future as today
    kce, kpe = listed_strike_below(F, depth=1), listed_strike_above(F, depth=1)   # from LISTED strikes
    ic, ip = max(F-kce,0), max(kpe-F,0)         # forward intrinsic
    ec = clip(vwap_ce - ic, 0, k.ext_max_frac*vwap_ce)      # extrinsic, bounded (0.9)
    ep = clip(vwap_pe - ip, 0, k.ext_max_frac*vwap_pe)
    up = k.pad*ic + (1+k.pad)*k.extrinsic_weight*ec          # distance ABOVE spot  (NIFTY: 0.1*50 + 1.1*0.35*ext)
    dn = k.pad*ip + (1+k.pad)*k.extrinsic_weight*ep
    lo, hi = k.floor_pct/100*spot, k.cap_pct/100*spot
    up, dn = clip(up, lo, hi), clip(dn, lo, hi)
    return spot - dn, spot + up                  # unrounded floats; display rounding = 0.1*step only
```
- **Round step:** compute unrounded; for display round to `0.1 x strike_step` (= NIFTY's 5/50), floor at tick (0.05). Legacy `level_round_step: 5` on a 5-pt-strike stock (ratio 1.0, +-2.5 error on a 5-10 quantity) is what created born-breached bands (measured rounding error as % of the 1.1xVWAP width term: IDFCFIRSTB 88%, ITC 35%, BANKBARODA 29%, KOTAK 21%, FEDERAL 20%, HDFC 13%, SBIN 12%, NIFTY-equivalent 0.4% only for BANKNIFTY).
- **Non-expiry cap in % of spot:** NIFTY's proposed 45-pt cap (14_config) at spot 22.9k is 0.196% = **7.5 x NIFTY's 1-min sigma** (0.0265%). Apply the same sigma-multiple: `cap_pct_i = 7.5 x sigma_1min_i` -> BANKNIFTY 0.25%, FINNIFTY 0.23%, HDFCBANK 0.35, ICICIBANK 0.36, SBIN 0.35, LT 0.38, RELIANCE 0.44, KOTAKBANK 0.44, AXISBANK 0.45, BHARTIARTL 0.49, INFY 0.50, TCS 0.50 (sigma = std of 1-min log returns from these 3 tapes: 0.0334, 0.0313, 0.0462, 0.0476, 0.0472, 0.0505, 0.0587, 0.0581, 0.0595, 0.0647, 0.0673, 0.0666 %; ITC/BAJFINANCE not measurable -> default `2.0 x NIFTY sigma`). Store in `calibration/sigma_1min_pct.yaml`, refresh by script from the last 20 sessions, never inline. `extrinsic_weight 0.35`, `pad 0.10`, `floor_pct 0.06` (same as NIFTY's ~13-pt floor). Expiry days: NIFTY's expiry regime applies unchanged; sisters keep the same formula (monthly chain has no expiry regime).
- **Monthly-chain extrinsic:** the 1-ITM VWAP of a monthly chain is 1.0-3.6% of spot (BANKNIFTY 1.14, FINNIFTY 1.22, HDFC 2.4, INFY 3.6, BSE 3.6), i.e. mostly time value; at weight 0.35 with the cap the band collapses to the cap for every instrument except where intrinsic dominates, which is the intended behaviour (the cap, not the extrinsic, sets the width). Raw 1.1xVWAP bands were +-1.0-1.4% for the heavyweights (06/07).
- **Born-breached guard:** band is anchored at current spot so it cannot be born breached; but a band whose half-width < floor, or any rejected chain, yields NONE -> NEUTRAL.

### 2.5 Slower, cleaner sister state
- **Clock:** 1-min bar closes (last tick of the minute). Stocks' cash prices tick in only 3-18% of 4 Hz rounds (about every 5-30 s), so sub-minute sister logic is noise by construction. A bar whose last cash tick is older than 90 s is `STALE`, not NEUTRAL, and carries no vote.
- **Anchor cadence:** `reanchor_secs: 600` (was 180), aligned to the 09:20 grid, plus on chain-gate transitions only. 3-min re-anchors re-created the born-breach every 3 min (06/07).
- **Persistence:** APPROACH needs >=2 consecutive bar closes inside `approach_frac x half_width` (0.25) of a level; BREACH needs >=2 consecutive closes beyond level +/- `buffer` (0.02% of spot) = >=120 s (same as `continuation.confirm_secs`); leaving a state needs 2 closes back inside by half the buffer (hysteresis). No FALSE_BREAK/REVERSAL signalling for sisters.
- **Measured effect** (`06_sister_v2_sim.py`: bands rebuilt offline from level_tape anchors every ~9 min, 1-min closes, 3 days <15:15, cap 7.5 sigma, persistence 2): non-neutral share of bars 0.1-2.2% for BANKNIFTY 0.8, FINNIFTY 0.3, HDFC 0.3, ICICI 0.2, KOTAK 0.3, LT 0.1, INFY 1.0, RELIANCE 1.1, SBIN 1.3, TCS 2.0, AXIS 2.2, BHARTIARTL 6.1 (a trending name on 10-15); 0.3-1.7 episodes/day (BHARTI 4). Legacy: BANKBARODA 89/25/80% of rounds, ITC 65%, BHARTIARTL 34%, HDFCBANK/FINNIFTY ~0 (07). Sensitivities: persistence 1 raises the share to 0.7-3.7% (BHARTI 9.5%); cap 12 sigma -> BANKNIFTY/FINNIFTY/HDFC/AXIS/KOTAK/INFY go to 0 episodes (cap too loose); approach_frac 0.5 -> 2-5% (BHARTI 22%). Simulation limits: 3 days, anchored from the legacy chain rows, no claim of predictive value.

### 2.6 Config knobs (v2 `vajra_config.yaml`)
```yaml
universe:
  weights_file: nifty_weights.csv            # role/chain_mode/strike_step columns drive everything below
  voter_weight_sum_range_pct: [45, 65]       # validator
sisters:
  enabled: shadow                            # off | shadow | advisory   (never 'gate')
  sound: off                                 # no sister sound in the sound table at all
  clock: bar_1m
  stale_bar_secs: 90
  reanchor_secs: 600
  band: {pad_pct: 0.10, extrinsic_weight: 0.35, ext_max_frac: 0.9, floor_pct: 0.06,
         cap_sigma: 7.5, sigma_file: calibration/sigma_1min_pct.yaml, sigma_default_x_nifty: 2.0,
         round_display_frac_of_step: 0.1}
  state: {approach_frac: 0.25, buffer_pct: 0.02, persist_bars: 2, release_bars: 2, release_buffer_frac: 0.5}
  chain_gate: {min_itm_notional_cr: 1.0, ramp_minutes: 120, max_spread_pct: 2.5, require_feed_vwap: true,
               vwap_max_over_intrinsic_x_halfstraddle: 1.5, min_strikes_ticked: 11}
startup:
  warmup_secs: 45                            # suppress per-instrument FEED_GAP until then; emit one STARTUP_WARMUP
  instrument_dead_secs: 60                   # INSTRUMENT_DEAD event after warm-up, repeats every 300 s
strike_step: {source: table, validate_vs_chain: true, auto_widen: false}
```
Validator (startup, fatal): `cap_sigma*sigma >= floor_pct`; `persist_bars >= 2`; `sisters.enabled != gate`; every `voter` has a weight; no subscribed stock lacks a row; `sound` has no key containing "sister".

### 2.7 Shadow features replacing the band vote (07 §3 inputs, with what I measured about their data)
`breadth5` and `bank_excess5` need only cash ticks: 12 voters x 1-min bars. Sync-ness warning from the data: 1-min return correlation with NIFTY 0.63 (HDFC), 0.59 (ICICI), 0.52 (RELIANCE), 0.28 (INFY), 0.26 (TCS), so the panel explains ~half of NIFTY variance at best; do not treat any one stock as a signal.

## 3. Did any sister lead NIFTY? (measured, <15:15 only)
Data: basis_tape `spot` last-per-second, forward-filled, 3 days (10-14 from 13:26, 10-15 from 09:41, 10-16 from 11:11). ITC missing all of 10-16 (defect above).
**Test A - 1-min log-return cross-correlation, sister(t) vs NIFTY(t+k), k>0 means sister leads** (`03_leadlag.py`, `testA_xcorr_1min.csv`, N~684): contemporaneous c0: FINNIFTY .869, BANKNIFTY .765, HDFC .627, SBIN .598, ICICI .585, LT .584, RELIANCE .524 ... TCS .264. Lead-minus-lag (mean of k=+1..+3 minus k=-1..-3), 95% block-bootstrap CI (30-min blocks): BANKNIFTY +0.003 [-0.051,+0.046], FINNIFTY +0.014 [-0.038,+0.054], HDFC 0.000 [-0.053,+0.045], ICICI +0.019 [-0.033,+0.068], RELIANCE -0.012 [-0.062,+0.055], AXIS +0.023 [-0.025,+0.083], KOTAK +0.028 [-0.029,+0.095], TCS -0.010 [-0.031,+0.064], **INFY -0.075 [-0.099,-0.011] (NIFTY leads INFY)**. No instrument has a positive CI. Single-lag values are all |c|<=0.13 (LT +0.128 at k=+1 is one lag, one name of 23, uncorrected).
**Test C - 5-s returns, lags -60..+60 s** (`testC_xcorr_5s.csv`): peak at lag 0 for BANKNIFTY, FINNIFTY, HDFC, ICICI, RELIANCE, INFY, TCS, BHARTI; at -1 s (NIFTY ahead) for SBIN, KOTAK, AXIS, LT, ITC. Asymmetry favours NIFTY: BANKNIFTY k-1 .482 vs k+1 .461; FINNIFTY .614 vs .590; HDFC .378 vs .327; ICICI .368 vs .295; RELIANCE .301 vs .229. Correlation beyond |5 s| is <0.07 everywhere.
**Test B - event study** (`04_event_symmetric.py`, `testB2_symmetric.csv`): 5-min windows with a >=4 sigma move in the event-defining instrument, both moved the same way >=2.5 sigma, first crossing of 1.5 sigma of each own 1-min vol, statistic tX-tN. n=5-20 events per pair. Median tX-tN (event defined by NIFTY / by X): BANKNIFTY -0.5 / -3.0 s, FINNIFTY 0 / -2 s, HDFC +13 / 0, ICICI -7.5 / -62, RELIANCE -3 / +0.5, SBIN +3 / -6, INFY +98 / -9, KOTAK +43.5 / -38. Signs flip with who defines the event for most names, i.e. selection noise; BANKNIFTY/FINNIFTY are within 0-3 s (below the 1 s sampling plus 0.25 s round cadence resolution of what we can claim). **A naive version (events defined by NIFTY, 50%-of-own-move crossing, `03_leadlag.py` Test B) shows "sisters lead by 20-130 s" in 6-9 of 14 events - that is a selection/normalisation artefact (it also appears with events defined by X), do not quote it.**
Conclusion: no tradable lead from any sister or stock on these tapes. Verdict stays: sisters = context/shadow. Required to ever promote: >=20 sessions, pre-registered lead-lag on 1-min bars with the same CI method, and incremental value over NIFTY's own momentum.

## 4. Things that are fine and should NOT be changed
- NIFTY-only execution; sisters can never send an order.
- Monthly chain for BANKNIFTY/FINNIFTY/stocks (`_options_expiry_for`), per-instrument try/except isolation, universe-completeness (`stale` rows), edge-latched events, tape-time EWMA.
- `compute_basis` IMPLIED/ACTUAL logic for sisters (parity on a monthly chain gives BANKNIFTY basis ~+176 = a carry, correctly subtracted in forward space).
- Per-instrument steps in config for indices (BANKNIFTY 100, FINNIFTY 50 verified on all days).

## 5. Tests to add
1. `test_weights_nifty_only`: no voter weight sourced from bank/fin columns; TCS present; voters sum in [45,65]; non-NIFTY-50 name with role=voter fails.
2. `test_strike_step_never_widens_on_partial_chain`: feed 13-strike window of a 10-step stock where only every 2nd strike ticks -> step stays 10, `STEP_MISMATCH` emitted, no `STRIKE_STEP_CORRECTED`.
3. `test_atm_from_listed_strikes`: ITC spot 267.5, grid 5 -> ATM 265 or 270 (a listed strike), basis available.
4. `test_instrument_dead_event`: 120 s without a basis row after warm-up -> `INSTRUMENT_DEAD`, repeats at 300 s (regression for the ITC 4 h black-out).
5. `test_startup_warmup_no_feedgap_flood`: 24 instruments with options arriving 1-20 s late produce 0 `FEED_GAP basis` and exactly one `STARTUP_WARMUP`.
6. `test_sister_band_never_born_breached`: price 229.3, step 5, VWAP 6.58 (BANKBARODA case) -> band contains spot, half-width in [floor, cap].
7. `test_sister_cap_scales_with_sigma`: cap_pct = 7.5 x sigma; NIFTY 22.9k -> 45 pts.
8. `test_chain_gate`: AUBANK-like (spread 8.8%, `computed` VWAP, notional 0.01 cr) -> NONE; BANKNIFTY-like passes; VWAP outside [intrinsic, intrinsic+1.5 half-straddle] -> NONE.
9. `test_sister_state_persistence`: single 1-min close beyond level -> NEUTRAL; two -> BREACH; one close back inside -> still BREACH (hysteresis).
10. `test_sister_sound_absent`: sound table and dashboard JS contain no sister sound; validator fails if `sound` has a sister key.
11. Tape regression on 10-15/10-16: after fixes non-neutral share per sister <5% except BHARTIARTL; ITC present all day on 10-16.
