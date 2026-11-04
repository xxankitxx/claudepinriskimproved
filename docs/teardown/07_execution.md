# 07 — Execution, sizing and exit-ladder teardown: teardown
Verdict: Ready for Monday with no blocker in my area. 2.0 is slightly better than 1.0 here (management runs every round, current-ATM strike, fallback walk stays inside the chain). But it inherits 1.0's unguarded NaN-quote entry path, and "expiry day is unchanged" is not literally true: the strike it buys changes.
Score (your area): 1.0 = 6/10, 2.0 = 6.5/10

Scope: `vajra/execution.py` (2.0 vs 1.0 diff is only 3 hunks: `capital_fraction`, `atm_strike=` param, budget line), plus the orchestrator call sites `enter` (orchestrator.py:733), `manage` (:780), `force_flatten` (:964 finalize), cooldowns (:656-666, :789-798), gates (:691-713).

## Operator-rule compliance (verified)
| Rule | Status | Evidence |
|---|---|---|
| All-in sizing, lots = floor(capital/(prem*65)) | OK | execution.py:170-178. Edge run: 1,00,000 @ ask 100 gives 15 lots (975 qty); after a +7,897.5 win, the next sizing @100 gives 16 lots (compounds) |
| capital_fraction = 1.0 everywhere | OK, but dead mechanism | regime.py:56 and :62 hard-code 1.0 (not read from yaml); orchestrator.py:177 copies it; test_v2.py:56/59/242 assert it. Executor clamps to [0,1] |
| No trade cap / no loss stop | OK | `max_trades=0`, `daily_loss_stop_pct=0.0` are hard-coded and never enforced anywhere (grep: only regime.py/dashboard display). `_entries_today` is a counter only |
| Hard stop −12% (pre-arm) | OK | 88.01 holds, 88.00 exits SL_HIT −11,700 |
| Trail arms +10%, floor max(+7%, peak×0.94) | OK | yaml:202-204 sets all three tiers to 0.06, so the tier code is inert. Peak 150 gives floor 141.0: 141.1 holds, 141.0 exits TRAIL_SL |
| 15:25 flatten | OK | 15:24:59 holds; 15:25:00 gives EXPIRY_FLATTEN |
| Entries 09:21 to 15:25 | OK | yaml:224 `last_entry: "15:25"` (the orchestrator.py:99 comment says 15:10, which is stale) |
| Cooldowns 660 s after SL_HIT, 15 s after profit | OK as written; there is a gap (finding 2) | orchestrator.py:103-105, 659-666, 789-798 |
| Floors: 45 non-expiry, 30 expiry | OK | 44.95 is rejected and 45 enters; 29.9 is rejected and 30 enters |
| Expiry-day ITM fallback | OK | CE: ATM 20 / 1-ITM 28 / 2-ITM 55 enters 24900 @55. PE: walks to 25050 @40. Non-expiry thin ATM is rejected (no fallback) |
| Fills BUY@ask, SELL@bid | OK | execution.py:66-76; falls back to ltp when ask/bid is 0 |
| LIVE_ORDERS false, paper | OK | yaml:189 |

## Findings (most severe first)

### [HIGH] NaN ask (or NaN ltp with ask 0) passes every entry guard and gives a 30,769-lot position, then a NaN capital kills the session (execution.py:195-206, 174, 177-178; orchestrator.py:733 is outside any per-round try) — reproduced at unit level; feed occurrence plausible
- Scenario: `entry_prem = q.ask if q.ask else q.ltp`. NaN is truthy, so NaN is used. `NaN < min_premium` is False, so the floor check passes. `compute_lots`: `max(0.05, NaN)` returns 0.05, so lots = 100000 // (0.05*65) = **30,769**. `place()` fills at NaN. The manage NaN guard (execution.py:228) checks only `ltp`, so a NaN **bid** on exit books `pnl = NaN` and `capital = NaN`. The next `compute_lots` raises `ValueError: cannot convert float NaN to integer`. `enter()` runs in `process_round` outside the per-instrument try (orchestrator.py:568-572), so the exception reaches the run-loop catch (:950). The session then finalizes and the engine is dead for the day. A position entered at NaN never stops either: `max(NaN, prem)` returns NaN, so the peak and the stop comparisons are always False.
- Evidence (`python3 -B /home/claude/teardown/scratch/07/edge.py /home/claude/teardown/vajra2`):
  ```
  NaN ask -> ([('ENTRY', 25000, 1999985, nan, '')], (30769, nan))
  NaN ltp ask0 -> ([('ENTRY', 25000, 1999985, nan, '')], (30769, nan))
  manage NaN bid on SL [('EXIT', 'SL_HIT', nan, nan)] capital nan
    next compute_lots EXC ValueError cannot convert float NaN to integer
  inf ask -> REJECT INSUFFICIENT_CAPITAL   (safe)   neg ask -> REJECT PREMIUM_TOO_THIN (safe)
  ```
  How it can arrive: the live feed does `float(t.get("ask") or 0.0)` (feed.py:416), and breeze `_f()` (breeze.py:343) accepts the string "nan". Replay rebuilds from parquet doubles. The recorded 10-15 tape is clean (797,458 near-ATM NIFTY quotes: 0 NaN/null/≤0, 266 crossed bid>ask; `/home/claude/teardown/scratch/07/scan.py`), so this has low likelihood. The impact is extreme for a hand-mirroring operator who sees "BUY 1,999,985". Code is identical in 1.0.
