# 16 — Synthetic full-session stress testing: teardown
Verdict: Ready for Monday as far as stress testing can show. 23 full 09:15–15:30 sessions ran through 2.0 with zero crashes and zero breaches of the sizing, cooldown, exit or 15:25 rules. On gross garbage and frozen spot, 2.0 made **no** trades; 1.0 made 6 garbage-driven trades. Small plausible bad prints (under 40 pts) still get through. 2.0 is **better** than 1.0.
Score (your area): 1.0 = 5/10, 2.0 = 8/10

## Method (my own generator, not VAJRA's `synthetic.py`)
* `/home/claude/teardown/scratch/16/synthgen.py`: Black-76 chains on a forward F = S·e^{rT}, so the implied basis is 4–18 pts. Variance time is (rest of today + trading days to expiry + 0.3 × non-trading days)/252. At 1–3 DTE the 1-ITM option carries 70–150 pts of time value; on expiry day it decays toward zero.
  * VWAP per absolute strike is accumulated from 09:15, with volume weighted toward ATM. Bid/ask is ±0.15%. Expiry is Mon 19-Oct (DTE0 = 10-19, DTE1 = 10-16, DTE2 = 10-15, DTE3 = 10-14). Stocks and sisters use the 27-Oct monthly expiry.
  * 24 instruments, 4 rounds/s, 90,000 rounds per day. The engine is fed from 09:19:30.
  * The previous-close seed comes from `prev_close_snapshot()`.
* Each scenario has a seeded true NIFTY path plus a *display overlay*. Options are always priced off the true path, and the chain window is centred on the displayed ATM, exactly like `LiveBreezeSource._build_snapshot`.
* Scenarios: trend_up/down (±1.2%), chop (±45 pt cycles), v_reversal, gap_open (+260 vs prev close), slow_grind (+0.4%), flash_spike (+130/−150/+90 in 20 s, revert in 2 min), frozen_spot and garbage_spot (details below), and subtle_garbage (±35 pt one-tick prints every 7 min, plus a 20 s +30 offset every hour).
  * frozen_spot: freezes of 5 min, 3 min, 25 s and 15:15–15:20, all on a trending market.
  * garbage_spot: +220 and −180 single ticks, NaN, 0, inf, a negative print, a ÷10 decimal shift, a 60 s +185 offset, 10 s of ±150 flicker, and the recorded 15:15 freeze followed by a +240 tail.
* `/home/claude/teardown/scratch/16/run_session.py` runs the real `Orchestrator.run()` (both versions) behind an **independent rule auditor** that checks every round:
  * all-in sizing: lots = floor((1e5 + realised P&L)/(fill·65));
  * a reference exit model (−12% hard stop before arming, arm at +10%, floor = max(+7%, 0.94·peak), 15:25 flatten), compared with every engine exit;
  * 660 s / 15 s cooldowns;
  * entry window 09:21–15:25 and entry within 7 pts of (or through) the held level;
  * whether the strike is the current ATM;
  * entries on a garbage tick, within 30 s after one, or on a spot frozen for at least 30 s;
  * a position still open after 15:25.
* Tapes went to an in-memory stub with sha256 hashes of orders, decisions and levels. One run used the real `TapeWriter` instead.
* The box ran at load 17–34 on 2 CPUs for most of the session. So the bulk matrix ran **NIFTY-only**, which is valid because confluence never vetoes an entry (see the "not run" list below). Each session took about 40–50 CPU-s.

