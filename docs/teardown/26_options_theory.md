# 26 — Options theory of the non-expiry MTM band: teardown
Verdict: Not a Monday risk: 19 Oct is an expiry day, and that day runs the exact 1.0 band (orchestrator.py:263-266). As a non-expiry band, 2.0 is better than 1.0 because its levels can actually be reached. But the "writers 20% underwater" idea is mostly not what sets the level. On the with-trend side it is just S±25, and the profit evidence is too thin and too timing-sensitive to tune parameters on.
Score (your area): 1.0 = 2/10, 2.0 = 5/10

Data: my own extract of the NIFTY chain (ATM±4 strikes, LTP and feed VWAP) from the three tick tapes, streamed in 4000-row batches.
Coverage: 14 Oct 13:26-15:15, 15 Oct 09:41-15:15, 16 Oct 11:11-15:15. I cut each day at 15:15 because the feed was garbage after that.
Bands were computed with **2.0's own `compute_band_v2`** at each 20-minute anchor from 09:20, giving 33 anchors and 66 levels.
Trades came from a proxy sim of the operator rules: approach within 7 pts → buy the ATM CE/PE, all-in sizing, −12% stop, trail arms at +10% with floor max(+7%, 0.94·peak), cooldowns of 15 s after a win and 11 min after a stop, re-anchor after each exit and when each cooldown ends, hold the band while a trade is open, minimum premium 45.
Scripts are in `/home/claude/teardown/scratch/26/` (extract.py, sim.py, anchors.py, trades.py, trades2.py, grid.py), with the per-anchor table in anchors.csv.
VWAP source on the tapes is `feed` in all 366k quotes checked, i.e. Breeze's full-session average price, so a tape that starts mid-day does not distort VWAP.

## Findings (most severe first)

### [HIGH] On the with-trend side the MTM level is just "S ± 25". The writer-pain logic never decides it (trigger_engine.py:174) — reproduced
- Theory: the session VWAP of *today's* 1-ITM strike lags the trend. After a rally, the 1-ITM CE is a strike that was OTM or ATM when most of today's volume traded, so LTP/VWAP is already above 1.2. That makes `(1.2·VWAP − LTP)` negative, the floor wins, and the level becomes S+25 (rounded). The put side mirrors this on a sell-off.
- Evidence from anchors.py over 33 anchors:
  - LTP/VWAP of the 1-ITM CE on 15 Oct (trend-up day): 1.36–1.56 at every anchor from 10:00 to 14:20. Raw MTM upper distance was −57 to −3 pts, so the level sat at the floor all day.
  - Floor-bound share: upper 76%, lower 12%. Pooled, 45% of all levels are ≤27.5 pts from spot.
  - Pooled MTM half-width percentiles q10/25/50/75/90: **23 / 25 / 44 / 109 / 140**. Legacy (1.0): 69 / 82 / 101 / 139 / 161.
  - Per anchor, the near side has a median of 25.2 pts and the far side a median of 110 pts.
- When price reaches a floor-bound level, writers are already well past "20% underwater". LTP/VWAP of the anchor strike at first touch of the MTM upper was 1.21, 1.27, 1.34, 1.47, 1.48, 1.48, 1.51 and 1.58. So the dashboard label ("pain level") describes something other than what triggers the trade.
- What the band really is: a momentum trigger. It fires after an 18-pt push from the anchor (25 − 7 proximity) on whichever side the 1-ITM option trades above its VWAP. It is a reasonable signal for a breakout buyer, but it is **not** a level where writers come under pain.
  - A simpler rule reproduces it. "trendfix" = S±25 on any side where 1-ITM LTP > VWAP, else the 1.0 level. It produced 15 trades, 12 wins, +₹1.11 L, against MTM's 13 trades, 11 wins, +₹0.92 L (grid phase 0).
- CHANGELOG.md:16-17 claims "median half-width came in from 90–132 pts to roughly 35–80 pts". **Not reproduced.** Only 18% of levels fall in 35–80; the distribution is split into 25 and 100–160.
- Fix: no code change needed for Monday. **PROPOSAL (operator):** document the band honestly as "trend side = 25-pt momentum trigger, counter side = VWAP-pain level", and show LTP/VWAP of the 1-ITM option on the dashboard so the operator can see which regime each side is in. Do not raise `mtm_pad` hoping to "move the trend-side level". The floor-bound side ignores the pad completely.

