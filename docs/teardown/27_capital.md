# 27 — Capital & compounding accounting: teardown
Verdict: Sizing and compounding are correct and identical in 1.0 and 2.0 (all-in, no cap or minimum, compounds exactly, no float drift that can change a lot count). The one real gap is a mid-day restart: capital silently resets to ₹1,00,000 and an open trade is orphaned. 1.0 has the same gap; 2.0 makes it no worse. The area is ready for Monday as long as there is no mid-day restart. Overall: same as 1.0, plus a deliberate strike change.
Score (your area): 1.0 = 7/10, 2.0 = 7.5/10

## Findings (most severe first)

### [HIGH] Mid-day restart resets capital to ₹1,00,000 and orphans the open position (vajra/execution.py:107-108, vajra/app.py:133) — reproduced
- `Executor.__init__` always sets `self.capital = starting_capital`. Nothing reads today's `order_tape.jsonl` back on startup: grep for restore/resume/rehydrate finds nothing in `vajra/`. `OrderWrapper._paper_seq` also restarts, so order ids repeat (PAPER-1…) in the appended jsonl. The docs claim "Mid-session restarts are safe … the engine re-seeds" (OPERATOR_MANUAL.md:105). They admit the position is lost, but they never say that capital resets too. RUN_LIVE.md:24 and README.md:41 document `-SkipVerify` for mid-day restarts, so restarts are an expected operation.
- Reproduced with the real 2.0 Orchestrator on the recorded 15 Oct tape, window 09:50–10:10, restart forced at 09:58 while a trade was open:
  ```
  python3 -B scratch/27/orch_window.py <2.0> 2026-10-15 09:50 10:10 --restart 09:58
  RESTART at 09:58:00 engine capital before 106708.0 open pos True
  session 0: 09:52:09 ENTRY 22800CE 780 ... 09:52:59 EXIT TRAIL_SL +6708.0 ; 09:54:15 ENTRY 22850CE 845 @120.35  (never exited)
  session 1: 10:00:17 ENTRY 22850CE 715 @136.1 PAPER-1  -> 10:08:15 EXIT TRAIL_SL +9402.25 PAPER-2
             dashboard {"starting":100000,"equity":109402.25,"pnl":9402.25}
  ```
  Without the restart, the same window has the 09:54 trade exit at 10:01:49 for +15,632.5, and capital is 1,22,340.5 by the third entry. After the restart:
  (a) The 22850 CE ×845 trade has an ENTRY in the tape but never an EXIT. The operator, who mirrors by hand, gets no exit signal for it.
  (b) The next entry is sized off 1,00,000 instead of 1,06,708 + the open trade's result. That gives 11 lots instead of 12–13, which breaks binding rule 1.
  (c) The dashboard says the day's P&L is +9,402. `order_tape.jsonl` sums to +16,110 for the same day. The tapes and the dashboard now disagree.
  (d) Order ids PAPER-1 and PAPER-2 now appear twice in the same file.
- 1.0 behaves identically (execution.py has the same init, and diffing the two versions shows no restore code).
- Fix (≈30 lines, in `Orchestrator.__init__` after the Executor is built, or in `app.py` before `run`):
  1. Read `tapes/<today>/order_tape.jsonl` if it exists.
  2. Set `capital = starting_capital + Σ pnl_realized` over the EXIT rows.
  3. Set `wrapper._paper_seq = max(int(id[6:]) for PAPER-n ids)`.
  4. If the last ENTRY has no EXIT after it, rebuild `Position(strike, right, qty, entry_fill=fill_price, entry_ts=ts, cost=qty*fill, peak=entry_fill)`, replay `journal`/`_open_trade`, emit an ANOMALY `POSITION_RESTORED`, and let `manage()` keep running it.
  5. Add a test that restarts mid-trade and checks that capital and the position carry over.

### [MEDIUM] Out of capital or a thin premium writes one REJECT row per round to the binding order_tape (vajra/orchestrator.py:733-760, vajra/execution.py:204-208) — reproduced
- Every round inside the 7-pt zone with no position calls `enter()` again, and each call writes another REJECT row. The ENTRY_REJECTED *event* is limited to one a minute (`_emit_alert_event`), but the `order_tape` rows are not limited at all.
  ```
  orch_window.py <2.0> 2026-10-15 09:50 09:56 --capital 2500
  09:52:09 REJECT 22800 CE 0 ... INSUFFICIENT_CAPITAL   (x900 rows in ~4 min, 1437 rounds)
  dashboard {"equity":2500.0,"pnl":-97500.0}  -> engine correctly never trades again (no forced minimum)
  ```
