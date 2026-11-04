# 12 — Non-expiry performance comparison, 1.0 vs 2.0, on the recorded days: teardown

Verdict: 2.0 is better than 1.0 off expiry and fit for Monday in this area, but its edge is unproven. Every 2.0 trade was taken on a sane feed. 4 of 1.0's 5 trades were taken on the broken 15:20 cash feed. 2.0's extra trades are real (no feed artefact), but they are a trend-following ~18–25-pt breakout, and they lost on 2 of the 3 days. The feed gate also leaks on 14-Oct (HIGH).
Score (your area): 1.0 = 3/10, 2.0 = 6/10

## Method (so the numbers can be trusted)
* Harness `/home/claude/teardown/scratch/12/harness.py`: it drives each engine's **real `Orchestrator` + `Executor`** with a null tape writer that keeps only orders, signals, NIFTY re-anchors and feed events. It streams rows from parquet in 1,000-row batches. Capital starts at ₹1,00,000 and compounds through the day: `lots = floor(capital/(ask×65))`, entry fills at the ask, exits at the bid. There is no cap and no loss stop. Each day resets to ₹1,00,000, as the engine does.
* **NIFTY-only tapes.** Off the NIFTY rows, nothing in either engine gates an entry. 1.0 `process_round` has `fire = entry and expiry_gate_ok and band and not position and not cooldowns`. 2.0 has an ordered gate list, `orchestrator.py:692-714`. The sisters only feed the advisory confluence. So I extracted the NIFTY rows of each day (`extract.py`: 27,564 / 79,746 / 60,030 rows) and replayed those. This is about 1/10 of the CPU of a full replay.
  * **Fidelity proof 1:** the 1.0 NIFTY-only replay reproduces the recorded live `order_tape` exactly on all three days. Every timestamp, strike, qty, fill and P&L matches, for example −2,180.75, +6,233.50, +3,022.50, +8,528.00 and −68.25.
  * **Fidelity proof 2:** I replayed 2.0 over the **full 24-instrument tape** for 10-15 up to 10:20 and compared it with the NIFTY-only window: `trades equal True levels equal True 148915.75 148915.75` (CPU 51.1 s vs 4.7 s).
* **Coverage caveat:** the recordings start at **13:26 (14-Oct), 09:41 (15-Oct) and 11:11 (16-Oct)**. Both engines' "open" anchor is the first recorded round, not 09:20. These are partial-day results.

## Results (all-in compounding, per day)

| Day | 1.0 trades / wins | 1.0 P&L | 1.0 trades on broken feed | 2.0 trades / wins | 2.0 P&L | 2.0 on broken feed |
|---|---|---|---|---|---|---|
| 14-Oct | 1 / 0 | −2,180.75 | **1 of 1** | 2 / 0 | **−15,622.75** | 0 |
| 15-Oct | 2 / 2 | +9,256.00 | **1 of 2** | 9 / 8 | **+68,724.50** | 0 |
| 16-Oct | 2 / 1 | +8,459.75 | **2 of 2** | 4 / 1 | **−19,071.00** | 0 |
| **Total** | **5 / 3** | **+15,535** | **4 of 5 (net +9,301.50)** | **15 / 9** | **+34,030.75** | **0** |

Counterfactuals:
* 1.0 on clean feed only: 1 trade, +6,233.50.
* 2.0 with `nonexpiry.band_method=legacy` (feed gate and current-ATM fix only): 14-Oct 0 trades, 15-Oct 1 trade +10,617.75, 16-Oct 0 trades.
  * Its 15-Oct trade is 1.0's 10:00 trade, but it buys the current-ATM 22850 CE at 134.40 instead of 1.0's 22750 CE at 196 (105 points in the money), and makes +11.05% instead of +6.99%.
  * The other 14 trades of 2.0 come from the MTM band.

2.0 aggregate:
* Gross wins +109,590 and gross losses −75,560, so the profit factor is 1.45.
* The average win (₹12.2k) is about the same size as the average loss (₹12.6k).
* All of the profit comes from one trending day (15-Oct). Daily returns were −15.6%, +68.7% and −19.1%.

### 1.0 trades on the 15:15–15:25 broken cash feed
On all three days the feed froze from about 15:15:20 to 15:20:00 (14-Oct: spot 22625.15 for 280 s). The spot then jumped while the option-implied future barely moved. Clean max |basis| before 15:14 was 19.2 / 18.8 / 29.8.

| Day | Entry | Spot (garbage) | Implied future | Basis | "Level" | Bought | Result |
|---|---|---|---|---|---|---|---|
| 14 | 15:20:03 | 22491.55 | 22607.2 | **+115.7** | lower 22505.05 "breached" by 13.5 | 22600 PE @132.25, 11 lots | flatten −2,180.75 |
| 15 | 15:20:11 | 23138.85 | 22934.6 | **−204.2** | upper 23056.4 "breached" by 82 | 22950 CE @106, 15 lots | flatten +3,022.50 |
| 16 | 15:20:09 | 23222.00 | 23105.0 | **−117.0** | upper 23184.45 "breached" by 38 | 23100 CE @91.15, 16 lots | trail +8,528.00 |
| 16 | 15:24:47 | 23227.20 | ≈23108 | **−119.3** | lower 23222.7 (re-anchored on garbage at 15:22:17) | 23300 PE @220.8, 7 lots | flatten −68.25 |