## Scenario table (NIFTY-only, full 09:15–15:30 sessions; P&L in ₹ on ₹1L all-in compounding)
| scenario | DTE | 2.0 trades / W / P&L | 2.0 rule violations | 2.0 bad-feed entries | 1.0 trades / P&L | 1.0 violations |
|---|---|---|---|---|---|---|
| trend_up | 0 | 25 / 12 / −48,851 | 0 | 0 | not run | – |
| trend_up | 1 | 17 / 11 / +57,076 | 0 | 0 | 10 / +32,796 | 2 stale-band entries, 4 strikes not ATM |
| trend_up | 3 | 12 / 6 / −20,634 | 0 | 0 | not run | – |
| trend_down | 0 | 16 / 10 / +18,447 | 0 | 0 | not run | – |
| chop | 0 | 30 / 22 / +636,890 | 0 | 0 | not run | – |
| chop | 1 | 20 / 11 / −29,266 | 0 | 0 | not run | – |
| chop | 3 | 14 / 0 / −79,147 (all SL_HIT) | 0 | 0 | not run | – |
| v_reversal | 0 | 45 / 31 / +251,043 | 0 | 0 | not run | – |
| gap_open | 0 | 29 / 16 / −36,692 | 0 | 0 | 34 / +20,676 | 3 stale-band entries, 12 strikes not ATM |
| slow_grind | 0 | 7 / 3 / −26,205 | 0 | 0 | not run | – |
| flash_spike | 0 | 21 / 11 / +218,384 | 0 | 0 | 23 / +218,481 | 2 stale-band entries, 14 strikes not ATM |
| frozen_spot | 0 | 22 / 14 / −1,758 | 0 | 0 | 20 / +31,610 | 9 strikes not ATM |
| frozen_spot | 1 | 10 / 2 / −56,592 | 0 | 0 | not run | – |
| garbage_spot | 0 | 14 / 8 / −19,581 | 0 | **0** | 20 / −28,174 | **6 garbage entries**, 3 stale-band entries, 12 strikes not ATM |
| garbage_spot | 1 | 12 / 6 / −13,835 | 0 | 0 | not run | – |
| subtle_garbage | 0 | 26 / 16 / +27,658 | 0 | **7** | 27 / +39,566 | 7 garbage entries |

* No run crashed in either version. There were no SESSION_ERROR or INSTRUMENT_ERROR events, no MISSED_EXIT, EXIT_MISMATCH, SIZING, cooldown, entry-window or OPEN_AFTER_1525 violations, and no rejects.
* "STRIKE_ITM_FALLBACK" in 2.0 is the expiry-day rule working as designed: ATM premium below ₹30, so it steps to the 1-ITM strike, mostly after 14:45.
* Trade counts look sensible: 7 (slow grind) to 45 (V-reversal) per session, peaking at 13 entries per hour. The busiest periods are 15-s re-entries on a level that is already held as breached (allowed by the "or a break through it" rule; up to 24 per day in v_reversal).
* NIFTY re-anchor gaps were 1200 s whenever no exit or cooldown intervened. A 1260–1290 s gap appears only where the feed gate deferred a re-anchor.
* **Determinism:** I re-ran garbage_spot DTE0 on 2.0. The order, decision and level hashes were identical (764ad1ee…, d105b3ca…, 25e57c92…), as was the P&L (−19,581.25).
* **Real TapeWriter:** garbage_spot DTE0 to 11:12, covering the NaN tick, ran with no errors. All 6 parquet tapes read back, with 26,879 tick rows matching the jsonl sidecar row count. The jsonl sidecar is about 10× the parquet size (95 MB versus 9.8 MB for 2 h of NIFTY alone).

## Findings (most severe first)
### [HIGH] Plausible bad prints under 40 pts open all-in trades; the feed gate never fires (feed_health.py `update`, thresholds in vajra_config.yaml `feed_sanity`) — *reproduced*
- In subtle_garbage DTE0, **7 of 26** 2.0 entries were taken on the *exact round* of a one-tick ±35/+30 bad print. The true spot was 17–42 pts from the level, outside the 7-pt rule. Examples:
  - `10:49:00 CE 23100 disp 23092.35 true 23057.35 true_dist 41.8`
  - `11:03:00 … true_dist 34.45 -> SL_HIT −13,601`
  - `11:24:00 PE … true_dist 39.0 -> SL_HIT −11,989`
- The run logged zero `FEED_GAP stage=feed_sanity` events. A direct probe of `FeedHealth` confirms the gap: a +35 or +39 one-tick print stays `ok` throughout, while +42 trips `SPOT_JUMP`.
- 1.0 takes the same 7 trades, so this is not a regression. But it is the remaining hole in 2.0's "no trade on garbage data" claim.
- Fix: check the *deviation* of basis from its own short EWMA or median (for example, more than 12 pts away from a basis that normally drifts 0–2 pts per second), not the absolute 45 pts. Or require an approach to persist for 2 rounds (0.5 s) before firing.

