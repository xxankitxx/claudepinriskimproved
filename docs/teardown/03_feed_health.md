# 03 — Feed-health gate (vajra/feed_health.py + every gate site): teardown
Verdict: Ready for Monday and much better than 1.0, which has no gate at all. On all 162k recorded NIFTY rounds it raised zero false alarms and blocked every garbage-feed entry 1.0 took. It does have one measured false-negative hole (it re-opens on garbage that sits just under 45 pts) and two edge behaviours to fix soon.
Score (your area): 1.0 = 2/10, 2.0 = 7/10

Scope and data. `feed_health.py` (93 lines) is wired in at `orchestrator.py:128-135` (built), `:371-383` (update and edge events/sound), `:411-414` (blocks the interval re-anchor), `:676` and `:701-702` (entry gate `FEED_UNHEALTHY`), `:880-885` (blocks the post-exit and post-cooldown re-anchor) and `:1009` (`decision_tape.feed_ok`). In 1.0, `grep -rn "frozen|implausib|feed_sanity" /home/claude/vajra2/vajra` finds nothing, so 1.0 has no equivalent.
I replayed the gate class over the exact `(ts, spot, basis)` stream of every NIFTY row in all three `basis_tape.parquet` files. These are the same IMPLIED-parity basis values the 2.0 orchestrator feeds it. Rounds: 14 Oct 27,564 (13:26–15:29), 15 Oct 79,746 (09:41–15:29), 16 Oct 60,030 (11:11–15:25). Scripts are in `/home/claude/teardown/scratch/03/` (`sim.py`, `sweep.py`, `stale_reopen.py`). I also ran one full 2.0 `replay.py` on 14 Oct.
**No tape covers 09:15–09:41.** Everything I say about the open is from reading code, not from data.

## Findings (most severe first)

### [HIGH] Gate re-opens three times inside the 14 Oct garbage window. |basis| hovers at 29–45, just under `max_abs_basis_pts: 45` (feed_health.py:71, vajra_config.yaml:164) — reproduced
- Evidence. Sim with the shipped thresholds (`sim.py`), 14 Oct:
  ```
  15:15:45 SPOT_FROZEN ... 15:20:38 ok | 15:20:48 SPOT_IMPLAUSIBLE ... 15:21:19 ok
  15:21:35 SPOT_IMPLAUSIBLE ... 15:22:05 ok | 15:22:43 SPOT_IMPLAUSIBLE ... 15:27:19 ok
  ```
  That is 250 "ok" rounds (about 76 s) between 15:15:45 and 15:25, all of them before the 15:25 entry cutoff.
  The 2.0 replay `event_tape` confirms it: `FEED_HEALTH RECOVERED` at 15:20:38, 15:21:19 and 15:22:05.
  The data in those windows is not clean. The day's basis was −1 to −13 before the freeze. In the windows, spot drifts up while the option-implied future stays flat at about 22613–22618. Basis reads −17 → −29 → −38 → −43.
  The deviation from the prior-10-minute median basis is 19–33 pts (sampled: 15:20:50 dev 23.5, 15:21:51 dev 30.3, 15:22:32 dev 33.1).
  The gate's 30 s recovery is satisfied as soon as basis wanders under 45 for 30 s.
- Impact. On 14 Oct the replay was already in a PE trade (14:45:13) and the nearest level was 20–52 pts away, so nothing fired. On a flat book within 7 pts of a level, 2.0 would enter on a spot that is 30–45 pts wrong. It would also buy the "current ATM" (`orchestrator.py:732`) computed from that wrong spot.
- Fix (any one closes it on the tape; together they are robust):
  1. Add a **relative** basis rule. Trip when `|basis − ref| > 20` for ≥ 2 s, where `ref` is the median basis over the last 10 min of *OK* rounds, frozen while the gate is closed.
     Clean evidence: the longest run with dev > 20 is 0 s (14th), 1.26 s (15th) and 0 s (16th), so there are no false positives. It trips at 15:20:02, 15:20:11 and 15:20:09, holds through the 14th's windows, and is independent of days-to-expiry.
  2. Use `recovery_secs: 60` plus `max_abs_basis_pts: 40`. Sweep (`sweep.py`): false-positive rounds before 15:15 = [0,0,0], ok rounds 15:15:45–15:25 = [0, 85*, 0].
     *The 85 are 15 Oct 15:15:45–15:16:06: frozen-detection latency, not a re-open.
  3. Or add hysteresis: re-open only when |basis| < 0.6 × threshold or dev < 10.

