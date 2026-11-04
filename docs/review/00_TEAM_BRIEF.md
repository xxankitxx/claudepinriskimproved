# VAJRA 2.0 — Review Team Brief (read fully before you start)

You are one member of a 30-person engineering/quant review team. The operator (Ankit) runs
**VAJRA**, a NIFTY-options pin-risk / gamma-burst *paper-trading* engine (Python 3.13, ICICI
Breeze feed, FastAPI dashboard, parquet "tapes"). He has asked the team to read the code **in its
entirety**, document the corrections it needs, propose improvements, and help design **VAJRA 2.0**.

The codebase to review (read-only!): `/home/claude/vajra2/`
```
VAJRA_PIN_RISK_ENGINE_SPEC.md   README.md   OPERATOR_MANUAL.md   RUN_LIVE.md   ITERATIONS_LOG.md
vajra_config.yaml   nifty_weights.csv   requirements.txt   run_vajra.ps1   orchestrator.sh
replay.py   replay_browser.py   verify_vajra.py   demo_dashboard.py
vajra/__init__.py app.py breeze.py config.py credentials.py dashboard.py execution.py expiry.py
      feed.py marketdata.py mathx.py orchestrator.py realfeed.py schema.py synthetic.py tape.py timeutil.py
vajra/engine/__init__.py basis_engine.py basis_monitor.py continuation_validator.py decision_maker.py
      freshness_engine.py pin_locator.py trigger_engine.py
vajra/sounds/README.md (+ six .mp3 files)
tests/*.py (10 files, 77 tests — all pass; `python3 -m pytest -q` takes ~12 s)
tapes/2026-10-14, 2026-10-15, 2026-10-16  (recorded live days: level/basis/decision parquet,
      signal/order/event jsonl, tick_tape.parquet 150–285 MB each)
```
**Rules**
1. Do NOT modify anything under `/home/claude/vajra2/`. Write ONLY your findings file (path given in
   your task). You may run read-only analysis scripts (write them under
   `/home/claude/vajra2_review/scratch/<your-number>/`), `pytest` subsets, and small Python snippets.
   Do NOT run full-day replays (each takes 4–5 minutes of CPU; the lead runs those).
2. `vajra/credentials.py` contains real broker secrets. Never copy, print, or quote the values.
3. Be concrete: cite `file:line`, give a failure scenario, and a specific fix (code sketch welcome).
4. Rate every finding: **CRITICAL / HIGH / MEDIUM / LOW**, and your certainty (confirmed by running
   something / confirmed by reading / plausible). Do not pad; a short, sharp list beats a long one.
5. Honest engineering over flattery. If something in the spec or operator rules is wrong, say so.

## The two operator complaints that triggered this review

**A. "I have been sitting idle on non-expiry days."** NIFTY weekly expiry is Tuesday (Monday when
Tuesday is a holiday). The engine is "structurally an expiry-day system" (OPERATOR_MANUAL §1) and
the manual even says zero trades on non-expiry days is *correct*. The operator now wants that fixed:
"narrow the lower and upper pin-risk levels on a non-expiry day if they are too far", and generally
make the system productive every session.

**B. "Too many sounds — ting, ting, ting."** The dashboard (`vajra/dashboard.py`, JS near lines
454–474 and 968–989) plays six sounds. The operator wants a small, deliberate sound policy — fewer
sounds, each one meaningful — and is fine with new sounds being downloaded (CC0) if needed.

## Hard evidence already established by the lead (from the three recorded days)

All three recorded days are NON-expiry days (Wed 14, Thu 15, Fri 16 Oct 2026; the active expiry
was Mon 19 Oct because Tue 20 Oct is the Dussehra holiday).

### A1. The band is far too wide off expiry, and it re-centres on spot every 20 minutes
NIFTY band = `K_CE + round(1.1 × VWAP(1-ITM CE)) − basis` / `K_PE − round(1.1 × VWAP(1-ITM PE)) − basis`.
On expiry day the ITM VWAP is almost pure intrinsic, so the band hugs spot (±25–35 pts). Off expiry
the ITM VWAP carries 80–200 pts of extrinsic (time value), so:

| day | spot range | half-width (upper−lower)/2, p50 | distance to nearest level, p50 | minutes within 7 pts (the entry trigger) |
|---|---|---|---|---|
| 10-14 (3 trading days to expiry) | 190 pts | 132 | 100 | 3 of 124 |
| 10-15 (2 tdays) | 475 pts (trend day!) | 115 | 67 | 6 of 349 |
| 10-16 (1 tday) | 303 pts | 90 | 70 | 4 of 255 |

Because NIFTY re-anchors (`freshness.hold_min_secs: 1200`) with K_CE/K_PE chosen relative to the
*current* ATM, the band is always ≈ ±half-width around the spot at anchor time; price must travel
~100 pts within one 20-minute window to trigger. Even the 475-pt trend day produced ONE legitimate
approach (10:00). `routing.nonexpiry_mode: trade_small` only opens the gate; it does NOT size down.

### A2. ALL of the recorded "trades" at 15:20 were taken on a broken feed
On all three days the Breeze **cash** feed (every index and every stock) froze from 15:15:00 to
15:20:0x (spot unchanged for ~290 s while the options/implied future kept ticking), then resumed
with garbage: NIFTY "spot" jumped +185 / +240 / −134 pts in one tick while the option-implied future
moved < 10 pts (basis −190 / −247 / −64, impossible for NIFTY). The engine then "entered" at
15:20 on every day and was force-flattened at 15:25. Those P&Ls are fiction. There is no frozen-spot
detector, no spot-vs-implied-future plausibility check, and no last-entry cutoff before 15:25.
(ws/api SDK logs show no reconnect at 15:15 — it is upstream; defend against it anyway.)

### A3. Event floods
`REVERSAL_WATCH` fired 1,351 times on 10-15 and 821 times on 10-16 (edge-triggered but flickering
4×/sec). `FALSE_BREAK` fired 12 times in 23 s on 10-15 (10:01:20–10:01:43) while spot jittered
around the level. Schema enum `RECALC_TRIGGERS` lacks `post_cooldown`, which the orchestrator writes.

### A4. Other facts worth knowing
- Pre-open seed on 10-14 came from a June recording ("STALE_PREV_CLOSE_SEED") because no prior live
  tape existed; 10-16 had 4 STARTUP_CONFIG events (3 `breeze_connect_failed`) — restarts mid-day.
- Recorded days start late (13:26 / 09:41 / 11:11) — the operator starts/restarts mid-session.
- TCS is in the universe but missing from `nifty_weights.csv` (zero voice).
- `vajra/breeze.py:_on_ticks` carries forward last-known values for any falsy field (ltp 0 → stale).
- The replay engine is deterministic and reproduces the recorded 10-16 day exactly (2 trades,
  +8,459.75). The lead has a streaming replay harness; the team's proposals will be *measured* on
  these tapes, so give parameters, not vibes.

## What a great findings file looks like
```
# <NN> — <Role>: findings
## 1. Correctness defects (most severe first)
### [CRITICAL] <one-line title>  (file:line)  certainty: confirmed-by-reading
- What happens / failure scenario:
- Why it matters:
- Fix:
## 2. Improvements / design proposals (ranked)
## 3. Recommendations specific to the two operator asks (A: non-expiry levels; B: sounds)
   — concrete parameter values / formulas / policy, with your reasoning
## 4. Things that are fine and should NOT be changed (operator-locked rules you agree with)
## 5. Tests you would add
```
Keep it under ~400 lines. Write it to the exact path in your task description.
