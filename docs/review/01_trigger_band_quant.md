# 01 — Gamma-trigger band (§5.3) quant review: findings + NON-EXPIRY NARROWING design

Scope read: entire `/home/claude/vajra2` tree (spec, manuals, config, all `vajra/*.py`, engine
modules, tests, scripts). Quant work: NIFTY rounds + full ATM±6 chains extracted from the three
`tick_tape.parquet` files (27,564 / 79,746 / 60,030 rounds), replayed through a NIFTY-only
mini-replay that reproduces the engine's anchor policy (20-min hold, post-exit + post-cooldown
re-anchor), 7-pt anticipatory entry, ATM buy-at-ask, the locked exit ladder (SL −12 %, trail arms
+10 %, +7 % floor, 6 % give-back), 660 s / 15 s cooldowns, all-in sizing on ₹1,00,000, lot 65.
Scripts: `/home/claude/vajra2_review/scratch/01/{extract_nifty,build_rounds,baseline_band,mini_replay,anchor_compare}.py`,
raw output in `mini_replay_out.txt`, `mini_replay_out2.txt`. **Entries were cut off at 15:10 and
flattened 15:24:59 in every run so the 15:15 broken-feed window (A2) can never produce a trade.**

A fact that reframes the whole exercise (confirmed by running — 15-min OHLC of the tapes):
the "spot ranges" of 190 / 475 / 303 pts are inflated by the 15:15–15:20 garbage ticks. The
**genuine** intraday ranges on the recorded portions were **≈97 pts (10-14, 13:26→15:15),
≈280 pts (10-15, a one-way up-trend 22,747→23,056), ≈80 pts (10-16, 11:11→15:15)**. Two of the
three days were dead chop days; on an 80-pt day no level system with a −12 % hard stop on a
~110-pt ATM premium (≈ 25 spot pts of adverse move) can be "productive". Every candidate below
lost money on 10-16. The honest headline: narrowing makes the engine *reachable*; it cannot
make a chop day profitable, and the manual's "zero trades on a quiet day is correct" was
P&L-correct on 10-16.

---

## 1. Correctness defects (most severe first)

### [HIGH] Basis ABRUPT test uses a 30-ROUND std window = 7.5 s at 4 Hz → REVERSAL_WATCH flood  (vajra/engine/basis_monitor.py:38,48,70-78)  certainty: confirmed-by-reading (+ A3 counts)
- What happens: `std_window=30` is hard-coded in *rounds*. At `poll_interval_secs: 0.25` that is a
  7.5-second sample of Δbasis, almost all of which are zero (noise floor) with an occasional 1–3 pt
  tick → `std` is tiny and any single Δbasis ≥ noise floor clears `abrupt_k × std`. Hence 1,351
  REVERSAL_WATCH on 10-15 (A3). The spec's "rolling_std(D_BASIS)" and `ewma_halflife 5 min`
  assume a minute-scale window.
- Fix: make the std window time-based (e.g. `std_window_secs: 300`, push one sample per
  ≥ 15 s), or sample Δbasis per minute bar. Also require `|d_eff| ≥ max(noise_floor, 0.05 % spot)`.

### [HIGH] No plausibility gate on spot / implied basis; garbage anchors written to level_tape  (vajra/engine/basis_engine.py:55-56, orchestrator.py:649,720-750)  certainty: confirmed-by-running
- What happens: on 10-16 at 15:22:17 the engine re-anchored (`post_exit`) with basis **−187.95**
  (U 23,483 / L 23,228) and on 10-15 at 15:25 with basis −74/−78; all from the frozen-then-jumping
  cash feed (A2). Nothing rejects a basis that is physically impossible for NIFTY (|basis| 188 on
  a 1-day weekly ≈ 0.8 % — real |basis| on these tapes is 2–22 pts).
- Fix: in `_process_instrument` after `compute_basis`: if `abs(basis) > max(30, 0.0015*spot)` →
  `FEED_GAP(IMPLAUSIBLE_BASIS)`, skip the round for NIFTY (no band, no entry, no re-anchor); plus a
  frozen-spot detector (spot unchanged ≥ 60 s while ATM option ltp changed → `FEED_GAP(FROZEN_SPOT)`),
  and `execution.last_entry: "15:10"` (today entries are allowed until 15:25).

