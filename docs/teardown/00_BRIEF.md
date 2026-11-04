# VAJRA 2.0 — Independent Teardown (read fully before you start)

You are one of 30 independent reviewers. Each of you works ALONE: do **not** read any other reviewer's
report in `/home/claude/teardown/reports/`, and do not coordinate. Nobody has pre-judged the code for you
— form your own view from the code and data.

## What you are reviewing
* **VAJRA 2.0** (the candidate): `/home/claude/teardown/vajra2/` — read-only snapshot.
* **VAJRA 1.0** (what runs today, for comparison): `/home/claude/vajra2/` — treat as read-only; do not modify.
* Recorded live tapes (all NON-expiry days: Wed 14, Thu 15, Fri 16 Oct 2026; the active weekly expiry was
  Mon 19 Oct because Tue 20 Oct is a holiday): `/home/claude/vajra2/tapes/<date>/` — `tick_tape.parquet`
  (150–285 MB, the full per-round NIFTY/sister/stock chains — stream it with `pyarrow.parquet.ParquetFile(...).iter_batches()`,
  never load it whole: the box has 8 GB RAM and 2 CPUs shared by 20 reviewers), plus `level_tape / basis_tape /
  decision_tape` parquet and `event_tape / order_tape / signal_tape` jsonl as 1.0 recorded them.
* VAJRA is a NIFTY-options **paper-trading** engine (ICICI Breeze feed, FastAPI dashboard on :8766, parquet tapes).
  The operator runs it live on **Windows** (`D:\`), Python 3.13, and mirrors its paper trades by hand.
* Claims in `CHANGELOG.md`, `README.md`, the manuals and code comments are **claims to verify, not facts.**

## The operator's binding rules (these are requirements, not up for debate)
1. **All-in sizing on every trade**: lots = floor(capital / (premium × 65)); capital = ₹1,00,000 start + running
   P&L (compounds through the day). **No daily trade cap. No daily loss stop.** Full capital is how the engine
   is evaluated.
2. Execute NIFTY options only; paper only (`LIVE_ORDERS: false`).
3. Entry: anticipatory, on every approach within 7 pts of a HELD level (or a break through it); no veto from the
   cross-instrument confluence. Entries allowed 09:21 until the 15:25 flatten.
4. Exits: hard stop −12% (before the trail arms); trail arms at +10% peak, floor = max(+7%, peak × 0.94); 15:25 flatten.
5. Cooldowns: 11 min after a hard-stop loss; 15 s after a profitable exit.
6. Levels: NIFTY re-anchors every 20 min (+ after exits / cooldown ends); sisters/stocks every 3 min.
   Expiry-day level formula `K ± round(1.1·VWAP(1-ITM)) − basis` is locked.
7. Universe: NIFTY, BANKNIFTY, FINNIFTY + 21 stocks.
8. The operator wants the engine to trade non-expiry days (it was idle on them), the dashboard to show tick
   updates without delay, and **no unnecessary alert sounds**.
9. It must run on **Monday 19 Oct 2026** (an EXPIRY day) without hiccups.

## Rules
* Write ONLY your report file (exact path in your task). Scratch scripts go in `/home/claude/teardown/scratch/<NN>/`.
* Run Python with `python3 -B` and pytest with `python3 -B -m pytest -p no:cacheprovider` (the snapshot is read-only).
  To run the dashboard, pick a port 8900+NN to avoid collisions.
* At most ONE full-day replay per reviewer (each takes 4–8 CPU-minutes). Prefer windows of a few minutes.
* `vajra/credentials.py` holds real broker secrets: never print, copy or quote its values.
* Evidence over opinion: cite `file:line`, show the command you ran and its output, give a concrete failure scenario
  and a concrete fix. Rate each finding **BLOCKER (must fix before Monday) / HIGH / MEDIUM / LOW** and state your
  certainty (*reproduced* / *read in code* / *plausible*).
* Be adversarial and honest. If 2.0 is worse than 1.0 anywhere, say so plainly. If something is fine, say so briefly.

## Report format (`/home/claude/teardown/reports/NN_<slug>.md`)
```
# NN — <role>: teardown
Verdict: <one line: is your area ready for Monday? better/worse/same as 1.0?>
Score (your area): 1.0 = x/10, 2.0 = y/10
## Findings (most severe first)
### [BLOCKER|HIGH|MEDIUM|LOW] title (file:line) — certainty
- Scenario / evidence (commands + output):
- Fix:
## What 2.0 does better than 1.0 (evidence)
## What 2.0 does worse than 1.0 (evidence)
## Tests / checks you ran
```
Keep it under ~300 lines. Your final message: a 6-line summary (verdict, scores, top 3 findings).