These were real option fills on real option quotes, but the triggers were fake. The net +9.3k is luck, and it is most of 1.0's off-expiry "profit". **2.0 took none of them:** its feed gate tripped SPOT_FROZEN at 15:15:45 / 15:16:16 / 15:15:33, before the first garbage tick.

### 2.0 entries (all clean feed: |basis| at entry ≤ 18.05)
* **15-Oct, all CE, all at upper levels, 5.4–7.0 pts away:**

  | Entry | Strike | Fill | P&L |
  |---|---|---|---|
  | 09:45:55 | 22750 | 138.65 | +7,186 |
  | 09:52:09 | 22800 | 126.65 | +7,267 |
  | 09:54:15 | 22850 | 120.35 | +16,835 |
  | 10:06:50 | 22900 | 123.45 | +17,628 |
  | 10:46:43 | 22950 | 119.65 | +4,137 |
  | 11:28:48 | 22950 | 132.95 | +10,884 |
  | 12:35:38 | 23000 | 119.20 | +15,766 |
  | 12:54:30 | 23000 | 136.00 | +11,895 |
  | 13:36:37 | 23050 | 125.70 | −22,874 (SL, 23 lots) |

  There were no entries after the 13:51 cooldown end.
* **16-Oct:**

  | Entry | Strike | Fill | P&L |
  |---|---|---|---|
  | 11:12:05 | 23100 CE | 91.75 | +17,992 |
  | 12:18:48 | 23150 CE | 88.75 | SL −13,520 |
  | 12:37:56 | 23150 CE | 88.90 | SL −13,046 |
  | 13:20:11 | 23150 CE | 81.70 | SL −10,498 |

  The last three came off upper levels 23140 / 23145 / 23140.
* **14-Oct:**

  | Entry | Strike | Fill | P&L |
  |---|---|---|---|
  | 14:12:40 | 22600 PE | 143.50 | SL −10,855 |
  | 14:45:13 | 22600 PE | 137.35 | flatten −4,768 |

  The levels were lower 22605 / 22610.

**Are the extra trades plausible or artefacts?** They are plausible. The feed was sane at every entry, the fills are the recorded ask and bid, sizing follows the operator rule, and the full-universe replay gives the same trades. They are not feed artefacts. They are the predictable output of the MTM band (see the MEDIUM finding below), which turns the engine into a short-range momentum breakout.

## Findings (most severe first)

### [HIGH] Feed-sanity gate re-opens inside the broken tail and re-anchors levels on garbage (feed_health.py:73-91, orchestrator.py:411, vajra_config.yaml `feed_sanity`) — reproduced
- **Scenario / evidence.** On 14-Oct the garbage spot's basis hovered at −36 to −46, just inside the 45-pt trip. With a fixed 30 s recovery and no hysteresis, the gate re-opened three times:
  ```
  15:15:45 FEED_GAP SPOT_FROZEN | 15:20:38 RECOVERED | 15:20:48 SPOT_IMPLAUSIBLE -50.3 | 15:21:19 RECOVERED
  | 15:21:35 SPOT_IMPLAUSIBLE -45.75 | 15:22:05 RECOVERED | 15:22:43 SPOT_IMPLAUSIBLE | 15:27:19 RECOVERED
  ```
  - Each re-open allows entries and re-anchors.
  - A flat-start window replay (`harness.py ... nifty/2026-10-14 --start 15:00`) shows `15:21:30 interval 22625.0 22765.0`, a NIFTY re-anchor computed from spot 22651 while the real implied future was about 22615.
  - It was a near miss for a PE entry: the garbage spot reached 22646 against an entry trigger of ≤22632.
  - In the full-day run, 2.0 was already in a trade, so it did no harm on this tape.
- **Fix.**
  - Add hysteresis: trip at |basis| > 45 but recover only after |basis| < 20 **and** a changing spot for ≥ 60 s.
  - Alternatively, latch the gate for the rest of the session after a SPOT_FROZEN episode at or after 15:10. This is the same fault signature on all three tapes.
  - Add a test from the 14-Oct 15:20–15:23 rows.

