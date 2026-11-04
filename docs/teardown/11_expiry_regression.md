# 11 — Expiry-day regression teardown: teardown
Verdict: **Ready for Monday.** On a forced expiry day (is_expiry_day=True), 2.0 trades exactly like 1.0 except for the four deliberate safety changes. With those changes switched off, 2.0 reproduces 1.0's orders, fills, P&L, decision words and NIFTY levels on every replayed round. 2.0 is **better** than 1.0 here. One HIGH leak remains in the feed-sanity gate.
Score (your area): 1.0 = 6/10, 2.0 = 8/10

## Method (what was actually run)
* **Driver** `/home/claude/teardown/scratch/11/drive.py`. Each codebase runs in its own process (`sys.path` = that root, its own `vajra_config.yaml`, its own `feed._snapshot_from_row`, `TapeWriter` and `Orchestrator`). Rounds are streamed from the recorded `tick_tape.parquet`, row group by row group, within a time window. Both engines get the same hand-built resolution: `ExpiryResolution(session 2026-10-19, expiry 2026-10-19, IMPLIED, is_expiry_day=True, is_monthly=False, shifted_from_holiday=True, near_month 2026-10-27)`.
  * This is byte-for-byte what 2.0's `build_calendar(cfg).resolve(date(2026,10,19))` returns (checked).
  * 2.0 resolved `regime = EXPIRY, legacy band, capital_fraction 1.0, no cap, no loss stop`.
  * To save disk and CPU, tick/basis tapes and non-NIFTY level rows are not written. They are outputs only, not engine inputs.
* **Comparators**
  * `cmp.py`: orders, NIFTY anchors, per-round NIFTY levels, decision/reason pairs, event counts.
  * `strict.py`: field-by-field equality.
* **Windows** (the tapes begin 13:26 / 09:41 / 11:11):