### [MEDIUM] A held position is blind while the printed spot is far off (feed.py:398-417 `_build_snapshot`; orchestrator.py:771-780) — *reproduced (blinding), no loss in this instance*
- In blind_garbage, a +400 print was held for 120 s at 11:39 with a CE 23150 position open. The chain window followed the bad ATM (23500 ±6), so the position's strike dropped out of the snapshot. 2.0 logged `FEED_GAP stage=manage_quote` and checked neither stop nor trail for 120 s.
- No exit happened to be due in that window (true premium 58.3–65.3, stop 53.8), so no loss resulted here. With a −12% move inside the window, the stop would be missed.
- Fix: always include the open position's strike in `_build_snapshot`. The ticks are already subscribed in `raw`.

### [LOW] Approach cue and entry cue ring in the same round (orchestrator.py:679-683 and 750) — *reproduced*
- 60–100% of approach sounds rang in the same 250 ms round as the entry sound. Examples: chop 28/36, gap_open 20/34, v_reversal 24/32, trend_up DTE1 14/24.
- Each session produced 45–122 sounds, so a typical trade is approach + entry + exit = 3 dings. That conflicts with "no unnecessary alert sounds".
- Fix: ring `approach` only when the entry is gated (cooldown or feed), or suppress it when `fire` is true.

### [LOW] Realised exits overshoot the stop and floor levels — *reproduced, expected*
- On expiry-day afternoons the bid gaps through the trigger: TRAIL_SL fills as low as +4.1% (15:24:07 in frozen_spot DTE0) and SL_HIT fills as deep as −13.5%.
- The rule logic itself is right; the auditor saw 0 mismatches. Paper fills are taken at the bid, so this is fill slippage, not a code defect. Worth telling the operator.

### [INFO] chop at DTE3: 14 trades, 14 SL_HIT, −79%
- Every MTM-band touch in this mean-reverting market reversed. This is the rules working, not a bug, but it shows the non-expiry band can bleed badly in chop.

## What 2.0 does better than 1.0 (evidence)
* **Gross garbage and frozen spot:** 2.0 made 0 bad-feed entries. 1.0 made 6 in garbage_spot DTE0, for example:
  - `10:05 CE on disp 23309 vs true 23089`
  - `15:20 on the +240 tail`
  - `15:21:30 PE 23250` — 200 pts ITM, on a band re-anchored on garbage.
  - 2.0 raised `SPOT_IMPLAUSIBLE`/`SPOT_FROZEN` on every episode, played the feed alarm 5 times, and logged 2,328 rounds of FEED_UNHEALTHY.
* **Fresh band after a cooldown:** 1.0 entered on the stale pre-cooldown band 2–3 times per session. The entry was measured 17–29 pts from the freshly re-anchored level (for example `12:40:35` dist 29.3). 2.0 re-anchors before evaluating the entry (orchestrator.py:656-668) and had 0 such entries.
* **Strike choice:** 2.0 buys the current ATM. 1.0 bought the anchor-time ATM, 50–250 pts away from the current ATM, 9–14 times per session.
* **Expiry-day levels** were identical in 1.0 and 2.0 at every re-anchor (garbage DTE0 level lists match). The locked formula is untouched.

## What 2.0 does worse than 1.0 (evidence)
* Nothing material found. Both versions share the sub-40-pt hole. On the same paths, 2.0's P&L differs from 1.0's only through strike choice and gating (flash_spike 218,384 vs 218,481).

## Not run (box overloaded; the coordinator asked me to stop)
* The 1.0 matrix for trend_up/down, chop, v_reversal and slow_grind at DTE0, and most 1.0 runs at DTE1–3.
* 2.0 at DTE2 entirely; 2.0 at DTE3 except trend_up and chop.
* Full 24-instrument sessions: started, but none completed, so the NIFTY-only versus full-universe equivalence was not verified empirically.
  - An earlier 24-instrument partial run (09:19:30–09:30) measured about 4 ms per round, well inside the 250 ms budget.
* The determinism re-run was done for one scenario only.

## Tests / checks you ran
* `python3 -B run_session.py --root <2.0|1.0> --scn <s> --dte <d> --universe nifty --out res/...`: 23 runs on 2.0 and 7 on 1.0, plus the determinism re-run, `--tape real --until 11:12` and blind_garbage `--until 12:10`.
* `FeedHealth` threshold probe (+35/+39/+42 one-tick prints).
* Ground-truth premium replay of the blind window (`synthgen.black76` on the true forward).
* Scripts and results: `/home/claude/teardown/scratch/16/{synthgen.py,run_session.py,summarize.py,res/*.json}`.