### [MEDIUM] Entry strike taken from the HELD band's ATM, up to 20 min stale  (vajra/execution.py:184 via orchestrator.py:596-602)  certainty: confirmed-by-reading
- What happens: `select_strike(direction, band.atm_strike, step)` uses the ATM at anchor time.
  With the baseline band a 100-pt drift is *required* to reach a level, so the "ATM" bought is
  typically 2 strikes off (e.g. 10-15 10:08 entry: band ATM 22,900 while spot 22,970 → a 70-pt ITM
  CE was bought, premium floor/sizing/exit % all computed on the wrong instrument). With any
  narrowing it is 1 strike off half the time.
- Fix: pass the current `compute_basis(nifty_snap, step, mode).atm_strike` into `enter()`
  (spec §7: "Default to ATM or 1-strike-ITM").

### [MEDIUM] `pad = max(0.10, pad_floor)` makes PAD un-tunable downward  (vajra/engine/trigger_engine.py:69; spec §5.3 line 144)  certainty: confirmed-by-reading
- `replay.py --set trigger.pad_floor=0.05` (the README's own example of a perturbation) is a
  silent no-op. The spec text is the bug: a floor on the pad with no way to lower it. Fix: `pad = float(pad)`
  (config `trigger.pad: 0.10`), keep a separate sanity clamp `0 ≤ pad ≤ 1`.

### [MEDIUM] `RECALC_TRIGGERS` enum lacks `post_cooldown`  (vajra/schema.py:25 vs orchestrator.py:565,573)  certainty: confirmed-by-running (level_tape 10-15 has 2 `post_cooldown` rows)
- Fix: add it (and `open`, see next) and make `_level_row` assert membership.

### [LOW] First intraday recompute after a seed is labelled `interval`, never `open`  (vajra/engine/freshness_engine.py:61-71)
- `seed_from_prev_close` calls `mark_recomputed`, so `_last_attempt_ms` is set and the 09:20 calc
  takes the `interval` branch. `level_tape` on a seeded day never contains `open` (spec §5.4 says it
  must). Fix: track `_seeded=True` and return `"open"` for the first post-seed recompute.

### [LOW] Stale June seed: a 1,500-pt-away band and a June timestamp inside the October partition  (orchestrator.py:691-718, feed.py:216-236)  certainty: confirmed-by-running
- 10-14 `prev_close_seed` row: ts 15:30:05 of the June day, ATM 24,050, U 24,131 / L 23,941 while
  NIFTY was 22,600. `level_tape.ts` is therefore non-monotonic. Fix: stamp the seed row with the
  session start ts, and refuse (SESSION_ERROR) a seed whose spot differs > 3 % from the first live spot.

### [LOW] `nonexpiry_mode: trade_small` does not size down  (orchestrator.py:220-228, execution.py:167-174)
- Spec §8.4 "low-size mode". Fix: `execution.nonexpiry_capital_frac: 0.5` applied in `compute_lots`
  when `not is_expiry_day`.

### [LOW] Dashboard "T-N to expiry" counts calendar days, not trading days  (dashboard.py:657-658)
- Shows T-5 for Wed 14 → Mon 19. Fix: send `trading_days_to_expiry` from the engine (needed for §3 anyway).

---

## 2. Theory: why the band is wide off expiry, and what the correct generalisation is

Write the 1-ITM call price as `C = (S − K) + basis + E`, where `E = P(K)` is the same-strike OTM put
(put-call parity, forward space). The locked level is

```
UPPER_expiry = K_CE + (1+pad)·V_CE − basis                      # V = session VWAP of the ITM call
```
= the *expiry* breakeven of the average writer, padded. Rearranged against the anchor spot:
`UPPER − S ≈ pad·V + (V − C) + E`. On expiry day `E ≈ 5–15` and the band hugs spot. Off expiry
`E ≈ 85–130` (measured: 1-ITM extrinsic 10-14 ≈ 125, 10-15 ≈ 95, 10-16 ≈ 70) — that extrinsic is
the *only* reason the half-width is 90–135 pts. Writers do not wait for expiry to feel pain:
they cover when the option trades above what they sold it for. The **mark-to-market pain level**
is the spot at which the ITM option would trade `pad` above its VWAP *now*:

```
UPPER_mtm = S + ( (1+pad)·V_CE − C_now(K_CE) ) / Δ_CE
LOWER_mtm = S − ( (1+pad)·V_PE − P_now(K_PE) ) / Δ_PE
Δ ≈ |C(K−step) − C(K+step)| / (2·step)   (finite difference on the live chain, clamp [0.30, 0.98])
```
Key property (checked algebraically): on expiry day `E→0`, `Δ→1`, `C_now = S + basis − K`, so
`UPPER_mtm = K_CE + (1+pad)·V_CE − basis` **exactly the locked formula**. MTM is therefore not a new
rule but the same rule evaluated at *today's* clock instead of expiry's — the "theta compresses the
band" sentence in §5.3 made rigorous. Candidate (b) (time-scaling) is automatically inside it: the
extrinsic and delta carry the time-to-expiry. Candidate (a) (strip f·E) is the Δ=1 special case.

The MTM level is naturally **asymmetric**: on a trend day the trend-side ITM option already trades
above its VWAP (writers already underwater), so that side's level sits at/under spot; the other
side is far. Example (10-15, 11:28 anchor, S 22,956): baseline +71/−164; MTM +25 (floor)/−118.

---

## 3. Measured results (mini-replay, NIFTY-only, 15:10 entry cutoff)

Half-width = median (U−L)/2 over anchors; `zone` = minutes with spot within 7 pts of/beyond a
level; `ep` = edge-triggered approach episodes; trades/wins/SL/P&L per the locked exit ladder.

| candidate | 10-14 half / zone / ep / trades (W, SL, P&L) | 10-15 (TDE 2, trend) | 10-16 (TDE 1, chop) | total trades / W / P&L |
|---|---|---|---|---|
| **0 baseline** `K ± 1.1V − b` | 132 / 0 / 0 / 0 | 115 / 2 / 2 / 1 (1,0,+10.6k) | 90 / 0 / 0 / 0 | 1 / 1 / **+10.6k** |
| a strip f=0.5 (pad on stripped) | 68 / 14 / 50 / 2 (0,2,−21.7k) | 58 / 154 / 366 / 10 (8,2,+49.9k) | 45 / 42 / 137 / 5 (1,4,−35.1k) | 17 / 9 / −6.9k |
| a strip f=0.75 | 35 / 64 / 71 / 4 (1,2,−17.8k) | 28 / 297 / 60 / 16 (11,5,+76.6k) | 25 / 127 / 273 / 11 (5,6,−30.4k) | 31 / 17 / +28.4k |
| a strip f=1.0 (pad on stripped) | **1** (degenerate) | 0 | 9 | 35 / 21 / +51.0k — band glued to spot, 11–17 trades/day |
| a strip f=1.0, pad 0.20 on full V | 30 / 81 / 103 / 5 (2,2,−16.1k) | 25 / 309 / 84 / 17 (11,6,+65.1k) | 25 / 169 / 326 / 10 (5,5,−22.9k) | 32 / 18 / +26.1k |
| e MTM pad 0.10, floor 15 | 45 / 32 / 44 / 3 (2,1,+0.6k) | 51 / 159 / 437 / 12 (9,3,+50.5k) | 32 / 56 / 176 / 7 (3,4,−22.7k) | 22 / 14 / +28.3k |
| e MTM pad 0.15, floor 20 | 55 / 26 / 78 / 3 (1,1,−9.2k) | 65 / 105 / 168 / 11 (8,3,+39.4k) | 42 / 33 / 86 / 5 (2,3,−21.7k) | 19 / 11 / +8.5k |
| **e MTM pad 0.20, floor 25** | 65 / 14 / 47 / 2 (0,1,−15.6k) | 80 / 65 / 138 / 9 (**8,1,+68.7k**) | 55 / 32 / 83 / 4 (1,3,−19.1k) | **15 / 9 / +34.0k** |
| e MTM pad 0.30, floor 30 | 85 / 0 / 0 / 0 | 100 / 64 / 147 / 6 (4,2,+28.5k) | 70 / 18 / 54 / 4 (1,3,−25.8k) | 10 / 5 / +2.7k |
| e MTM pad 0.20, floor 0.15×straddle (≈40) | 72 / 0 / 0 / 0 | 80 / 29 / 102 / 7 (4,3,+1.8k) | 56 / 16 / 41 / 3 (1,2,−16.7k) | 10 / 5 / −14.9k |
| e MTM pad 0.20, **disarm** already-underwater side | — / 0 | — / 0 | — / 0 | 0 trades |
| c cap 60 pts (= ±60 breakout) | 60 / 0 / 0 / 0 | 60 / 12 / 17 / 2 (2,0,+27.9k) | 60 / 6 / 25 / 1 (0,1,−11.9k) | 3 / 2 / +16.0k |
| c cap 45 pts | 45 / 11 / 46 / 1 (0,1,−11.6k) | 45 / 20 / 25 / 2 (2,0,+38.1k) | 45 / 4 / 8 / 2 (0,2,−22.3k) | 5 / 2 / +4.2k |
| c cap 0.35×ATM straddle | 97 / 0 | 82 / 8 / 21 / 1 (1,0,+11.8k) | 66 / 0 | 1 / 1 / +11.8k |
| c cap 0.25×ATM straddle | 71 / 0 | 66 / 9 / 20 / 2 (2,0,+17.6k) | 50 / 7 / 17 / 1 (1,0,+6.1k) | 3 / 3 / +23.7k |
| d max-OI CE/PE strikes | 100 / 0 | 150 / 28 / 55 / 4 (2,2,−3.0k) | 75 / 17 / 44 / 2 (2,0,+15.6k) | 6 / 4 / +12.6k |
| bench ±40 breakout | 40 / 6 / 17 / 2 (1,1,−3.1k) | 40 / 46 / 150 / 8 (4,4,−17.4k) | 40 / 10 / 34 / 3 (1,2,−16.7k) | 13 / 6 / −37.2k |

Reading the table honestly:
1. **Baseline is unreachable off expiry** (1 approach in 3 days; the engine's recorded 15:20 "trades" were A2 artefacts). Confirmed.
2. **Pure extrinsic strip (a, f=1) degenerates**: with pad on the stripped premium the half-width is ~0–10 pts (V−E ≈ intrinsic ≈ 50, pad 5 pts) → 11–17 trades/day, 6 stops/day. Reject. f = 0.5–0.75 is an arbitrary blend with no reduction to the expiry formula; reject as a primary design (the time-scaled f(D)=1−√((D−1)/D) gives f = 0.18/0.29/1.0 for D=3/2/1 — too wide, then degenerate; also rejected).
3. **The MTM (e) family is the only candidate that is both theoretically closed (reduces to the locked formula on expiry) and trades at a sane rate (2–9/day)**. pad 0.20 / floor 25 dominates its neighbours: 15 trades, 60 % wins, +34k; on the trend day 8/9 wins with one stop; losers on both chop days.
4. **Where the MTM trades came from matters**: the `disarm` variant (ignore a side whose level is already inside the floor, i.e. the option is already > pad above VWAP) produced **zero** trades — every winning MTM trade on 10-15 was taken on the floor-clamped trend side: "writers already 20 % underwater → buy the continuation 25 pts out". That is the §2.5 thesis (strong hands covering fuels the move) and it worked on the one trend day, but it is a momentum-continuation bet, not a level touch, and it is what produced the 10-14 / 10-16 stops. The genuinely "out there" MTM levels (50–100 pts) produced 10 trades / 5 wins / −15k.
5. **Simple caps are not stupid**: cap 0.25×straddle went 3/3 (+23.7k) with half-widths 50–71 — but 3 trades is noise, and a cap turns the level into "anchor spot ± X" with no writer semantics.
6. **Max-OI strikes (d)** gave 6 trades / 4 wins / +12.6k; walls were 50–200 pts away, approached ~2×/day. Useful as a *second* trigger family, not as the band (the wall is a pin as often as a launch).
7. Three days (one trend, two chop) is far too little to claim edge; the lead's harness over more tapes must decide pad/floor. What I *can* say: the ordering baseline < cap < MTM in reachability is structural, and the MTM formula is the only one that keeps the expiry-day behaviour byte-identical.

---

## 4. Recommendation for operator ask A (non-expiry levels) — ship this as the default

**Formula (non-expiry sessions only; expiry sessions keep the locked §5.3 formula untouched — MTM
equals it there anyway):**
```
E_CE  = ltp(K_CE, PE)                      # extrinsic of the ITM call via parity (same-strike OTM put)
E_PE  = ltp(K_PE, CE)
Δ_CE  = clamp(|ltp(K_CE−step,CE) − ltp(K_CE+step,CE)| / (2·step), 0.30, 0.98)   # fallback 0.65
Δ_PE  = clamp(|ltp(K_PE+step,PE) − ltp(K_PE−step,PE)| / (2·step), 0.30, 0.98)
UPPER = roundto( S + max( ((1+pad_mtm)·V_CE − ltp(K_CE,CE)) / Δ_CE , floor_pts ), 5 )
LOWER = roundto( S − max( ((1+pad_mtm)·V_PE − ltp(K_PE,PE)) / Δ_PE , floor_pts ), 5 )
```
All inputs are already on the tape (`level_tape` must gain `e_ce, e_pe, delta_ce, delta_pe,
band_mode` columns — schema bump 1.1.0). Basis is *not* subtracted: MTM is computed in spot space
(S and ltp are simultaneous), which also removes the implied-basis noise from the level.

**Defaults (config keys to add under `trigger:`):**
```yaml
trigger:
  pad: 0.10                 # replaces pad_floor (remove the max(0.10, ·) clamp)
  nonexpiry_band: mtm       # legacy | mtm   (expiry day always legacy)
  mtm_pad: 0.20             # writers must be 20 % underwater on the ITM VWAP (10 % → 22 trades, 15 % → 19, 30 % → 10)
  mtm_floor_pts: 25         # level never closer than 25 pts (≈ 12 % hard-stop distance on a 110-pt ATM); floor, NOT disarm
  mtm_delta_fallback: 0.65
  mtm_cap_straddle: 0.0     # optional cap of the far side at k×ATM straddle (0 = off; 0.35 changed nothing on these tapes)
execution:
  last_entry: "15:10"
  nonexpiry_capital_frac: 0.5
```
Why these and not the alternatives: pad 0.20 is the smallest pad that cut the chop-day stop count
without killing the trend day (0.10 → 8 stops / 3 days, 0.20 → 5, 0.30 → 5 but half the winners);
floor 25 ≈ the spot distance of the −12 % hard stop at Δ≈0.5, so an entry at the floor is at least
one stop-distance from "already breached"; the floor must *clamp*, not disarm, because the
already-underwater case is the thesis's squeeze and was the only source of profit here. Expected
behaviour: half-width 55–80 pts off expiry (vs 90–135 today), ~3–9 approaches/day, 2–9 trades/day,
converging to today's ±25–35 on expiry day with zero change to expiry-day decisions (the
determinism/golden tests stay green because the expiry branch is untouched).

Also recommended alongside (not part of the formula): (i) the plausibility gates in §1 (otherwise
the first narrow band meets the 15:15 feed break and fires garbage); (ii) current-ATM strike
selection (§1, MEDIUM); (iii) keep `hold_min_secs: 1200` — with a 60-pt band the 20-min re-centre
is what makes the band reachable, don't lengthen it; (iv) log `band_mode` into decision_tape so
the lead's harness can split expiry/non-expiry P&L.

What NOT to ship: extrinsic-strip f=1 (degenerate), ±N-pt breakouts (−37k), time-scaled f(D) (wide
then degenerate), max-OI as the band (keep as a surfaced context/second trigger).

## 5. Things that are fine and should NOT be changed
- The locked expiry-day formula `K ± round(1.1·VWAP) − basis` and the round-half-up (`mathx.round_to`): correct, golden-tested, and MTM reduces to it.
- Held levels (anti-goalpost, §5.4) and the 20-min NIFTY re-anchor: a band that follows spot tick-by-tick can never be approached.
- 7-pt proximity: with 55–80-pt half-widths it is ~10 % of the half-width; fine. Do not widen it to "fix" reachability.
- Using the same-strike OTM counterpart for extrinsic (parity) rather than a Black-Scholes model: no fabricated IV, all on the tape (§0.4).
- Operator rule "zero trades on a quiet day is correct": P&L-true on 10-16; the fix is reachability, not forcing trades.

## 6. Tests I would add
1. `test_mtm_reduces_to_legacy_on_expiry`: build a chain with E=0, Δ=1 (C = S+b−K); assert `UPPER_mtm == K_CE + round(1.1V) − basis` to the golden 24125/24035.
2. `test_mtm_floor_clamps_not_disarms`: ITM call ltp = 1.5·VWAP → UPPER == round(S + 25).
3. `test_mtm_delta_fallback`: missing neighbour strikes → Δ=0.65 used, band `ok=True`, `delta_source="fallback"` on the tape.
4. `test_implausible_basis_is_feed_gap`: basis −188 on a 23,000 spot → FEED_GAP(IMPLAUSIBLE_BASIS), no level_tape anchor, no entry.
5. `test_frozen_spot_detector`: spot unchanged 70 s while ATM ltp moves → FEED_GAP(FROZEN_SPOT) and entries blocked.
6. `test_entry_strike_is_current_atm`: band ATM 22,900, spot 22,970 → buys 22,950/23,000 (current ATM), not 22,900.
7. `test_basis_monitor_std_window_is_time_based`: 4 Hz feed with 1-pt jitter must not flag ABRUPT more than N times/hour.
8. `test_recalc_trigger_enum_complete`: every `recalc_trigger` the orchestrator can write is in `RECALC_TRIGGERS` (post_cooldown, open).
9. `test_pad_is_tunable`: `trigger.pad=0.05` changes `raw_upper`.
10. Replay-level: on the 10-15 tape with `nonexpiry_band: mtm` the first approach is at ≤ 09:50 and no entry has ts ≥ 15:10.
