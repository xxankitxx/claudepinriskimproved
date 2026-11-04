# 07 — Cross-instrument decision-maker & confluence: findings
Scripts: /home/claude/vajra2_review/scratch/07/ (01_states, 04_bias_stats, 05_reversal_vs_bias, 06_nifty_states_timeline, 08_predict).
Evidence = decision_tape instrument vectors (3 days), level/basis tapes, NSE factsheet PDFs in the repo.

## Headline
The cross-instrument module is, on the recorded days, (a) mute for the instruments that matter, (b) loud and wrong for a few that don't, (c) circular because NIFTY votes in its own bias, and (d) mis-weighted. It has no measurable predictive value on 3 days. Recommendation: keep it ADVISORY, rebuild the inputs (price-based breadth, correct weights, no NIFTY self-term), delete the dead FIRE/LEAD gate, and do NOT give it a veto or a sizing role until it is calibrated on >=20 sessions via the replay harness.

## 1. Correctness defects (most severe first)

### [CRITICAL] Sister/stock bands cannot produce a real signal; non-neutral states are quantisation artifacts  (orchestrator.py:176,258-421; config level_round_step=5; trigger_engine.py:90-93)  certainty: confirmed-by-running
- Sisters/stocks re-anchor every 180 s on monthly-expiry chains (VWAP carries weeks of extrinsic), so the band is ~+-1.0-1.4% of spot around spot at anchor time (HDFCBANK +-1.41%, BANKNIFTY +-1.18%, FINNIFTY +-1.2%, ITC +-0.95%); the "approach" zone is 0.05% (linger_band_pct). A 1.2% move inside 3 minutes never happens.
- Measured non-neutral share of rounds (3 days): HDFCBANK 0/0/0; FINNIFTY 0, 4, 0 rounds; BANKNIFTY 7, 0, 0 rounds; SBIN 9, 0, 1. The 0.35 bloc and the 10.6% heavyweight never spoke.
- The few that DO speak are artifacts: `level_round_step: 5` is applied to stocks priced 70-700 with 5-pt strike steps, so `round_to(1.1*VWAP, 5)` has +-2.5 error on a ~5-10 quantity. E.g. BANKBARODA spot 229.3, K_PE 235, VWAP 6.58 -> 1.1x=7.2 -> rounds to 5 -> lower=229.72 > spot -> "breach" born at the anchor and re-born identically every 3 min. Result: BANKBARODA non-neutral 89% / 25% / 80% of rounds (CONFIRM_DOWN 82% of 10-14), ITC 65% of 10-15 (CONFIRM_UP), BHARTIARTL 34% (10-15), IDFCFIRSTB 15% (10-16).
- Also data glitches: FINNIFTY VWAP_CE jumped 427 -> 643 (K=24850, intrinsic ~93) at 13:32 on 10-14 (illiquid monthly chain; no sanity check vs intrinsic+premium).
- Fix: for non-NIFTY instruments stop using the pin band for the vote. Use price-based features (below). If bands are kept, round to the instrument's strike step (or 0.05% of spot), reject a band whose spot is already outside it at anchor ("born-breached" -> NEUTRAL, log), and bound VWAP to [intrinsic, intrinsic + k*ATM_premium].

### [CRITICAL] Junk DC-bias drives the REVERSAL_WATCH flood (A3) and would have frozen every legit entry  (orchestrator.py:448,514-527; decision_maker.py:148-151)  certainty: confirmed-by-running
- `_is_reversal` needs `_sign(prev_bias) != 0`. The "prevailing bias" is just the constant offset of BANKBARODA/ITC/etc. (|bias_ewma| median 0.03, max 0.11). 99.3% (10-15) and 100% (10-16) of REVERSAL_WATCH events fired when NIFTY itself had zero contribution. 10-14 had none only because the tape starts 13:26 (basis weight <0.5).
- decision_tape.reversal_freeze_flag was true for 100% of rounds 10:00-13:00 both days (62% / 45% of whole days). Had the DecisionMaker been the live gate, the one legitimate trade (10-15 10:00:14, +6,233) would have been blocked by REVERSAL_FREEZE (and was below threshold: bias 0.054 vs 0.25, NIFTY only APPROACH).
- Fix: reversal logic must compare against NIFTY's own recent move (or d_basis sign vs. NIFTY 5-min return), never a chorus DC offset; edge-trigger with hysteresis (>=30 s clear) and a cooldown.