### [MEDIUM] The 2.0 non-expiry "edge" is a trend-following ~18–25-pt breakout; it repeats losers at the same level (trigger_engine.py:166-176, orchestrator.py:661-668, 803) — reproduced
- **Mechanism.** `up = min(legacy, max((1.2·VWAP−LTP)/Δ, 25))`. In a trend, the with-trend ITM option trades above 1.2×VWAP, so that side snaps to the **25-pt floor**. The counter-trend side stays at the legacy ~100–130 pts. On 15-Oct every band was about 25 pts above spot and about 130 below (e.g. `10:02:04 lower 22710 upper 22885 / legacy 22686.7 22931.7`).
- **What it causes.** Every post-exit or post-cooldown re-anchor resets the trigger about 18 pts beyond the current spot, so the engine buys with-trend after every 18-pt push. That pays on a trend day (15-Oct, 8/9 winners) and bleeds in chop:
  - On 16-Oct it bought 23150 CE three times in 62 minutes at the same 23140–23145 level, and each was stopped at −12%. The post-stop cooldown re-anchors give the same level again (`12:33:36 post_cooldown 23030/23145`, `13:07:15 post_cooldown 23030/23140`).
  - On 14-Oct it lost on both trades.
  - With all-in compounding, one −12% stop on 23 lots (−22.9k) wiped out about 3 average wins.
- **Fix (no locked rule touched).** Evaluate `mtm_floor_pts` (25 → 40/50) and `mtm_pad` on more tapes before trusting the +34k. Report P&L per regime of day. Three partial days and n=15 is not evidence of an edge.

### [MEDIUM] Recorded days are partial; the results cannot be read as "a 2.0 day" (tape coverage) — reproduced
- **Evidence.** The NIFTY rows run 13:26:06–15:29:12, 09:41:55–15:29:59 and 11:11:03–15:25:52. Mornings are missing on two days, and the open anchor is the first recorded round, not 09:20.
- **Fix.** Before judging, record full sessions on 19-Oct and later (expiry day uses the legacy band anyway) and re-run this harness.

### [LOW] 1.0 replay cannot run even a NIFTY-only day; 2.0 fixed it (1.0 feed.py:95-97 `rows = self._read_rows()` + sort) — reproduced
- **Evidence.** Via 1.0's `rounds()`, the 80k-row NIFTY tape went over 1.6 GB RSS in 30 s and was OOM-killed (`Memory cgroup out of memory: Killed process ... python3 anon-rss:1168312kB`).
- 2.0's `ReplaySource._iter_rows` (feed.py:81-96) streams. My harness streams for both engines.

### [LOW] Stale comment says entries stop at 15:10 (orchestrator.py:99-100) — read in code
- The code uses `last_entry: "15:25"`, which matches the operator rule. Fix the comment.
- CHANGELOG "Result on the recorded days" is an empty placeholder, so there is no performance claim to verify.

### [INFO] A trail exit can book below the +7% floor
- 15-Oct 10:46 trade: the floor was 128.03 and the exit filled at the bid of 123.00 (+2.8%). The trigger is on the LTP and the fill is at the bid. Both engines behave the same way. This is realistic paper behaviour; the operator should expect it.

## What 2.0 does better than 1.0 (evidence)
* **No trades on the broken feed.** 0 of 15, against 1.0's 4 of 5. The gate tripped on every day before the first garbage tick, and there were no false trips during clean sessions (one FEED_GAP per day on 15 and 16-Oct, none before 15:15 on any day).
* **It trades non-expiry days on a real feed,** which is what the operator asked for: 15 clean-feed trades against 1 for 1.0.
* **Current-ATM strike.** For the same 15-Oct 10:00 signal, the legacy 2.0 bought 22850 CE at 134.40 (+10,618), while 1.0 bought 22750 CE at 196 (+6,233).
* Holds the band while in trade, re-anchors after cooldown before entering, and streams replays (verified).

## What 2.0 does worse than 1.0 (evidence)
* **Larger drawdown days.** 2.0 lost −15.6% and −19.1% on 14 and 16-Oct, against 1.0's −2.2% and +8.5% (and 1.0's 16-Oct gain was pure feed luck).
* **Repeated same-level stop-outs:** three SLs at 23140–23145 on 16-Oct.
* The gate leaks on a slow-garbage feed (HIGH above); 1.0 has no gate at all, so 2.0 is still strictly safer.

## Tests / checks you ran
* `extract.py <tape> nifty/<day>`: NIFTY-only streams (10 s / 28 s / 26 s).
* `harness.py /home/claude/vajra2 nifty/2026-10-{14,15,16}`: 1.0 replay matches the recorded `order_tape` exactly.
* `harness.py /home/claude/teardown/vajra2 nifty/2026-10-{14,15,16}`: the 2.0 results above (`out/v2_*.json`).
* `harness.py ... full/2026-10-15 --end 10:20` vs NIFTY-only: identical trades and levels. This was my only full-universe replay, a window.
* `--set nonexpiry.band_method=legacy` on all three days: isolates the MTM band's contribution.
* `--start 15:00` on 14-Oct: the gate leak and the 15:21:30 garbage re-anchor.
* Late-session spot/future/basis dump at 10 s resolution for each day: the frozen-then-jump signature, and clean max |basis| of 19.2 / 18.8 / 29.8. Note that 16-Oct's clean 29.8 leaves only 15 pts of margin to the 45 trip.
* All outputs are in `/home/claude/teardown/scratch/12/out/`.