- Fix: add a `_px(x)` helper that returns x if `x is not None and math.isfinite(x) and x > 0` else None. Use it for ask, bid and ltp in `enter`, `_nearest_itm_above_floor` and `OrderWrapper.place`. If a BUY has no finite positive price, REJECT `NO_QUOTE`. If a SELL bid is non-finite, use ltp (manage already guarantees a finite ltp). Add `assert math.isfinite(self.capital)` handling in `_close`.

### [MEDIUM] A losing TRAIL_SL exit gets no cooldown at all (orchestrator.py:789-798) — reproduced at unit level
- The stop cooldown is keyed on `exit_reason == "SL_HIT"` and the profit cooldown on `pnl_realized > 0`. The trail triggers on **ltp** but fills at **bid**, and once the trail is armed there is no hard stop. So a TRAIL_SL can book a loss in two ways:
  1. A gap through the floor: armed at 111, next tick 80, gives `TRAIL_SL @80, −19,500`.
  2. The spread: ltp 104.3 is under the floor but the bid is 98, giving `TRAIL_SL @98, −1,950`.
  
  Neither case sets either cooldown. The post-exit re-anchor runs in the same round, and the next round (~250 ms later) may re-enter at full capital. Same in 1.0.
- Fix (operator choice): either apply the 15 s cooldown to *every* exit (`elif action == "EXIT"`), or treat any losing exit as a stop (660 s). The minimum is the first option.

### [MEDIUM] "Expiry day is unchanged" is false: the current-ATM strike changes expiry-day trades (execution.py:188-191, orchestrator.py:722/739; CHANGELOG.md:20 vs :33; regime.py:4 says "identical to VAJRA 1.x") — reproduced
- `atm_strike` is `br.atm_strike = round_to(spot, 50)` from the same round (basis_engine.py:38). It is not stale: if NIFTY fails this round, `nifty_band` is None and the NO_LEVELS gate blocks entry. I believe this is the **correct** reading of `strikes: ATM_or_1ITM`, and it keeps the expiry ITM-fallback walk inside the live ATM±6 window (feed.py:400-405). 1.0 could walk from an anchor ATM that had already drifted toward the window edge.
- It does change expiry-day behaviour. I replayed the bundled 10-15 fixture with its EXPIRY_RESOLVED event patched to `is_expiry_day:true` (so the locked legacy band ran):
  ```
  v1_exp: 10:00:15 ENTRY 22750 CE 455 @196.0 -> 10:08:19 TRAIL_SL +6,233 ; 15:20:12 ENTRY 22950 CE ... +3,022
  v2_exp: 10:00:15 ENTRY 22850 CE 715 @134.4 -> 10:08:16 TRAIL_SL +10,618  (15:20 entry blocked by feed-sanity gate)
  ```
  Same trigger second, different strike (2-ITM vs ATM), different qty and P&L. On a real expiry day the 1-ITM VWAP is small, so the anchor ATM and the current ATM differ only when spot has drifted at least 25 pts from the anchor ATM before the approach, or on a break-through. That will be rarer than in this replay, but it will happen. A cheaper ATM also reaches the 30 floor sooner late Monday, which triggers the ITM fallback more often.
- Fix: none to the code. Get explicit operator sign-off, and correct CHANGELOG.md:20 / regime.py:4 to say "expiry band unchanged; strike = current ATM (was anchor ATM)".

### [MEDIUM] A mid-day restart resets capital to ₹1,00,000 and forgets the open position (execution.py:107-108; no restore anywhere) — read in code
- The manual (OPERATOR_MANUAL.md:105-107) calls restarts "safe" and recommends `-SkipVerify` restarts (run_vajra.ps1:8). It mentions only the lost position. Running P&L is also discarded, so the all-in compounding rule ("₹1L + running P&L") is broken after any token refresh or crash relaunch. A lost position also lets the engine take a new trade while the operator still holds the old one on ICICI. Same in 1.0.
- Fix: on startup, if today's `order_tape.jsonl` exists, set `capital = start + Σ pnl_realized` and rebuild an unmatched ENTRY as an open `Position`. At minimum, emit a loud event naming the restored or reset capital.