### [HIGH] NIFTY votes in its own bias with weight 1.0 -> the "chorus" is circular and LEAD is impossible  (decision_maker.py:122-138,205)  certainty: confirmed-by-reading + data
- Decomposition (04): NIFTY's own contribution is 0.10-1.0 when non-zero; everything else is median 0.03-0.045. sign(bias) == sign(NIFTY dir) by construction, so check (3) "bias agrees" cannot disagree with NIFTY. `_confluence.confidence` and signal_tape.conviction inherit this.
- LEAD ("fire before NIFTY breaks on an overwhelming chorus", thr 0.55): max achievable non-NIFTY voice needs bloc 0.35 + HDFC+ICICI+RELIANCE ~0.27 all at strength 1.0; with NIFTY's own term, it fires simply ~5 min after NIFTY breaches (EWMA lag), i.e. later than CONFIRM. LEAD as specified cannot lead.
- The threshold 0.25 has no scale meaning (total possible |bias| ~2.4). On 10-14 |bias_ewma| never reached 0.1; on 10-15/16 every round with |bias_ewma|>=0.25 lies in 15:21-15:24 — the broken-feed spike. The "calibrated" gate's only FIREs would have been fiction.
- Fix: bias_ext = sum over NON-NIFTY only; NIFTY handled separately; normalise weights to sum 1.

### [HIGH] Weight table is wrong  (nifty_weights.csv; config.py:31-49)  certainty: confirmed (factsheets)
- Stocks outside the NIFTY top-10 get their weight INSIDE NIFTY BANK / NIFTY FIN (e.g. BAJFINANCE 8.06, BSE 5.62, FEDERALBNK 6.38, INDUSINDBK 5.40) — that measures influence on the sector index, not on NIFTY. Non-top-10 NIFTY financials in total are only 35.15 - 28.63 = 6.52% of NIFTY, yet BAJFINANCE+SHRIRAMFIN+BAJAJFINSV+SBILIFE are given 18.61; FEDERALBNK, AUBANK, BANKBARODA, IDFCFIRSTB, BSE are not NIFTY-50 names at all (true weight 0) but carry 25.6.
- Sum of stock decision weights = 102.5% (top-10 = 52.86 genuine; 11 proxies = 49.62), plus bloc 0.35 plus NIFTY 1.0. Financials hold ~113% vs their true 35.15% of NIFTY; IT/auto/FMCG/pharma have almost no voice. It is a financials detector, not a NIFTY detector.
- Triple counting: the five shared banks (28.63% of NIFTY, 60.7% of BANKNIFTY) vote as stocks, inside BANKNIFTY and inside FINNIFTY.
- TCS missing (zero voice; weight is < 2.56 since it is outside the factsheet top-10; get the exact figure from the NSE index factsheet — do not guess). index_beta in config is unused.
- BANKNIFTY/FINNIFTY: decision_tape shows 0.175 each (0.35*0.5*agree) but instruments_view/dashboard show w_bloc 0.35 each (orchestrator.py:352-353) — display lies by 2x [LOW].
- Fix: voters = NIFTY-50 members only, weight = true NIFTY weight, renormalised; drop non-members from the feed (saves ~5 chains); treat BANKNIFTY as one optional voter (its 5 banks already counted) and drop FINNIFTY as redundant (0.93 corr).

### [HIGH] Verdict logic defeats the weight-aware design; zero-weight and junk voices count  (orchestrator.py:936-953)  certainty: confirmed-by-reading + tape
- `len(same) >= 3` -> STRONG ignores weight: 10-16 15:24:47 entry was "STRONG" from BANKBARODA+BHARTIARTL+IDFCFIRSTB (pull ~0.13 << 0.35) on a broken feed.
- Any single opposing voice (even ITC 0.026*0.1) forces CONFLICTED. TCS (weight 0) still appears in same/opp lists (loop only skips dir==0/NEUTRAL).
- Dashboard NIFTY LEAN uses absolute_net_pull with no dead-band (dashboard.py:858-861): BANKBARODA alone paints "NIFTY BEARISH" for ~80% of 10-14 and 10-16.
- Fix: skip pull==0 voices; STRONG = same_pull>=x and opp_pull<=y*same_pull; CONFLICTED only if opp_pull >= 0.5*same_pull; dead-band the lean at |net|<0.05.

### [MEDIUM] signal_tape.conviction can be negative; `enter()` params are dead  (orchestrator.py:596-598,961-969; execution.py:180)
- confidence = -|bias| when bias disagrees: 3 of 5 recorded signal rows have negative "conviction" (-0.019, -0.008, -0.156). `conviction/threshold/channels` passed to Executor.enter are never read.
- Fix: log signed `confluence_net` separately; drop unused args.

### [MEDIUM] Positional bloc bug  (decision_maker.py:106-112)
- `bloc_vals` appends only present signals, then `bank=bloc_vals[0]`, `fin=bloc_vals[1]`: if BANKNIFTY is absent from `signals`, FINNIFTY's value is read as BANKNIFTY. Use `signals.get(b)` with default 0.

