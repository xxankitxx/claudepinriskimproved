# 02 — Trigger-band mathematics: teardown
Verdict: Ready for Monday. On expiry day the NIFTY band is byte-identical to 1.0 (checked against the code and on real chains). Off expiry the "mtm" band does what it says numerically: no NaN, no inf, never crosses spot, missing strikes are handled. But on the trending side it is not an MTM level at all. 78–94% of the time it is a hard-coded **spot ± 25 pts** (`mtm_floor_pts`), and it can widen a level by up to 2.45 pts. Better than 1.0 for the non-expiry goal; same as 1.0 on expiry day.
Score (your area): 1.0 = 6/10, 2.0 = 6.5/10

Scope read: `vajra/engine/trigger_engine.py` (whole file, diffed against 1.0), `vajra/orchestrator.py:100-130, 259-273, 405-430, 826-840, 1222-1245`, `vajra/regime.py`, `vajra/mathx.py:round_to`, `vajra/feed.py` (snapshot/VWAP), `vajra_config.yaml` trigger/nonexpiry, `tests/test_v2.py` band tests.
Scratch: `/home/claude/teardown/scratch/02/{bands.py,stats.py,sim.py,edge.py,check2.py}`. Every NIFTY tick-tape row on 14/15/16 Oct (167,340 rows) was recomputed with both formulas.

**Sanity check of the method:** recomputing the legacy formula from the tick tape reproduces **every** NIFTY anchor that 1.0 recorded in `level_tape`. That is 7/7 on 10-14, 21/21 on 10-15 and 16/16 on 10-16 (`sim.py`), so the comparisons below use 1.0's real inputs.

## Findings (most severe first)

### [MEDIUM] On the trending side the "mtm" level is just the 25-pt floor, so the band is a fixed momentum trigger (trigger_engine.py:174-176, vajra_config.yaml:151) — reproduced
- The formula: `up_mtm = max(((1+0.20)·VWAP_CE − LTP_CE)/Δ_CE, 25)`. When the ITM option already trades above 1.2×VWAP, the raw value is negative: writers are already more than 20% underwater and the "pain level" is behind spot. The `max(…, 25)` then turns "already breached" into "25 pts further in the trend direction".
- Measured on every session row from 09:20 to 15:15 (`python3 -B stats.py …` and a follow-up snippet):

| day | side bound by floor | raw value < 0 | CE ltp/vwap median | PE ltp/vwap median |
|---|---|---|---|---|
| 10-14 | DN 90% | 35% | 0.88 | 1.18 |
| 10-15 | UP 94% | 86% | 1.39 | 0.84 |
| 10-16 | UP 78% | 30% | 1.16 | 0.87 |

- Simulated anchors at 09:20 and then every 20 min (no trade-driven re-anchors), counting the first approach within 7 pts per anchor (`sim.py`):
  - 1.0 legacy: **1** approach in three days (10-15 10:00, 110 pts from anchor).
  - 2.0 mtm: **13** approaches. **Every one** is 23–26 pts from its anchor spot, i.e. all of them hit the floor. Examples on 10-15: 09:45:55 UP 23.2, 10:06:50 UP 24.0, 10:38:05 UP 23.1 … 13:45:45 UP 25.5.
  - So in practice 2.0 non-expiry trading means "buy the CE after an ~18-pt move with the session trend, re-arm every 20 min / after every exit". The MTM formula is not what produces these trades; the single config constant `mtm_floor_pts: 25` is.
- Is the formula defensible? Only on the counter-trend side. There it gives 30–185 pts, which is a sensible narrowing of 1.0's 80–195. On the trend side the pin-risk rationale (writers defending a strike) disappears, and the level is not "HELD" in any market sense. It is a function of the anchor spot.
- Fix (operator decision, not a code bug): label it honestly on the dashboard as "floor (spot±25)" whenever `raw < floor`, and record `raw_up_mtm`/`raw_dn_mtm` in `level_tape` so the operator can see when the floor is doing the work. If the intent is "writers already underwater means a breakout is in progress", say so and make the floor the explicit, tunable entry rule. Calibrate `mtm_floor_pts` on tape, because it now drives nearly every non-expiry entry.