### [LOW/MEDIUM] Held strike can leave the snapshot window, and then there is no stop (feed.py:400-407; execution.py:227-229) — plausible
- The live snapshot includes only current-ATM ±6 strikes, even though older subscriptions stay alive. If the held strike is more than 300 pts from spot, `quote()` returns None and `manage()` HOLDs forever. That means no SL, no trail, no 15:25 flatten, and finalize returns REJECT `NO_QUOTE_AT_FLATTEN`. Normal moves cannot get there because the −12% stop or the trail fires first. A garbage spot jump (+240 was seen at 15:2x) shifts the window by 5 strikes, close to the edge. Same in 1.0.
- Fix: always add `(pos.strike, pos.right)` to the snapshot build in `_build_snapshot` while a position is open.

### [LOW] An exact +10% peak does not arm the trail (execution.py:246) — reproduced
- `100*(1+0.10) = 110.00000000000001`, so a peak of 110.00 leaves `trail_floor 0.0`. Exact +10% prices are common at 0.05 ticks. Arming is one tick late, and in the meantime only the −12% stop protects the position. Fix: `>= entry*(1+pct) - 1e-9`.

### [LOW] The alert text can name the wrong strike (orchestrator.py:731)
- APPROACH_ALERT `buy` is `cur_atm`, emitted *before* `enter()`. When the expiry ITM fallback picks another strike, or the entry is REJECTed, the cockpit strip says "BUY NIFTY 25000 CE" while the order is 24900 (or none). The ENTRY sound uses the real strike. Fix: emit the strike from the order row, or drop `buy` from APPROACH_ALERT.

### [LOW] Misc
- execution.py:136-137 comment "non-expiry trade_small = 0.5 by default" and yaml:139 "take paper trades at reduced size" are stale and contradict the locked rule. Delete `capital_fraction` entirely so it cannot be revived.
- The 14:30 `EXECUTION_CAUTION_WINDOW` event text says "size down + tighten exits". Nothing sizes down (correct per the rule), so the text misleads.
- The 15:25 exit is labelled `EXPIRY_FLATTEN` on non-expiry days (1.0 tape shows the same).
- `_close` does not pass `expiry_iso`, so a live EXIT would go out with `expiry_date=""` (breeze.py:324). This is unreachable while `LIVE_ORDERS:false`.
- A persistent REJECT (thin premium / NO_QUOTE) writes one order_tape row and one `trades_log` row per round (≈4/s). The event is rate-limited (:324-331) but the order rows are not.
- A zero bid or zero ask falls back to ltp, which is an optimistic fill on a one-sided book. This is acceptable for paper.

## What 2.0 does better than 1.0 (evidence)
- `manage()` runs every round NIFTY is present, even when the band or basis failed (orchestrator.py:771). In 1.0, management was nested under `nifty_band is not None` (1.0 orchestrator.py:620-629), so stops and the 15:25 flatten were skipped on those rounds.
- Cooldown-end re-anchor happens *before* entry evaluation (:656-667). 1.0 could enter on the pre-cooldown band.
- The strike is the current ATM, and the expiry fallback walk stays inside the subscribed window (see the MEDIUM finding above).
- The feed-sanity gate blocks entries on the 15:15-15:20 frozen/garbage feed. In 1.0, 3 of 5 recorded trades were 15:20+ entries (1.0 order tapes 14/15/16 Oct).
- REJECT/APPROACH events are rate-limited.

## What 2.0 does worse than 1.0 (evidence)
- Nothing is functionally worse in execution. The worse part is documentation: CHANGELOG.md:20 and regime.py:4 claim expiry-day identity that the fake-expiry replay disproves. All execution hazards above are inherited unchanged; 2.0 did not fix the NaN or cooldown holes.

## Tests / checks you ran
- `diff /home/claude/vajra2/vajra/execution.py /home/claude/teardown/vajra2/vajra/execution.py` shows 3 hunks only.
- `python3 -B /home/claude/teardown/scratch/07/edge.py /home/claude/teardown/vajra2`: 30 edge cases covering sizing, floors, fallback, NaN/inf/neg/zero/missing quotes, the ladder boundaries, the spread loss, the 15:24:59/15:25 boundary, the missing quote at flatten, and compounding. Output is quoted above.
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_execution_risk.py tests/test_v2.py tests/test_entry_path.py`: 57 passed.
- Replays of the bundled 10-15 fixture, normal and with a patched `is_expiry_day:true`, on both 1.0 and 2.0 (outputs in `/home/claude/teardown/scratch/07/out/`). 2.0 normal: 4 trades, all TRAIL_SL wins. 2.0 fake-expiry: 22850 CE vs 1.0's 22750 CE. No REJECT rows.
- `/home/claude/teardown/scratch/07/scan.py 2026-10-15`: near-ATM NIFTY quote hygiene on the full recorded tape. 79,746 rounds, 0 NaN/zero, 266 crossed quotes.
- The recorded 1.0 order tapes (14/15/16) contain 5 trades, 3 of them entered at or after 15:20.