- What happens below one lot is correct per the rules: lots=0, the order is rejected, and no lot is forced. The engine stays idle for the rest of the day, because capital cannot recover without trading. Reaching that point from 1L needs about 27 consecutive −12% stops (1e5·0.88ⁿ < 45·65), so for capital it is unlikely. But the same per-round flood happens with `PREMIUM_TOO_THIN`, which is much likelier. Example: on 15 Oct at 15:20:11 the current-ATM 23150 CE ask was 32.95, below the non-expiry floor of 45. The NIFTY approach *sound* (orchestrator.py:679) also still plays when no trade is possible for lack of capital.
- Same in 1.0.
- Fix: write a REJECT row to `order_tape` only when the reject reason or strike changes, or at most once per approach episode. Keep the per-round reason in `decision_tape`, where it already goes. Also gate the approach sound on `compute_lots(atm_ask) > 0`.

### [LOW] Dashboard equity marks the open position at LTP, but the exit fills at the bid (vajra/orchestrator.py:1247-1252) — reproduced
- 1.0 window run on 15 Oct (09:50–10:15): the last dashboard state showed `unrealized 1040.0, equity 101040.0`. The actual END_OF_SESSION exit filled at bid 191.6 against LTP 192.0 and booked +832.0. So the dashboard overstated realizable equity by ₹208.
- This is cosmetic: it never feeds sizing, because sizing uses booked `ex.capital`. Same in 1.0 and 2.0.
- Fix: mark at `bid` when it exists (`q.bid or q.ltp`), or label the figure "mark @LTP".

### [LOW] order_tape `pnl_realized` is written as raw float, e.g. -2180.750000000008 (vajra/execution.py:277) — read in code and seen in the 1.0 tapes
- All three recorded days show this: 6233.4999999999945, 8527.999999999989, -68.25000000000259. The drift never changes a lot count (see the next item). But the operator copies these figures by hand.
- Fix: `pnl = round((fill - pos.entry_fill) * qty, 2)` and do the same for `cost`. Even better, keep capital in integer paise.

### [INFO] Float drift cannot change a lot count (proof plus a 300-trade run) — reproduced
- Every quantity is a multiple of 65 and every price sits on the 0.05 tick. So capital in 0.05-rupee units is always ≡ 2,000,000 ≡ 15 (mod 65): it starts there and each trade adds Δticks·65k. A lot boundary is lots·65·ask, which is ≡ 0 (mod 65). Capital therefore always sits at least ₹0.75 from any lot boundary. Float error is around 1e-9, so `capital // (ask*65)` always gives the exact floor. A brute-force search over 300k random trades found 0 exact-boundary cases, which matches the proof.
- Five seeds × 60 trades through the real Executor gave 0 lot violations and 0 P&L violations. The largest drift against an exact-Decimal reference was 5e-10 after 60 trades (capital ₹12.47L). The qty sequences from 1.0 and 2.0 were identical for every seed:
  ```
  seed 4 trades 60 cap1 1246947.7500000005 cap2 1246947.7500000005 exact 1246947.75 drift 5e-10 viol 0 0 qty_identical True
  ```
  Separately, `trades_summary.realized_pnl` sums *rounded* journal P&L, so it can differ from engine capital by at most ₹0.005 per trade. The window runs found no gap above 0.05.

### [INFO] `capital_fraction` plumbing is dead but harmless (vajra/regime.py:56-63, vajra/execution.py:177, orchestrator.py:177)
- `resolve_regime` hard-codes `capital_fraction=1.0, max_trades=0, daily_loss_stop_pct=0.0` in both regimes and reads no config key for them. `max_trades` and `daily_loss_stop_pct` are not enforced anywhere. The executor still has a stale comment, "non-expiry trade_small = 0.5 by default" (execution.py:136-137), which is false. tests/test_v2.py:56,59,242 lock the value at 1.0.
- Recommendation: delete the multiplier, or at least the misleading comment, so nobody wires it back up by mistake.

## Sizing comparison 1.0 vs 2.0: the strike change, quantified
The sizing formula is byte-identical: `int(capital // (prem*65))`, with prem = ask, or LTP when there is no ask. The diff is only the optional `capital_fraction` multiplier, which is always 1.0. Sizing is computed on the same ask that becomes the fill (wrapper.place gives BUY at ask), so cost ≤ capital always holds. The only behavioural change is which strike gets bought. 1.0 buys the band's anchor-time ATM (`band.atm_strike`, execution.py:184 in 1.0). 2.0 buys the current ATM (`_nifty_head["atm"]`, orchestrator.py:722/741).

Applied to the five recorded 1.0 entries, using the tick_tape row at each entry timestamp (`scratch/27/strike_q.py`):