| win | tape | window | extra |
|---|---|---|---|
| A | 10-15 | 09:41–10:40 | (1.0's live 10:00 trade) |
| B | 10-15 | 15:00–15:30 | close + feed freeze |
| C | 10-16 | 15:00–15:26 | close + feed freeze |
| D | 10-14 | 15:00–15:30 | close + garbage feed |
| S | 10-15 | 11:00–12:30 | `entry.proximity_pts=40` stress (forces an SL_HIT and the 11-min cooldown) |

* **Synthetic expiry** (`vajra.synthetic.generate_rounds`, session 2026-10-19): 150 rounds from 13:00, 1200 rounds from 09:21, and 1400 rounds from 09:30, all at 15 s.
* **Neutralised 2.0** (the regression proof): 2.0 re-run on A, C and D with:
  * `feed_sanity.enabled=off`
  * `freshness.hold_while_in_trade=off`
  * `executor.enter` wrapped to drop `atm_strike`, which restores the anchor-ATM strike

## Round-by-round results (forced expiry)

| win | 1.0 trades → P&L | 2.0 trades → P&L | every difference explained by |
|---|---|---|---|
| A | 10:00:14 BUY 22750 CE ×455 @196.0 → 10:08:19 TRAIL +6,233 | 10:00:14 BUY **22850** CE ×715 @134.4 → 10:08:15 TRAIL **+10,618** | **current-ATM strike** (the band's anchor ATM was 22750, spot ≈22855). In-trade 10:01:56 interval re-anchor skipped = **hold-while-in-trade**. Post-exit / post-cooldown / interval anchors then shift 4 s (cascade of the earlier exit). |
| B | 15:20:11 BUY 22950 CE ×910 → flatten +2,821 | none | **feed gate**: SPOT_FROZEN from 15:16:16 until the close. 1.0 re-anchored at 15:20:00 on the frozen spot and entered 11 s later. |
| C | 15:20:09 BUY 23100 CE → TRAIL +8,528; 15:24:47 BUY 23300 PE → −68 | none | **feed gate** (SPOT_FROZEN 15:15:33 →). 1.0's 15:22:17 post-exit band used basis −182.7 (garbage). |
| D | 15:20:02 BUY 22650 PE ×585 → flatten −351 | none | **feed gate** (FROZEN 15:15:45, then IMPLAUSIBLE) — but see the HIGH finding: the gate re-opened 3 times. |
| S | 11:38:25 BUY 22900 CE ×585 @170.5 → 12:07:54 SL −11,466 | 11:38:25 BUY **22950** CE ×650 @140.25 → 12:07:37 SL −10,790 | **current-ATM**. The 11:40 and 12:00 interval re-anchors were skipped (**hold-while-in-trade**). After that, everything cascades 17 s earlier, including post_cooldown 12:18:37 vs 12:18:54. 2,588 rounds are labelled POST_STOP_COOLDOWN, where 1.0 labelled them NO_APPROACH (label only). |
| syn150 | 4 trades, end 880,855 | 4 trades, end 887,708 | First entry is 30 s later because the **feed gate** tripped SPOT_FROZEN on the 15-s synthetic cadence (artifact). Lots differ (2210/5525 vs 2210/5590). |
| syn1200 | 3 trades, end 233,487 | 3 trades, end 307,812 | **current-ATM** (24100 CE @80 vs 24150 CE @53). The synthetic spot goes flat after ~10:00, so the feed gate stays FROZEN and 2.0 skips all 13 interval re-anchors from 10:17 to 14:17 (artifact). |
| syn1400 | entries 09:39:00 / 09:44:15 / 09:59:30 | entries 09:39:00 / 09:44:15 / 09:59:15 | Same entry times; **current-ATM** strikes; exits ±1 round. |

**Neutralised 2.0 vs 1.0 (`strict.py`):**
```
== A  orders identical: True 2 | NIFTY level rows identical: True 13742 13742 | decision word identical: True
== D  orders identical: True 2 | NIFTY level rows: 6641 vs 6640 (only 1.0's 15:25:00 post-flatten re-anchor) | decision words identical
== C  orders identical: True 4 | NIFTY level rows: 6101 vs 6100 (same single post-flatten row) | decision words identical
end capital  A 106,233.50 = 106,233.50   D 99,649.00 = 99,649.00   C 108,459.75 = 108,459.75
reason-code-only diffs: NO_APPROACH->POST_PROFIT_COOLDOWN (59), NO_APPROACH->AFTER_LAST_ENTRY (205/899)
raw_bias not identical (sister/stock bands; advisory only, see LOW-3)
```

**Conclusion.** The expiry-day NIFTY band call, the 7-pt approach entry, the no-veto rule, the −12% / +10% / 7% exit ladder, the 15:25 flatten, all-in sizing and the 11-min / 15-s cooldowns are unchanged. The only trading differences come from:
1. the **current-ATM strike**
2. the **feed-sanity gate**
3. **hold-while-in-trade**
4. **no re-anchor after the 15:25 flatten** (cosmetic)

The **cooldown-order fix** did not change any outcome in these windows (orders matched with it still active). It can only matter on the round a cooldown ends while spot is within 7 pts of the old band but not the new one (`orchestrator.py:656-668`, read in code).

## Findings (most severe first)

### [HIGH] Feed-sanity gate re-opens inside the 15:20 garbage tail and re-anchors NIFTY on it (`feed_health.py:76-95`, `orchestrator.py:406-418`) — reproduced
- **Scenario / evidence.** Window D (10-14, forced expiry). The FEED_GAP / FEED_HEALTH events in `out/D_v2/2026-10-14/event_tape.jsonl`:
  ```
  15:15:45 FEED_GAP SPOT_FROZEN | 15:20:38 RECOVERED | 15:20:48 SPOT_IMPLAUSIBLE basis -50.3
  15:21:19 RECOVERED | 15:21:35 SPOT_IMPLAUSIBLE -45.75 | 15:22:05 RECOVERED | 15:22:43 SPOT_IMPLAUSIBLE -45.75 | 15:27:19 RECOVERED
  ```
  - `feed_ok=True` on 247 rounds between 15:20:38 and 15:22:43. These are open entry rounds on the broken feed.
  - At 15:21:30, 2.0 **re-anchored NIFTY** (`interval`, basis −35.85 vs ≈ −11 when clean; levels 22820.85 / 22565.85 against 1.0's 15:20 anchor of 22796.25 / 22541.25).
  - There was no entry only because the nearest level was ≥ 82 pts away. The same Breeze fault hit 3 of 3 recorded days. On Monday's expiry close, a garbage spot that lands within 7 pts of a level (|basis| 30–45 does not trip) gets an all-in entry at 15:2x.
  - The changelog says "while tripped … no new entries and no level re-anchors". That holds only in between the gaps.
- **Fix.** Add hysteresis to the gate:
  - After any SPOT_FROZEN of 30 s or more, or a second fault in a session, at or after 15:10, latch the gate closed until the flatten.
  - Otherwise require `recovery_secs ≥ 120` **and** |basis| < 25 (≈ 0.5 × max) before re-opening.
  - Add a test that replays 10-14 15:15–15:25 and asserts no `feed_ok=True` rounds after the first trip.

### [MEDIUM] SPOT_JUMP can false-trip on a genuine expiry-day gamma move (`feed_health.py:72-82`) — plausible
- **Scenario / evidence.**
  - The jump test is `|Δspot| > 40` between consecutive rounds, with **no cross-check against the option-implied future**. The module docstring itself defines garbage as "spot jumped … while the option-implied future moved < 10 pts".
  - Recorded clean maxima (my `feedstats.py` on the basis tapes, before 15:14):
    ```
    10-14 max jump 9.65, max|basis| 19.2, max unchanged 4.0 s
    10-15 max jump 22.25, max|basis| 18.8, max unchanged 9.8 s
    10-16 max jump 7.4, max|basis| 29.8, max unchanged 16.0 s
    ```
  - These are all **non-expiry** days. On an expiry day, a 40-pt one-second burst (news, or a breach "explosion") is exactly the move the engine exists to trade.
  - A trip blocks entries and re-anchors for at least 30 s of recovery. With levels 7 pts away, the approach is missed.
  - No expiry-day tape exists to measure this.
- **Fix.** Trip SPOT_JUMP only when `|Δspot − Δimplied_future| > 25`, i.e. the basis jumped. A real move carries the options with it. Keep `max_abs_basis` as the main garbage detector.

### [LOW] Synthetic expiry session now runs mostly in FEED_UNHEALTHY (`feed_health.py:69`, `synthetic.py:142`) — reproduced
- **Evidence.**
  - syn1200: 1,062 / 1,200 rounds are `FEED_UNHEALTHY`, and 2.0 skips all 13 interval re-anchors that 1.0 makes.
  - syn150: the first entry is delayed 30 s, and the feed-alarm sound and events fire.
  - Cause: the synthetic runs at a 15-s cadence with a scripted flat spot, which trips `frozen_spot_secs=30`.
  - The demo, the dashboard and the synthetic tests no longer exercise expiry-day re-anchoring.
- **Fix.** Set `feed_sanity.enabled=off` (or `frozen_spot_secs` ≥ 10 × dt) when the source is synthetic.

### [LOW] hold_while_in_trade deviates from the literal "NIFTY re-anchors every 20 min" rule (`orchestrator.py:414-416`) — reproduced
- **Evidence.**
  - Window A: 1.0 re-anchored at 10:01:56 during the trade; 2.0 did not.
  - Window S: 1.0 re-anchored at 11:40 and 12:00 during the trade; 2.0 did not.
  - There was no trading effect: exits are premium-based, and the post-exit recompute replaces the band. With the setting off (neutralised runs), the levels matched 1.0 exactly.
  - Side effect 1: the cockpit shows the entry band during a trade, not the current 20-min band.
  - Side effect 2: if the post-exit recompute fails (STALE_INPUTS / feed gap), the fallback band is older than 1.0's.
- **Fix.** Confirm with the operator. If the 20-min rule is literal for expiry, default `hold_while_in_trade: off` on the EXPIRY regime.

### [LOW] Sister/stock levels and raw_bias are not identical to 1.0 on expiry day (`orchestrator.py:271-273`, `vajra_config.yaml:34-55`) — reproduced
- **Evidence.**
  - `raw_bias identical: False` in all three neutralised runs, while orders, levels and decision words were all identical.
  - Causes: stock bands now round to 0.1 × step instead of 5 pts, and stock steps come from a fixed table.
    - BANKNIFTY and FINNIFTY are unaffected: `min(5, 0.1×step) = 5`.
  - The confluence is advisory only: `conviction` is unused in `Executor.enter`, and `dr` gates nothing. So no NIFTY trade changes.
  - Replay caveat: the recorded chains were subscribed on 1.0's doubled grid (AUBANK/SBILIFE strikes every 40, BAJFINANCE every 20). In replay, 2.0's table produces ATMs that are missing from the chain, which shows up as `MISSING_ATM_QUOTE` for AUBANK, SBILIFE, AXISBANK, BANKBARODA and BAJFINANCE (11 events in C). This is a replay artifact; live 2.0 subscribes on its own grid.
- **Fix.** None needed for trading. Document that expiry-day cross-instrument readings are not 1.0-comparable.

### [LOW] decision_tape reason codes changed meaning (`orchestrator.py:692-713`, `763-767`) — reproduced
- **Evidence.**
  - 2.0 writes the first closed gate on every round: POST_PROFIT_COOLDOWN / POST_STOP_COOLDOWN even with no approach, AFTER_LAST_ENTRY from 15:25, FEED_UNHEALTHY.
  - 1.0 wrote NO_APPROACH on those rounds.
  - Decision words are identical. Any tape diff tooling, or anyone comparing against 1.0 tapes, will see thousands of "changes" (2,588 in S, 899 in D).
- **Fix.** Keep it, but note it in CHANGELOG under Tapes.

### [LOW] Stale comments contradict the locked rules (`orchestrator.py:99-100`, `execution.py:136-138`) — read in code
- `orchestrator.py:99-100` says "NEW entries stop at last_entry (15:10)". The yaml `last_entry: "15:25"` and the default (`flatten_at`) give 15:25, and the replays confirm entries were allowed until 15:25.
- `execution.py:136-138` says "non-expiry trade_small = 0.5". `regime.py:62` always sets 1.0.
- Behaviour is correct; the comments mislead the next editor.
- **Fix.** Correct both comments.

## What 2.0 does better than 1.0 (evidence)
- **No trades on the frozen or garbage 15:15+ feed.** Applied to Monday, 1.0 would have entered at 15:20 on all three recorded closes (B +2,821, C +8,460, D −351; the profits are luck on a frozen spot). 2.0 took none. 1.0 also re-anchored on garbage: the 15:22:17 band in C had basis −182.7.
- **Strike = current ATM.** In A, 1.0 bought 22750 CE (105 pts ITM, ₹196, 455 qty). 2.0 bought 22850 CE (₹134, 715 qty) and made +10,618 vs +6,233. On the synthetic expiry, 2.0's ending capital is higher in all three runs.
- Positions are managed even on rounds where the NIFTY basis or band fails. 1.0 skipped stops and the flatten on those rounds (`orchestrator.py:769-803`, read in code; not triggered in these windows).
- Event noise is lower at the same trading behaviour: REVERSAL_WATCH 395 → 3 (A) and 580 → 0 (S).
- Replay streams the tape: window A took 149 s in 2.0 vs 571 s in 1.0, which also wrote a 1.1 GB tick jsonl per hour.

## What 2.0 does worse than 1.0 (evidence)
- The feed gate can block entries on a real fast move: SPOT_JUMP has no future cross-check (MEDIUM, plausible).
- The feed gate leaks during the garbage tail, so 2.0 re-anchored on bad data at 15:21:30 (HIGH). This is not worse than 1.0, which always did it, but the safety claim is weaker than stated.
- The synthetic expiry demo is now dominated by false FEED_UNHEALTHY.
- Otherwise, nothing on an expiry day is worse.

## Tests / checks you ran
- `python3 -B drive.py <root> <tape> <t0> <t1> <out> expiry [k=v]`: windows A, B, C, D and S on both engines (≈ 2.5 h of tape each; no full-day replay).
- `python3 -B synth.py <root> <out> {150|1200|1400} 15 {13:00|09:21|09:30}` on both engines.
- `NEUTRALIZE=1 python3 -B drive.py /home/claude/teardown/vajra2 … feed_sanity.enabled=off freshness.hold_while_in_trade=off` on A, C and D, then `strict.py`: orders, P&L and decision words identical to 1.0.
- `feedstats.py`: 2.0's `FeedHealth` run over the recorded NIFTY basis tapes to get trip times and clean maxima.
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_expiry.py tests/test_golden.py tests/test_execution_risk.py` → 22 passed.
- `build_calendar(load_config()).resolve(date(2026,10,19))` → `is_expiry_day=True, IMPLIED, shifted_from_holiday=True` (Monday is correctly an expiry day).
- Scripts and outputs: `/home/claude/teardown/scratch/11/` (`drive.py`, `synth.py`, `cmp.py`, `strict.py`, `feedstats.py`, `out/`).