### [MEDIUM] Dead FIRE path still computes and logs misleading flags  (orchestrator.py:538-543,664; decision_maker.py)
- decision_tape keeps the DM's bias/freeze/divergence fields but the decision/reason are overwritten, so the tape mixes two authorities. `channels_in_agreement` in DM includes NIFTY, `_confluence.same_side` excludes it. DM CONFIRM path adds >=1 min EWMA latency on top of 120 s confirm — contradictory with "anticipate".
- Also `_approach_entry` treats ANY spot beyond a level as zone (dist -82 pts on 10-15 15:20): chasing, not anticipating; cap at |dist|<=2*prox unless fresh.

### [LOW] Divergence penalty double-counts: opposing bloc already reduces raw_bias, then conviction*=0.5.

## 2. Measurements
- Bloc agreement with NIFTY approach (engine states): bloc (BANKNIFTY+FINNIFTY) non-neutral in 11 rounds of ~167k total; essentially always "silent". The only agree/disagree signals came from the artifact stocks listed above. Of 5 recorded entries, 1 was legitimate (10-15 10:00, verdict MILD via BHARTIARTL pull 0.054; +6,233); 4 were 15:20-15:25 broken feed.
- Predictive value (08_predict.py, 1-min grid, 728 min, non-overlapping-block bootstrap, windows touching 15:10+ dropped). Spearman vs NIFTY forward move:
  - bias_ewma: H10 -0.04 [-0.23,+0.13]; H20 -0.08 [-0.29,+0.12]
  - non-NIFTY chorus: H10 -0.04 [-0.23,+0.12]; H20 -0.07 [-0.27,+0.11]
  - price breadth (top-10 NIFTY names 5-min return minus NIFTY's): H10 -0.12 [-0.28,+0.03]; H20 -0.04
  - BANKNIFTY excess 5-min return: H10 -0.09; H20 -0.06
  - NIFTY own 5-min momentum: +0.07 / +0.09 (CIs include 0)
  Verdict: no detectable edge, and the power is low (effective N ~60 / ~30 independent windows; only |rho|>0.25-0.35 would register). The weak negative sign on breadth is mean-reversion-flavoured, not significant. Do not calibrate thresholds on this.

## 3. Proposal for v2 (answer to "what job should this module have?")
1. Expiry day: keep operator-locked "no veto"; advisory only.
2. Off expiry: NO veto and NO size cut yet. More entries are the operator's goal and a confluence veto reduces them; the data cannot justify a threshold. Instead ship it in SHADOW mode: every entry logs `confluence_net`, `breadth5`, `bank_excess5` (signed, normalised) to order/signal tape; a replay sweep over >=20 sessions decides later. Pre-registered rule to adopt: use as a SIZE modifier (not a veto) only if top-tercile vs bottom-tercile mean entry P&L differs with a bootstrap p<0.1 over >=60 entries; candidate mapping 0.5x lots if net opposes by >=0.25 normalised, 1.0x otherwise (never >1.0x, preserving all-in cap).
3. Inputs rebuilt (cheap, robust, no band dependence): `breadth5 = Σ w_i*ret_5m(i)/Σw_i - ret_5m(NIFTY)` over NIFTY top-10 (renormalised true weights), `bank_excess5 = ret(BANKNIFTY) - beta*ret(NIFTY)`; agreement = sign match with approach direction, hysteresis 0.02%; ignore stale instruments (>10 s old) and the frozen-cash-feed window.
4. Delete DecisionMaker FIRE/LEAD/divergence/freeze code (keep EWMA of external breadth if wanted); remove NIFTY self-term; one decision authority in decision_tape.
5. Weights: true NIFTY weights, Σ=1, add TCS from NSE data, drop non-members; refresh from factsheet semi-annually via a script that validates Σ and flags missing universe members as an error, not a warning.
6. Reversal watch: depends on NIFTY basis vs NIFTY price only; hysteresis + cooldown.

## 4. Fine / do not change
- No-veto on expiry; NIFTY-only execution; TimeEWMA on tape time (deterministic); `_confluence` excluding NIFTY; absolute_net_pull direction-independence (fix #6); in-trade confluence read relative to position direction; per-instrument try/except isolation; edge-latched events.

## 5. Tests to add
- Weight table: Σ stock weights <= 1.0; every universe stock has a weight; no non-NIFTY-50 name has weight>0; test fails on TCS missing.
- Born-breached: a freshly anchored band with spot outside/within linger -> state NEUTRAL (stock priced 229, step 5, VWAP 6.58).
- Verdict: three featherweights must not be STRONG; zero-weight voice excluded; one 0.002 opposing voice must not give CONFLICTED.
- Bloc positional bug: signals lacking BANKNIFTY but with FINNIFTY.
- Reversal: constant junk bias with NIFTY flat must not raise REVERSAL_WATCH.
- LEAD/shadow: external-only bias; NIFTY signal must not change it.
- signal_tape conviction >= 0 (or renamed signed field).
- Tape-based regression: on 10-15/10-16, count of non-neutral rounds for HDFCBANK/BANKNIFTY/BANKBARODA after fix is within sane bounds (BANKBARODA < 5%).