| day / time | spot | 1.0 strike (ITM depth) ask → lots, util | 2.0 current-ATM ask → lots, util |
|---|---|---|---|
| 14 15:20:03 | 22491.6 | 22600PE (+108) 132.25 → 11, 94.6% | 22500PE 91.80 → 16, 95.5% |
| 15 10:00:14 | 22855.2 | 22750CE (+105) 196.00 → 7, 89.2% | 22850CE 134.40 → 11, 96.1% |
| 15 15:20:11 | 23138.9* | 22950CE (+189) 106.00 → 15, 97.3% | 23150CE 32.95 → **REJECT (<45 floor)** |
| 16 15:20:09 | 23222.0 | 23100CE (+122) 91.15 → 16, 94.8% | 23200CE 49.30 → 31, 99.3% |
| 16 15:24:47 | 23227.2 | 23300PE (+73) 220.80 → 7, 92.6% | 23250PE 181.00 → 9, 97.6% |

*During the 15:15–15:20 feed freeze the spot is bad data: the 22950CE "ITM by 189" was priced at 106.

1.0 was routinely buying 73–189 points ITM, not ATM. The 2.0 change lowers the premium 18–54%, raises lot counts 1.3–2×, and lifts average capital use from about 93.7% to about 97.1%. That is because floor rounding wastes less with cheaper lots. Rupee delta exposure is roughly similar (more lots at ~0.5 delta against fewer at ~0.65–0.8). Gamma, theta and %-volatility per rupee are higher, so the −12%/+10% exits will trigger more often. This deliberate change is the only source of the sizing differences. With identical (capital, premium) inputs the two versions produce identical lots, as shown by the 300-trade runs above.

## Recorded 1.0 order_tape cross-check (14/15/16 Oct)
Every entry matches `floor((1e5 + Σ prior pnl)/(fill·65))`. Every exit matches `(exit_fill − entry_fill)·qty` using the **fills, not the intended prices**. For example, on 15 Oct 15:25 the fill-based P&L is +3,022.5 against +3,607.5 from intended prices. The fills are what get booked.
```
14: qty 715 = floor(100000/(132.25*65))*65 OK; exit -2180.75; end 97,819.25
15: 455 OK -> +6233.50 -> cap 106,233.50 -> 975 = floor(106233.5/(106*65))*65 OK -> +3022.50; end 109,256.00
16: 1040 OK -> +8528.00 -> 108,528 -> 455 = floor(108528/(220.8*65))*65 OK -> -68.25; end 108,459.75
```
Compounding through the day is exact on all three days.

## Engine-level window runs (real Orchestrator, recorded tick_tape)
`scratch/27/orch_window.py` checks every round that: each ENTRY qty equals floor(capital_before/(ask·65))·65; fill equals the sizing price; dashboard `capital.equity` equals `ex.capital + unrealized`; `ex.capital` equals 1e5 + Σ order_tape EXIT pnl; `trades_summary.realized_pnl` equals the order_tape sum.
- 2.0, 15 Oct 09:50–10:15, 5,984 rounds: 3 compounding trades (780 → 845 → 975 qty at capital 1,00,000 → 1,06,708 → 1,22,340.5), final 1,30,823.0, **0 mismatches**.
- 1.0, same window: 1 trade (22800CE, ITM), final 1,00,832, 0 mismatches.

## What 2.0 does better than 1.0 (evidence)
- It buys the current ATM, so it uses capital better (~97% vs ~94%). It no longer silently goes 100–190 points ITM (see the table above).
- An open position is now managed even on rounds where the band or basis failed. Stops and the 15:25 flatten therefore book on the same capital path; 1.0 skipped those rounds (orchestrator.py:769-771).
- The feed-sanity gate would have blocked 1.0's 15:20 entries made on frozen or garbage data (14/15/16 Oct), which were sized off nonsense ITM premiums.

## What 2.0 does worse than 1.0 (evidence)
- Nothing in capital accounting. The only additions are the dead `capital_fraction` multiplier and its misleading comment (execution.py:136-138), which add risk without adding function.

## Tests / checks you ran
- Read execution.py in both versions and diffed them; read regime.py, the Orchestrator entry/manage/_emit_orders/_capital_block/_update_view paths, tape.py restart handling, and the config `execution:` blocks (diffed).
- `scratch/27/exec_seq.py`: 5 seeds × 60 trades through each version's real Executor, against an exact-Decimal reference: identical, 0 violations, drift ≤ 5e-10.
- `scratch/27/boundary.py`: boundary search; the modular proof shows no float floor off-by-one is possible.
- `scratch/27/strike_q.py`: 1.0-vs-2.0 strike and sizing on the recorded entries.
- `scratch/27/orch_window.py`: windowed Orchestrator runs on 15 Oct for 1.0, 2.0, 2.0 with a restart, and 2.0 with capital at 2,500.
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_execution_risk.py tests/test_entry_path.py` gave 33 passed.