### [MEDIUM] Gate can block a genuine fast breakout for 30 s, and the entry then fires late with no overshoot cap (feed_health.py:73, orchestrator.py:1072-1073) — reproduced (synthetic)
- Evidence. Synthetic scenarios on a clean 15 Oct 14:00–14:03 slice:
  ```
  A  +45 pt one-round move, spot AND implied future move together (basis unchanged): blocked 119 rounds 14:01:00.170-14:01:30.385 [SPOT_JUMP, RECOVERING]
  A2 same, option LTPs lag one round:  blocked 30.2 s [SPOT_IMPLAUSIBLE, RECOVERING]
  B  +30 in 3 rounds of 10:            blocked 0
  C  8 s feed gap, then legit +42:     blocked 30.1 s [SPOT_JUMP]
  ```
  The "one-round jump" is measured between gate *updates*. Rounds where NIFTY is missing or its basis fails do not update the gate (`orchestrator.py:340-372`), so after a reconnect gap the jump is really an N-second move (scenario C).
- Measured clean behaviour on the tapes:
  - Max one-round |jump| is 9.65 / 22.25 / 7.40 pts (the 22.25 was 15 Oct 14:43:54).
  - Max 60 s range is 24.7 / 34.3 / 21.2 pts.
  - The tapes contain no real 40-pt round, so no false positive was observed. The risk is on event days and expiry afternoons, which these calm tapes do not sample.
- When the gate reopens, `_approach_entry` still fires on a breach of any depth (`spot >= ul - prox`, no maximum). The result is a late entry, 30 s into the move, at an inflated premium, not a missed one.
- What separates garbage from a real move: every garbage jump on the tapes moved the implied future by ≤ 2.1 pts (14th −46.6/−0.9, −56.9/−0.5, +54.8/+1.9; 15th +198.9/+0.4 … ; 16th +103.0/0.0, +110.9/−1.0). Clean |Δspot − Δfuture| per round never exceeded 15.8.
- Fix:
  - Replace SPOT_JUMP with a **divergence** rule: `|Δspot − Δfuture| > 30`.
  - Give jump-class faults a short recovery: re-open as soon as basis returns within 10 of `ref` for 2 s, so options catching up clears scenario A2. Keep the long recovery only for FROZEN and IMPLAUSIBLE.
  - Optionally cap breach-entry overshoot.

### [MEDIUM] A re-anchor blocked by the gate is not redone when the gate re-opens, so entry fires on the pre-fault band (orchestrator.py:411-414, 880-885; freshness_engine.py:67-71) — reproduced
- Evidence (`stale_reopen.py`, real orchestrator, `_build_band` stubbed: band A then band B):
  - Band A anchored at 10:00 (upper 22960).
  - Garbage feed 10:19:50–10:20:30 blocks the 10:20:00 interval re-anchor, which goes to `mark_attempt`.
  - Clean from 10:20:31. Output:
  ```
  10:21:01 gate=FEED_UNHEALTHY held_upper=22960 position=False
  10:21:02 gate=None held_upper=22960 position=True
  band builds: 1
  ```
  The entry is taken on a 21-minute-old band; the re-anchor that was due would have moved the upper to 23010. The retry fires at the next 30 s `retry_secs` tick at the earliest.
  The same applies to post-exit and post-cooldown re-anchors blocked at `:880`. Those set `_levels_stale=True`, but `:450-455` clears it on the next round because the held band is "valid".
  It is still strictly better than 1.0, which re-anchored *on* the garbage.