### [HIGH] The profit evidence depends on when the 20-minute grid starts; no parameter choice is supported by this data (grid.py) — reproduced
- Same rules, same data, with the 20-minute grid start moved by 0/5/10/15 min. In the live engine the phase moves after every exit and cooldown re-anchor anyway. Totals over the three tapes:
```
('mtm',0.2,25)  g+0: n=13 w=11 +92345 | g+5: n=14 w=9   -370 | g+10: n=15 w=9  +15899 | g+15: n=11 w=10 +97295
('mtm',0.1,25)  g+0: n=16 w=12 +95449 | g+5: n=15 w=10 +23826| g+10: n=18 w=10 +17735 | g+15: n=14 w=11 +94133
('mtm',0.2,20)  g+0: n=18 w=12 +36888 | g+5: n=19 w=13 +21392| g+10: n=20 w=11 -29442 | g+15: n=17 w=10 -12230
('mtm',0.2,30)  g+0: n=9  w=9 +111264 | g+5: n=14 w=8  -1528 | g+10: n=12 w=7  +32091 | g+15: n=11 w=8  +33595
('mtm',0.2,35)  g+0: n=10 w=9  +69456 | g+5: n=9  w=6 +11960 | g+10: n=8  w=7  +46413 | g+15: n=8  w=7  +37707
('fixed',25)    g+0: n=24 w=11 -31109 | g+5: n=21 w=13 +26201| g+10: n=22 w=13 +15100 | g+15: n=23 w=16 +64984
('fixed',40)    g+0: n=11 w=8  +24547 | g+5: n=11 w=7 +10540 | g+10: n=10 w=5  -8925  | g+15: n=12 w=6  -6832
('legacy',)     g+0: n=0          +0  | g+5: n=1  w=1  +8437 | g+10: n=1  w=1  +6757  | g+15: n=0         +0
```
- A 5-minute phase shift swings MTM 0.20/25 from +₹92 k to −₹0.4 k. The whole sample is about 13 trades, 11 of them CE on one trend day (15 Oct). Any "+₹X L" headline for the MTM band from these tapes is mostly luck of the anchor phase.
- What survives every phase:
  - A floor of **20 is worse** than 25–35 at every phase (sum over 4 phases: +₹17 k vs +₹205 k at 25, +₹175 k at 30, +₹166 k at 35).
  - Fixed symmetric bands (25 or 40, trading both sides) are clearly worse than the trend-selecting rules. Counter-trend 25-pt entries got stopped (fixed-25: 11 of 24 trades hit −12%).
  - pad 0.10 vs 0.20 makes no meaningful difference (+₹231 k vs +₹205 k summed), because the side that trades is floor-bound.
- Recommendation: **keep pad 0.20, floor 25, fallback delta 0.65 for now.** No change is justified. **PROPOSAL:** revisit the floor (25 vs 30) only after ≥10 full non-expiry sessions, judged on results across many grid phases rather than one replay.

### [MEDIUM] The counter-trend level is far and almost never reached; the "narrow-only vs 1.0" guard never binds (trigger_engine.py:176) — reproduced
- Approach rates per 20-min hold window (within 7 pts):

| Band | Upper | Lower |
|---|---|---|
| MTM | 30% | 6% |
| 1.0 legacy | 0% | 0% |

- Median 20-min excursion from the anchor: +11.5 / −17.4 pts.
- The legacy level was tighter than the MTM level in **0 of 66** levels, so `min(up_leg, up_mtm)` never decided anything. The narrow-only safeguard is harmless but inert, and the counter side is effectively "no trade" (100–160 pts away).
- Consequence: off expiry, 2.0 trades in one direction only. On 15 Oct every MTM trade was a CE. That fits a breakout buyer. The operator should know the engine will not catch a reversal through a VWAP-pain level unless the move is more than 100 pts in one 20-minute hold.
- Fix: none required. **PROPOSAL:** if two-sided activity is wanted, cap the far side with `max_half_width_pts` (60–80). The fixed-40 and fixed-60 rows above show no evidence that this pays, so leave it at 0 for now.

### [MEDIUM] IV moves, not gamma or theta, are the main error in the level between re-anchors (theory plus tape check) — read in code / computed
- The level is the linear solve `S + (1.2·VWAP − LTP)/Δ` at the anchor instant, then held for 20 min. Error sizes for DTE 1–4, S≈23,000, σ≈14.5%, √T≈0.09:
  - **Gamma:** Γ ≈ φ/(Sσ√T) ≈ 0.0013 per pt. The convexity term ½Γx² is ~1 option pt at 40 pts, ~2.4 at 60 and ~6.6 at 100. The linear solve therefore overstates the distance by ~1.5 / 4 / 11 pts. Tape check: where the raw MTM decided the level (~37 pts on 14 Oct), LTP/VWAP at first touch was **1.194 and 1.250**, close to the 1.20 target. Linear extrapolation is good enough inside about 60 pts, and the far levels it biases are rarely touched.
  - **Theta:** straddle decay on the tapes was 24–45 pts per session (e.g. 16 Oct ATM straddle 222 → 177). That is ~1 option pt per leg per 20-min hold, or ~1.5–2 spot pts of drift away from spot. Negligible next to the 25-pt floor.
  - **Vega:** ~8 option pts per IV vol-point, about **13 spot pts per vol-point** of shift in the true pain spot. The 30-day VIX moved 0.3–0.9 pts within these sessions, and weekly IV moves more. This is the largest error, and the formula has no IV term between anchors.
  - **Delta estimator:** `|C(K−50) − C(K+50)|/100` (trigger_engine.py:123) is the strike derivative, N(d2), not N(d1). The gap is ~φ·σ√T ≈ 0.005 at DTE 1–4, which is negligible. Observed Δ was 0.53–0.65 (mean 0.59 CE / 0.56 PE). The 0.30/0.98 clamp never bound.