### [LOW] The "narrow-only" rule can widen a level by up to 2.45 pts; `narrowed` misses it and the test hides it (trigger_engine.py:178-180, tests/test_v2.py:93-101) — reproduced
- Legacy levels are `K + round5(1.1·VWAP) − basis`. With non-integer basis they are **not** on the 5-pt grid (e.g. 22667.15). 2.0 does `round_to(S + min(legacy, mtm), 5)` on the **absolute** level. Whenever the legacy leg binds, this rounding can push the level outward.
- Lower level wider than 1.x: **531 rows** on 10-15 and **1292 rows** on 10-16, max +2.45 pts. Example, 10-15 09:54:10: S=22831.65, basis −2.15, legacy L=22667.15, mtm raw dn = (1.2·215.87−168.4)/0.551 = 164.5, so L = round5(22667.0x) = **22665** (2.15 pts wider). The upper side never widened on these days only because mtm always bound there.
- `narrowed` only tests `upper < legacy_upper` / `lower > legacy_lower`, so it reports False on these rows. The test uses `basis=0.0`, which puts legacy on the grid and masks the bug.
- Fix: clamp after rounding: `upper = min(round_to(S+up,5), base.upper_level)`, `lower = max(round_to(S-dn,5), base.lower_level)` (or round inward: floor for upper, ceil for lower). Add a test with basis = 7.3.

### [LOW] The "never closer than 25 pts" floor is really 22.5–27.5 (trigger_engine.py:175-179, vajra_config.yaml:151) — reproduced
- Half-up rounding of `S+25` to the 5-pt grid. Example, 10-15 09:41:55: S=22751.75, raw up = (1.2·144.38−158.65)/0.65 = 22.5, floored to 25, giving 22776.75, rounded to **22775 (23.25 pts)**. The minimum mtm distance on all three days is **22.5**. The entry then fires after a **15.5-pt** move, not 18.
- Fix: round outward on the floor leg (`ceil` to the grid for upper, `floor` for lower) or document "≈25".

### [LOW] Sister/stock bands differ from 1.0 on expiry day too (orchestrator.py:271-273) — reproduced
- `level_round_step = max(0.05, min(5, 0.1·step))` applies in every regime. Indices are unchanged (BANKNIFTY step 100 → 5, FINNIFTY 50 → 5). Stocks move:
  - RELIANCE (step 10): 1198.00/1158.00 vs 1.0's 1196.00/1156.00
  - HDFCBANK: 727.20/707.20 vs 729.20/709.20 (`check2.py`, EXPIRY regime forced)
- Impact is confluence display/log only (no veto, per rule 3), so not a trading change. Still, "expiry day identical to 1.x" is not literally true for stocks. Arguably this is a fix (finer grid), but it is a behaviour change on Monday that the operator should know about.

### [LOW] `pad_floor` is no longer clamped and has no validation (trigger_engine.py:79-81)
- 1.0 used `max(0.10, pad_floor)`; 2.0 uses `float(pad_floor)`. At the shipped 0.10 the result is identical. With `--set trigger.pad_floor=-0.5`, the band collapses through spot (edge.py: U=24105/L=24075 around spot 24100). That would also hit expiry day.
- Fix: `if not 0 <= pad < 1: raise` at config load.

### [LOW] Non-finite inputs give `ok=True` NaN bands, the same as 1.0 (trigger_engine.py:100-104; orchestrator.py:418-421) — read in code / reproduced synthetically
- NaN VWAP or NaN basis produces NaN levels with `ok=True`, and the orchestrator accepts the band because it checks only `nb.ok`. A NaN neighbour LTP is harmless by accident (`max(nan,25)` gives nan, then `min(leg, nan)` keeps leg). An inf neighbour clamps Δ to 0.98.
- No NaN or inf appeared in any of the 167k real rows. Not a regression.
- Fix: in `compute_band`, return `ok=False` if `not all(map(math.isfinite, (vwap_ce, vwap_pe, basis)))`.

### [LOW] Cosmetic and claim inaccuracies
- The docstring (trigger_engine.py:148) and config comment (vajra_config.yaml:147) say mtm "reduces exactly to the legacy formula" at zero time value. It does not: pad 0.20 vs 0.10, no basis term, Δ clamped to 0.98, a different rounding. With ext=0 the raw mtm value is ≈(1.2V−intrinsic)/0.98, which is greater than legacy, so mtm equals legacy only because of the `min`. The test allows ±5 pts (test_v2.py:114). Harmless, because mtm is never called on expiry day.
- A non-expiry legacy band with `max_half_width_pts>0` is labelled `band_method="legacy"` even though it is capped (check2.py: expiry=False legacy cap=40 gives U/L 22800/22720, method=legacy).
- The dashboard's symmetric "band ±X" (orchestrator.py:1234) hides a strongly asymmetric band. On 10-15 the median is +25 on one side and −126 on the other, while the display shows ±75.
- Δ is a central difference across strikes, |∂C/∂K| ≈ N(d2), not N(d1). For 3–5 DTE near-ATM the gap is about 0.01, so this is fine. The linear Δ ignores gamma: over 100 pts, Δ moves by about 0.1–0.2, so the counter-trend mtm level is roughly 10–15% too far. That errs conservative. Fine.