- Fix: on `hs.ended`, run `_recompute_nifty_band(round_ts, …, trigger="feed_recovered")` before the entry gates. This is the same pattern 2.0 already uses for cooldown ends at `:665-672`. Alternatively, keep `FEED_UNHEALTHY` until a pending re-anchor has succeeded.

### [MEDIUM] Cold start: the gate has no history when the 09:20 "open" anchor (or a mid-session start's first anchor) is built (orchestrator.py:536-537, 371) — read in code / plausible
- `process_round` returns before 09:20, so the gate never sees 09:15–09:20. The first update has `_last_spot=None`: jump = 0 and unchanged = 0.
- The day's most important anchor (`"open"`, `freshness_engine.py:62`) is therefore computed on round 1. At that point frozen detection is impossible (it needs 30 s) and so is jump detection. Only |basis| > 45 can catch a stale prev-close spot; a gap open under 45 pts would not be caught.
- The operator's recorded starts were 09:41, 11:11 and 13:26, and each mid-session start anchors immediately in the same way.
- Fix:
  - Call `feed_health.update` for NIFTY rounds from 09:15 even though no calc or tape happens before 09:20. The 09:20 rule is unchanged.
  - If the gate has seen less than `frozen_secs` of data, defer the anchor (via `mark_attempt`) until it has.

### [LOW] Absolute 45-pt basis ceiling ignores days-to-expiry and ACTUAL mode (feed_health.py:71, expiry.py:121) — plausible
- Clean |basis| max is 19.15 / 18.75 / 29.75 (16 Oct 11:19:30, T-1 trading day). That leaves a 15-pt margin.
- From Wed 21 Oct the active weekly (27 Oct) is the monthly, so basis switches to the traded future. That future is less liquid and carries about 6 days of carry; I have no tape of ACTUAL mode.
- The relative rule in Finding 1 removes the dependence on DTE. Keep 45 as an outer guard.

### [LOW] The gate is only updated on NIFTY rounds whose basis computes (orchestrator.py:371) — read in code
- On `MISSING_ATM_QUOTE` rounds (14 Oct replay: 15:21:44, 15:22:24, 15:28:17) `_health` is stale and the jump spans the gap.
- No entry can fire on those rounds (`NO_LEVELS`), so the only effect is Finding 2's gap scenario. Fix it with a time-normalised jump or divergence rule.

### [LOW] Feed alarm rings on a known daily artefact (orchestrator.py:381, alerts.py cooldown 300 s) — read in code + sim
- Fault-start edges were 4 on the 14th (15:15:45, 15:20:48, 15:21:35, 15:22:43) and 1 each on the 15th and 16th. With the 300 s cooldown that is 2 rings on the 14th (15:15:45 and 15:20:48) and 1 on each other day.
- This is defensible, because the operator mirrors trades by hand and should know the spot is garbage. Fixing Finding 1 also removes the second ring. No change required.