- Fix: none needed for correctness. **PROPOSAL (low value):** add the gamma term from the same three quotes, using Γ ≈ (C(K−50) − 2C(K) + C(K+50))/50², and solve Δx + ½Γx² = target. The data says this would change little.

### [LOW] The 25-pt floor is not honoured after rounding (trigger_engine.py:179) — reproduced
- `round_to(S + up, 5)` rounds half-up to the nearest 5, so a floor-bound level lands 22.5–27.5 pts from spot. Observed distances: 22.7 (16 Oct 11:20), 22.8 (15 Oct 11:40), 22.9 (16 Oct 14:20).
- With 7-pt proximity, an entry can fire after a 15.7-pt move, which is less than the median 20-min excursion. config line 151 ("never closer than 25 pts") and the test (test_v2.py:100, which allows `25 − 2.5`) both overstate the guarantee.
- Fix (**PROPOSAL**, changes entry timing slightly): round away from spot for MTM levels, i.e. `ceil((S+up)/5)*5` and `floor((S−dn)/5)*5`.

### [LOW] "Reduces exactly to the 1.x formula when time value is zero" is false (trigger_engine.py:150, vajra_config.yaml:147) — read in code
- With zero extrinsic and Δ→1, MTM gives K + 1.20·VWAP − basis, while legacy gives K + 1.10·VWAP − basis. The pads differ (`mtm_pad` 0.20 vs `pad_floor` 0.10). Delta is also clamped at 0.98, not 1.
- test_v2.py:104-114 passes only because its synthetic VWAPs are tiny and the tolerance is 5 pts.
- No effect on Monday, because expiry takes the `compute_band` path. Fix: correct the comment and docstring.

### [LOW] The delta uses neighbour LTPs that may be stale (trigger_engine.py:117-124) — plausible
- The neighbours' last trade prices can be seconds apart. Bid/ask are on the tape.
- Fix (PROPOSAL): use the mid when the spread is under 1 pt.
- Impact is small: Δ stayed within 0.53–0.65 at every anchor.

## What 2.0 does better than 1.0 (evidence)
- Its levels are reachable off expiry. The 1.0 legacy level was approached in 0 of 33 hold windows (median distance 101 pts). In my sim it produced 0–1 trades across all phases, and 1.0's own order_tape shows only one clean non-expiry trade (15 Oct 10:00); its other entries came on the broken 15:20 feed.
  - MTM 0.20/25: 30% of windows approach the upper level, 9–15 trades over the three tapes, mostly profitable at every phase except +5 min.
- The side it picks makes sense for a breakout buyer. It trades in the direction where the 1-ITM option is above its VWAP, and that beat symmetric fixed bands at every phase.
- Expiry is isolated cleanly. `_build_band` sends expiry days to the exact 1.0 `compute_band` call (orchestrator.py:263-266), so Monday 19 Oct is unaffected by anything in this report.

## What 2.0 does worse than 1.0 (evidence)
- Its theory is mislabelled. The documentation and dashboard sell a "writers' pain" level, but 45% of levels are a plain 25-pt offset, already well past pain when touched (LTP/VWAP at touch 1.21–1.58). The CHANGELOG half-width claim (35–80 median) does not reproduce.
- The level moves more at each re-anchor than 1.0's. On 15 Oct the mean |jump| per re-anchor was MTM U 26.3 / L 25.3 vs legacy 22.1 / 19.3 (spot 27.0); on 16 Oct MTM 15.0 / 10.0 vs legacy 13.9 / 5.6. The floor-bound side simply follows spot. Results therefore depend on anchor phase (see the grid table).

## Tests / checks you ran
- `python3 -B -I extract.py <date>` (×3) streamed NIFTY rows from the tick tapes: 27,564 / 79,746 / 60,030 rows, 100% ATM±4 coverage.
- `python3 -B anchors.py` gave the per-anchor table (Δ, extrinsic, LTP/VWAP, raw/MTM/legacy distances, approach flags, LTP/VWAP at touch). Summary: `median mtm up/dn 25.4/104.6, legacy 84.0/119.2; floor-bound up 0.76 dn 0.12; legacy-bound 0.0; approach mtm up 0.30 dn 0.06, legacy 0/0`.
- `python3 -B trades.py` / `trades2.py` / `grid.py`: the trade-proxy sims above (12 band specs × 4 grid phases), with trade lists.
- Cross-checked my legacy distances against 1.0's level_tape re-anchors (e.g. 14 Oct 13:46: U 22772 / L 22507 at spot ≈ 22600). Same order of magnitude; 1.0 anchors on a grid that starts with the feed rather than at 09:20.
- Checked vwap_source on the tapes: 100% `feed`.
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_v2.py` → 24 passed.
- Caveats:
  - The sim is a proxy, not an engine replay (no full replay was run). It fills at LTP and ignores slippage, the confluence display and approach-sound episodes.
  - The sample is 2.3 partial sessions. Every parameter statement above should be read with that in mind.