## Verified fine (briefly)
- **Expiry day = 1.0 exactly for NIFTY.** `_build_band` (orchestrator.py:263-266) returns the plain `compute_band(**_band_kw)` whenever `is_expiry_day`, whatever the method or cap; the mtm kwargs are never applied. Mocked over all 8 regime×method×cap combinations on a real chain (`check2.py`): all 4 expiry combinations are `identical_to_1x=True`. `compute_band` differs from 1.0 only in the pad clamp (identical at 0.10). `cal.active_weekly_expiry(2026-10-19)` gives `(2026-10-19, True)`, so Monday resolves to EXPIRY. The golden/entry-path tests pass (25 passed); the band tests in test_v2 pass (4 passed).
- **No NaN/inf/negative distances, no crossing of spot** on any real row (0/158,258 session rows). The minimum mtm distance is 22.5 pts; 1.0 legacy also never crossed.
- **Missing neighbour strikes:** only 1 real row lacked one; Δ fell back to 0.65 and the band was produced. Synthetic tests: missing neighbour gives 0.65, neighbour LTP 0 gives 0.65, stale equal LTPs clamp Δ to 0.30 (wider, then capped by legacy), inf gives 0.98. A missing ITM strike gives `ok=False`, which is the same as 1.0 and keeps the held band.
- Real Δ is 0.4–0.7. It never hit the 0.30/0.98 clamps.
- **Hand example** (edge.py, spot 24100, basis 10, ATM 24100):
  - Legacy U = 24050 + round5(1.1·130 = 143) = 24195, minus 10, = **24185**. L = 24150 − round5(137.5) = 24010, minus 10, = **24000**.
  - Mtm: Δ_CE = |180−105|/100 = 0.75, so up = (156−140)/0.75 = 21.3, floored to 25, giving **24125**. Δ_PE = |95−155|/100 = 0.60, so dn = (150−120)/0.6 = 50, giving **24050**.
  - The code output matches.

## Half-widths (NIFTY, 09:20–15:15, every tick row)
| day | legacy hw p5/p50/p95 | mtm hw p5/p50/p95 | legacy up / dn median | mtm up / dn median |
|---|---|---|---|---|
| 10-14 (from ~13:00) | 130 / 132.5 / 137.5 | 50 / 65 / 92.5 | 164 / 102 | 104 / **25** |
| 10-15 | 100 / 115 / 125 | 40 / 75 / 92.5 | 78.5 / 152 | **25** / 126 |
| 10-16 | 82.5 / 90 / 100 | 40 / 52.5 / 72.5 | 78.5 / 107 | **25.7** / 82 |

ITM extrinsic (the same-strike OTM LTP) fell from about 128 to 117 on 10-14 and from 111 to 89–95 on 10-15. That confirms the CHANGELOG premise that 1.0's off-expiry band sits roughly 90–135 pts out.

## What 2.0 does better than 1.0 (evidence)
- Off expiry the band is reachable: 13 vs 1 first-approaches over three days on a 20-min grid (`sim.py`). This is the operator's stated goal.
- Narrow-only plus `min` with legacy bounds the downside of a bad Δ or VWAP. Mtm can never be much further out than 1.0.
- Finer sister-stock rounding removes 5-pt quantisation on low-priced stocks.
- A per-anchor audit trail (`legacy_upper/lower`, `anchor_spot`, `delta_*`, `e_*`) in level/anchor rows.

## What 2.0 does worse than 1.0 (evidence)
- It can widen a level by up to 2.45 pts despite the "never widen" claim, and `narrowed` misreports those rows.
- The trend-side level is a parameter (25 pts) dressed up as a market-derived level. The real decision variable is untested and uncalibrated.
- `pad_floor` lost its safety clamp, and that change also applies on expiry day.
- Stock bands change on expiry day (display/confluence only).

## Tests / checks you ran
- `python3 -B bands.py <day>` ×3: full NIFTY recompute, 0 exceptions, 0 not-ok rows.
- `python3 -B stats.py 2026-10-14 2026-10-15 2026-10-16`: distributions, widening counts, spot crossings, Δ clamps, neighbour gaps.
- `python3 -B sim.py <day>`: 44/44 match against 1.0's recorded anchors, plus the approach simulation.
- `python3 -B edge.py`: hand example plus 11 synthetic edge cases (missing/zero/stale/inf neighbours, NaN VWAP/LTP/basis, negative pad, expiry-like chain).
- `python3 -B check2.py`: `_build_band` branch matrix, and sister rounding under EXPIRY.
- `pytest tests/test_golden.py tests/test_entry_path.py` gave 25 passed; `pytest tests/test_v2.py -k "band or mtm or legacy or golden"` gave 4 passed.
