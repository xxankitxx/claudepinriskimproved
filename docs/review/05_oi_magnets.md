# 05 — Pin locator / OI-magnet reviewer: findings

Scope: `vajra/engine/pin_locator.py` (§5.2), its use in `orchestrator.py`, the ATM±6 window (`feed.py`), the two-ATM question, and OI as a non-expiry trading input.
Scripts and extracts: `/home/claude/vajra2_review/scratch/05/` (`lib.py`, `extract_nifty.py`, `analysis_a..e.py`). My replica of `_max_oi` matches the recorded `level_tape.max_oi_*` in 100% of rounds on all three days (27,564 / 79,746 / 60,030 NIFTY rounds).
All statistics exclude the post-15:15 garbage-feed tail unless stated. Sample = 3 non-expiry days, so everything below is descriptive, not significant.

## 0. Corrected day ranges (excluding the 15:15+ frozen/garbage feed)
| day | live window | live spot range | range incl. garbage tail (lead's figure) |
|---|---|---|---|
| 10-14 | 13:26–15:15 | 22571–22668 = **97 pts** | 190 |
| 10-15 | 09:41–15:15 | 22745–23056 = **311 pts** (genuine one-way trend to 13:30, then 130 pt giveback) | 475 |
| 10-16 | 11:11–15:15 | 23061–23141 = **80 pts** | 303 |

10-14 and 10-16 are 80–100 pt days; no sane level set produces many trades on them. Only 10-15 is a real trend day.

## 1. Correctness defects (most severe first)

### [HIGH] max_oi_ce/pe is not sticky: it flickers when the sliding ATM±6 window drops/admits a strike (pin_locator.py:35-42, feed.py:360-377)  certainty: confirmed-by-running
- 10-15: `max_oi_pe` changed 84 times between 22700 and 22800; **all 84 are window artifacts** (the 22700 PE strike leaves/enters the window when spot-ATM flips 23000↔23050 around 23025). Zero genuine OI-rank flips.
- Any consumer (dashboard "OI MAG", future trading logic) sees a level jump of 100 pts with no market event.
- Fix: keep a day-long per-strike OI table (subscribe a fixed wider grid, see §1 window item), choose walls from that table, add hysteresis (change the wall only if the challenger leads by >10% for >=3 consecutive OI updates).

### [HIGH] "max_oi_ce" is often not a resistance and "max_oi_pe" not a support — no side restriction (pin_locator.py:35-41)  certainty: confirmed-by-running
- The argmax runs over all window strikes, including ITM ones. 10-16: max_oi_ce = 23100 was **below spot 78% of the time**; 10-15: 23000 was below spot 32% (after the break). Once broken it is a stale, already-spent wall.
- A restricted (CE strikes > spot, PE strikes < spot) definition fixes semantics but then flips on every crossing (209 flips on 10-15, 189 on 10-16) — so a wall needs hysteresis AND a "broken" state, not a naive argmax.
- Fix: return per side `wall_above` / `wall_below` = best OTM strike by OI, with a latch (see §2).

### [MEDIUM] Spec §5.2/§8.5 promises are not met: ATM-by-future and distances are dead fields; level_tape logs only strike numbers (pin_locator.py:21,24-25; orchestrator.py:832-844)  certainty: confirmed-by-reading
- `atm_by_future`, `dist_spot_to_max_oi_ce/pe` are computed but never read anywhere (grep-verified). `level_tape.atm_strike` is the band's **spot-based** ATM, not the pin locator's future-based one, contradicting §5.2 "Emits the chosen ATM".
- OI values/margins are not logged, so wall quality (see §3 flatness) can't be audited from level_tape; only recoverable from tick_tape. `_level_row` writes `0` for None (int64 column), indistinguishable from a strike "0".
- Fix: add `atm_fut`, `wall_up`, `wall_dn`, `wall_up_oi`, `wall_dn_oi`, `wall_up_margin` (#1/#2 OI ratio), `dist_*` to LEVEL_TAPE; use nullable columns.

### [MEDIUM] Zero unit tests for pin_locator (tests/ has no reference to locate_pin)  certainty: confirmed-by-reading
- Tie-break, empty chain, window edges, OI-only-on-one-side are all untested. Synthetic data (`synthetic.py:131-133`) injects artificial OI walls at ATM±2 steps, so the synthetic gate would "validate" any OI-wall logic trivially — do not use it as evidence.

### [LOW] Tie-break logic (pin_locator.py:38)  certainty: confirmed-by-reading + data
- Key `(oi, -|strike-spot|, -strike)` = highest OI, then nearest **spot** (docstring/spec say future), then lowest strike. In real data exact OI ties never occurred (OI is in 65-lot multiples ~1e6–2e7). Harmless; but if all OI were 0 (feed without OI) the function returns the strike nearest spot as a fake "magnet" instead of `None`. Tape shows zero all-zero rounds, so latent only. Fix: return None when max OI <= 0; tie-break by future for consistency.

### [LOW] Per-round cost: locate_pin sorts the chain twice for all 24 instruments at 4 Hz although the result is only used for NIFTY (orchestrator.py:303)  certainty: confirmed-by-reading
- Compute only for NIFTY, or only when OI changes (OI updates once a minute, see §3).

### Window ATM±6 (config chain.strikes_each_side=6)  certainty: confirmed-by-running
- For *nearest-wall* purposes ±6 (±300 pts) is enough: the next wall in the direction of travel is 50–250 pts away. 10-15 uptrend never starved the window of upside strikes; incomplete windows (16–25 of 26 strikes) occurred only briefly at start/restarts.
- For the *global* max-OI it is **not** enough: on 10-15 the PE argmax sat at the window edge (|offset|=6) 27% of the time and at |offset|>=5 for 78% — the true put wall is outside the window (censored), and the "put wall" p50 distance was 251 pts (p90 312) i.e. unusable.
- Recommendation: keep ±6 for the band; for walls subscribe a **fixed day grid** (prev-close ATM ±10 strikes, 100-multiples only is enough, see §3) and never let it slide. NIFTY-only cost: ~11 extra strikes x2 rights.

### Two ATMs: spot-ATM (compute_basis, basis_engine.py:38) vs future-ATM (pin_locator.py:29)  certainty: confirmed-by-running
- They disagree in **22% / 8% / 34%** of live rounds (10-14/15/16) because the median basis is −5 to −14 pts and spot sits near 25-pt rounding boundaries (basis noise alone moves it).
- Which should drive K_CE/K_PE? Keep **spot-ATM for the implied-future parity** (needs liquid, near-money strikes; golden-tested) but the ITM strikes for the band should not depend on a rounding that flips on a 25-pt boundary. When they differ, a future-ATM band is on average 19–32 pts lower (upper) and 14–32 pts lower (lower) and its half-width differs by 7–8 pts (133→140, 116→107, 96→88). Differences are of the order of the band's own noise, so **neither ATM is "right"**; the real defect is that the level jumps ±50 pts at each 20-min anchor when spot is near a boundary.
- Fix (belongs with band reviewers): interpolate VWAP-intrinsic between the two bracketing ITM strikes by the fractional position of the future between strikes instead of choosing one strike. Delete the unused `atm_by_future` or log it.

## 2. Improvements / design proposals (ranked)
1. **Do not make raw max-OI strikes the entry levels.** Evidence in §3: the levels are as far as the current band (p50 80–250 pts), static all day, and on a pinned day produce a whipsaw machine (10 touches at 23100 on 10-16, 1 of 10 broke >=15 pts beyond within 10 min; mean net move in break direction −0.4 pts).
2. **Use OI as a regime filter + wall latch (cheap, testable):**
   - `wall_up` = OTM CE strike with max OI among the first 6 strikes above spot; `wall_dn` likewise; both latched with hysteresis and a state `INTACT / BROKEN(ts)`.
   - **Pin regime flag**: `PINNED` when spot has stayed within ±0.25% of a strike holding >=15% of window OI (both CE and PE large — 23100 on 10-16: CE 19.2M + PE 16.1M) for >=60 min and day range < 100 pts. In PINNED, suppress breakout-anticipation entries on levels within 40 pts of that strike (they are mean-reverting) — or, if the operator wants activity, fade instead. This is the one place OI gives non-redundant information.
   - **Wall-break entry (the gamma trade):** enter only on a *close beyond* the wall by >=10 pts held 60 s with volume ratio >=1.2 (reuse ContinuationValidator with the wall as the level); size down (`trade_small` should mean ~50% lots). 10-15's 23000 break is the only example in the data: break 12:30, +55 pts by 13:30, fully retraced by 14:40 — a trade the 6% trail would have banked only if the exit worked; one sample.
3. **dOI as confirmation is weak and slow:** OI updates **once per ~60 s** per strike (median gap 60.1 s, p90 ~80 s, same cadence for every strike, 104–107 updates/hour on 10-14, 242 in 4 h on 10-16). A 15-min PE-build minus CE-build skew vs the next 15-min move: corr = 0.13 over 316 minute-samples (effective independent n ≈ 21) — indistinguishable from zero. Log it, do not gate on it.
4. **Max pain as a magnet: do not trade it.** Window-restricted max pain (±6) sat within 12–25 pts of spot on the two quiet days (consistent with a pin) but 64 pts below spot (p50) on the trend day and price never came back to it (close 22941 vs ~22900 at best). It changes with window size (±6 vs ±4 agree 100% / 66% / 90%; ±3 agree 94% / 50% / 84%), i.e. truncation-sensitive. Fine as a dashboard context line, not as an order trigger.
5. Persist `OI` per strike once per minute, not per 0.25 s round (tick_tape is 150–285 MB/day mostly redundant OI/VWAP repeats).

## 3. Evidence for the two operator asks
### A. Non-expiry levels — how far the OI strikes sat and did price react
| day | max-OI CE (engine) / dist p50 | max-OI PE / dist p50 | notes |
|---|---|---|---|
| 10-14 | 22700 all day / 80 (min approach 32: day high 22668) | 22500 all day / 120 (min approach 70) | price stayed inside the wall corridor, never within 7 pts → **0 entries** from walls |
| 10-15 | 23000 all day / 40 (spot crossed it 12:30) | 22700 (86%) / 22800 (14%, artifact) / 251 | PE wall 245–310 pts away = useless; CE break gave +55 then full retrace |
| 10-16 | 23100 all day / 18 (spot hovered ±40 around it all session) | 23000 / 116 | classic pin; 10 touches within 7 pts, 9 failed to extend |
- Distances are **no closer** than the ITM-VWAP band (half-width 90–132): walls would not have created more trades, except by oscillation at a pin (bad trades).
- All argmax strikes were 100-multiples. OI on 100-multiples is 2–5x neighbouring 50-strikes (e.g. 22700 CE 12.9M vs 22650 3.9M), so "nearest big OI strike" collapses to "nearest round-100 level"; the #1/#2 margin among 100-multiples is often 10–15% (10-16: CE 23100 19.2M vs 23200 16.7M), fragile as a signal.
- Placebo comparison (touch within 7 pts, 10 min forward): round-100 non-wall levels: n=3/7 mean "max beyond" 14.5/13.4 pts, "retreat" ~20 pts, i.e. same reaction as the OI walls (5.3/18.1 on the 10-16 pin). **No evidence price reacts differently at max-OI strikes than at any round level.** n is tiny; this is "not demonstrated", not "disproved".
### Recommendation for A
For narrowing levels use band-construction fixes (other reviewers); add OI only as the PINNED filter and the latched wall-break entry in §2. If an extra level is wanted on quiet days, the **nearest 100-multiple that is also an OI peak** within 1.5% of spot is the practical candidate.
### B (sounds): no OI interaction; if a wall-break alert is added give it the existing "approach" sound, no new sound.

## 4. Things that are fine and should not be changed
- Spec §5.2 intent (surface OI magnets as context) and showing max-OI on the dashboard chain.
- OI is populated from round one (zero-OI rows: 0.0000%), no carry-forward artifact seen for NIFTY OI.
- Deterministic tie-break (no ties observed anyway). Spot-ATM for the IMPLIED parity (golden-tested).
- ATM±6 for the ITM band inputs.

## 5. Tests I would add
1. `locate_pin`: empty chain -> None; all-OI-zero -> None; tie -> deterministic; OTM-only wall selection; strike dropping out of window must not change a latched wall.
2. Wall latch: hysteresis (challenger needs >10% for 3 OI updates), BROKEN state on close beyond wall by buffer.
3. Replay regression on 2026-10-15: assert `wall_dn` never flips between 22700/22800 (the 84-flip artifact) and `wall_up` stays 23000 until broken.
4. Pin-regime flag on 2026-10-16 tape fires after 60 min and suppresses breakout entries within 40 pts of 23100.
5. level_tape schema test for the new nullable wall columns and `atm_fut`.
6. Property test: spot-ATM vs future-ATM band differ by < 1 strike step and the interpolated band is continuous across a rounding boundary.