### Fine (checked, no issue)
- **False positives in clean trading: none.** 0 blocked rounds before 15:15 on all three days, across 158,247 clean rounds. Every episode started 15:15:33–15:16:16. The CHANGELOG claim is verified.
- **Lunch lull / unchanged spot:** the longest clean unchanged run is 4.0 / 9.8 / 16.0 s (16 Oct 13:58:58). A synthetic 25 s flat spot is not blocked; 35 s is blocked for 35.5 s. 30 s is a sound threshold.
- **True positives:** gate state at each of 1.0's garbage entries was closed: 14 Oct 15:20:03 PE (IMPLAUSIBLE), 15 Oct 15:20:11 CE (IMPLAUSIBLE), 16 Oct 15:20:09 CE and 15:24:47 PE 23300 (192 pts ITM; IMPLAUSIBLE). 2.0 blocks all four. The 15th and 16th stay closed from 15:16:16 / 15:15:33 until the end of the tape.
- **Detection latency:** freeze onset → trip took about 30 s (14th 15:15:15 → 15:15:45; 16th 15:15:03 → 15:15:33). The interval re-anchor around 15:20 and the garbage jump at 15:20:02–11 both fall well after the trip.
- **Gate closed while a trade is open:** management continues (`:722`, `:1019-1035`). In the 14 Oct replay the PE opened at 14:45:13 rode through 4 fault episodes, `hold_while_in_trade` held the band, and it flattened at 15:25:00 @129.2, the same fill 1.0 got. Exits are priced from option quotes, which kept ticking.
- **Expiry day (Mon 19, IMPLIED from the expiring weekly):** basis converges to about 0, which makes the absolute check *safer*. I found no false-positive mechanism (read in code; no expiry tape exists). The residual risk is a fast expiry-afternoon move, which is Finding 2.
- `last_entry` is 15:25 in the yaml (`vajra_config.yaml:224`), so it complies with the rules.

### Threshold recommendations (with data)
| key | shipped | clean max seen | recommend | why |
|---|---|---|---|---|
| frozen_spot_secs | 30 | 16.0 s | **25** (keep ≥ 20) | 15 s false-trips 16 Oct 13:59:13 (242 rounds); 20 has 0 FP but a 1.25× margin; 25 detects 5 s earlier |
| max_abs_basis_pts | 45 | 29.75 | **40** + new relative rule (dev from 10-min OK-median > 20 for ≥ 2 s) | 40/60 s recovery closes the 14th re-opens with 0 FP; the relative rule is DTE-independent |
| max_jump_pts | 40 | 22.25 | replace with **\|Δspot−Δfuture\| > 30** (clean max 15.8; every garbage jump had \|ΔF\| ≤ 2.1) | stops blocking genuine breakouts |
| recovery_secs | 30 | — | **60** for FROZEN/IMPLAUSIBLE; about 2 s basis-consistent for JUMP | 30 s re-opened on the 14th's garbage three times |

## What 2.0 does better than 1.0 (evidence)
- 1.0 has no feed gate. It entered on the 15:20 garbage tick on every recorded day (order tapes: 15:20:03, 15:20:11, 15:20:09, plus 15:24:47 192 ITM). 2.0 blocks all of them with zero clean false positives.
- 2.0 never re-anchors levels on a bad feed (`:411`, `:880`). 1.0 did.
- Faults are edge-latched as `FEED_GAP`/`FEED_HEALTH`, `decision_tape.feed_ok` is recorded, the dashboard chip and `/api/health` show the state, and the alarm sound is rate-limited.

## What 2.0 does worse than 1.0 (evidence)
- A genuine ≥ 40-pt single-round (or post-gap) move now delays a breakout entry by 30 s (synthetic A/C). 1.0 had no such delay.
- Nothing else is worse.

## Tests / checks you ran
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_v2.py -k "feed or health"` → 3 passed.
- `sim.py`: gate class over all three basis tapes. Episodes listed above.
- `sweep.py`: 9 threshold sets, measuring FP rounds before 15:15, first trip, and ok rounds 15:15:45–15:25.
- Clean statistics per day: jump, unchanged runs, |basis|, dev from median, |ΔS−ΔF|, 5 s and 60 s ranges.
- Synthetic scenarios A, A2, B, C, D, D2.
- `stale_reopen.py`: entry on the stale band after the gate re-opens (real Orchestrator).
- One full replay: `python3 -B replay.py --tape /home/claude/vajra2/tapes/2026-10-14 --out /home/claude/teardown/scratch/03/rp` took 11m45s wall. Trades: 14:12:40 PE → SL −10,855; 14:45:13 PE → flatten 15:25 −4,768. Events as cited.
