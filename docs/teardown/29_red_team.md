# 29 — Red team: teardown
Verdict: NOT attack-proof for Monday, but better than 1.0. 2.0 now holds up against the attacks that actually hurt 1.0 on the recorded days: trading the 15:15–15:25 frozen or garbage cash feed, missing a stop when the band/basis fails on a round, and bad-tick crash floods. It is still open to (a) unvalidated option prices (NaN, a garbage ask or bid) that mis-size or wipe capital and can kill the engine for the rest of the day, (b) levels that go stale and are traded silently, (c) a dashboard that shows "BUY …" when the engine will not trade, (d) a sound policy that rings mainly when no trade is possible, and (e) a regression where replay drops or crashes on a restarted or killed day.
Score (your area): 1.0 = 3/10, 2.0 = 5/10

All reproducers are in `/home/claude/teardown/scratch/29/`. `harness.py` builds a real `Orchestrator` (the real config, a session on Mon 19-Oct = EXPIRY) and feeds it hand-made NIFTY snapshots at 4 rounds/s. `attacks.py <repo>` runs every attack against 2.0 (`/home/claude/teardown/vajra2`) or 1.0 (`/home/claude/vajra2`). Unless marked otherwise, the outputs below come from `python3 -B attacks.py <repo>`.

## Findings (most severe first)

### [HIGH] A NaN option price is accepted. It sizes 30,769 lots, a position that never stops out, NaN capital, and then the engine crashes for the rest of the day (execution.py:195-206, 174-178, 277-281; orchestrator.py:528-820 has no try/except around the decision/entry section) — reproduced
- `breeze._f()` (breeze.py:343) returns `float("nan")` for a `"nan"` string, and nothing downstream checks ask/bid for finiteness. `manage()` checks only `ltp` (execution.py:228).
- The entry check `entry_prem < min_premium` is False for NaN, so it passes. `compute_lots` does `max(0.05, nan)`, which returns 0.05, so it sizes `100000 // (0.05*65)` = **30,769 lots** at fill NaN. Every stop comparison against NaN is False, so the position is never stopped.
- Output of `A1_nan_ask`: `ENTRY 25050 CE qty 1999985 fill nan` / `after -50% premium: open? True` / `capital after flatten nan`.
- A NaN **bid** on an exit mid-day gives `capital nan`. The next entry then raises `ValueError: cannot convert float NaN to integer` (`compute_lots`) inside `process_round`. That is outside the per-instrument guard, so `run()` catches it, writes SESSION_ERROR and calls `finalize()`. **The engine stops for the rest of the day.** (`A1b_nan_bid_midday`: `process_round CRASH -> ValueError`.)
- 1.0 behaves the same (same output). This is not a regression, but 2.0 left it open.
- Fix:
  - In `_build_snapshot`/`_snapshot_from_row`, drop a quote unless `ltp`, `bid` and `ask` are finite and ≥ 0.
  - In `Executor.enter`/`_close`, use ask/bid only when it is finite and within, say, ±15% of ltp; otherwise fall back to ltp.
  - Wrap the decision/entry/manage block of `process_round` in its own try/except that emits an ANOMALY, so one bad value can never call `finalize()` at 10:00.

### [HIGH] A garbage ask or bid is booked as the fill: the entry is sized on a bad price, and a single bad bid books a near-total loss (execution.py:66-87, 195, 272-277) — reproduced
- Nothing checks the book against the last trade. Entry is sized and filled at `q.ask`, exit fills at `q.bid`, while stops trigger on `q.ltp`. Results:
  - `A2_ask_high`: ask 300 while ltp 100 → `ENTRY qty 325 fill 300.0`, next round `SL_HIT fill 99.5 pnl -65,162`, capital 34,837.
  - `A2b_ask_low`: ask 46 while ltp ~100 → 33 lots, booked as a +1.16 L unrealised gain.
  - `A3_bid_garbage_exit`: ltp 88 (stop), bid 0.05 → `EXIT fill 0.05 SL_HIT -97,938`, capital 2,061. The rest of the day then compounds on ₹2k instead of about ₹88k.
- Adding to this, `BreezeWrapper._on_ticks` (breeze.py:255-260) carries a **stale** bid/ask forward whenever a tick arrives with 0 or missing, so a stale book is the realistic version of this attack.
- I scanned the recorded NIFTY near-ATM quotes (`quotes_scan.py`): 275,640 on 10-14 and 600,300 on 10-16. None had ask or bid more than 12% away from ltp, so the recorded days are clean. This is an adversarial or bad-tick case, not one seen on tape.
- 1.0 is identical.
- Fix: the same plausibility clamp as above (fill = ltp when the book is crossed, stale or more than X% away). Also record `fill_source` on the order row.

### [HIGH] A restart mid-session resets capital to ₹1,00,000, drops the open position from the engine's books and forgets the cooldown (orchestrator.py:176, execution.py:107-108, 135) — read in code + tape evidence
- No state is restored from `order_tape` on start. The operator's real starts show this matters:
  - 10-14 started at 13:25, 10-15 at 09:41.
  - 10-16 had **4 STARTUP_CONFIG events (10:24, 10:28, 10:46 SESSION_ERRORs, 11:10)** in `tapes/2026-10-16/event_tape.jsonl`.
- A restart after the morning's P&L breaks rule 1 (capital = start + running P&L). A restart while in a trade leaves the operator holding a real position the engine no longer manages. A restart inside the 11-minute stop cooldown re-enters immediately.
- 1.0 is the same.
- Fix: on start, rebuild `capital`, `position`, `_last_stop_ms` and `_last_profit_ms` from today's `order_tape.jsonl`, and emit RESUMED_STATE.

### [HIGH] Replay of a restarted or killed day loses or crashes on rows that 1.0 replayed (feed.py:85-96, tape.py:86-104) — reproduced, regression
- 2.0's `ReplaySource` now **prefers the parquet**. A same-day restart renames the earlier parquet to `.bak`, so the parquet holds only the last session.
- Test `rs/`: two sessions of 1000 + 300 rounds, then `ReplaySource` → `2.0 ReplaySource rounds 300` while the jsonl has 1300. The morning is silently gone.
- The recorded `tapes/2026-10-16/tick_tape.parquet` itself starts at **11:11:02** (row-group stats), which matches the 11:10 restart.
- A hard kill (console closed: no `finally`, no `close()`) leaves a parquet without a footer. In `kill.py` both of these raise `ArrowInvalid: Parquet magic bytes not found`: `ParquetFile` and `2.0 ReplaySource CRASH: ArrowInvalid`. 1.0 replays the same folder from the jsonl (`1.0 replay rounds 700`).
- The manual (OPERATOR_MANUAL.md:221-223) tells the operator to delete old jsonl, which makes the parquet the only copy.
- Fix:
  - Make ReplaySource read `*.parquet.*.bak` + `.parquet` in ts order.
  - Fall back to the jsonl when the parquet is unreadable or ends before the jsonl does.
  - Write parquet as rolling closed chunk files (for example every 5 min) so a kill loses at most one chunk.

### [MEDIUM] Stale levels are traded: the LEVELS_STALE gate is open 238 of every 240 rounds after a failed re-anchor (orchestrator.py:492-500, freshness_engine.py:73-81) — reproduced
- When the 20-minute re-anchor fails (an ITM quote is missing), `_levels_stale=True` lasts only for that one round. On the next round `need` is False (30 s retry) and `band is not None`, so the flag is cleared again.
- `stale.py` (09:40 re-anchor fails, VWAPs have moved): `levels_stale now: False | held band 25060.0 | last success age s 1205.0`, then `ENTRY 25050 CE`. That entry is on a 20-minute-old band, while a fresh band would be about ±15 pts. `A4`: stale on `2 / 240` rounds.
- The same happens after a failed 09:20 "open" calc: the engine then trades **yesterday's prev-close seed**.
- 1.0 has the same flag logic.
- Fix: clear `_levels_stale` only in `mark_recomputed` / a successful recompute, using `freshness._last_success_ms >= _last_attempt_ms`. Do not clear it because "a band exists".

### [MEDIUM] The dashboard tells the operator to BUY when the engine will not trade (orchestrator.py:1164-1188; dashboard.py:711-717, 826-843) — reproduced
- `_compute_alarm` builds `target = "BUY NIFTY <ATM> CE/PE"` whenever spot is in the 7-point zone, whatever the entry gate says. Only INSUFFICIENT_CAPITAL and PREMIUM_TOO_THIN are explained in the message.
- `A24_buy_after_flatten` (15:26): `gate AFTER_LAST_ENTRY | state ABOUT_TO_BREACH | target BUY NIFTY 25050 CE`.
- `A24b_buy_on_garbage_feed` (cash spot frozen 35 s, then +150): `gate FEED_UNHEALTHY` but the banner reads `BREACHED UPPER 25060 by 90.0 pts -> BUY NIFTY 25150 CE`. That strike comes from the garbage spot.
- `a10.py` (inside the 11-minute stop cooldown): the cooldown banner wins (dashboard.py:829), but the hero-panel "nbuy" box and THE SCREAM target still show `BUY NIFTY 25100 CE`.
- **Real tape** (`late.py … 2026-10-16 15:00`, a 15:00–15:30 window of the recorded day): the final cockpit state is `BREACHED BUY NIFTY 23200 CE | gate AFTER_LAST_ENTRY | feed SPOT_IMPLAUSIBLE basis -89.45`.
- The operator mirrors by hand, so this is a direct path to a manual trade the engine never took.
- 2.0 does better than 1.0 on the engine side: 1.0 actually *entered* on A24b (`IN TRADE UP CE 25000`) and has no gate text at all.
- Fix: when `self._entry_gate` is not None and not NO_APPROACH, set `target=None` and append `[NO TRADE: <gate text>]` to the message.

### [MEDIUM] The "approach" cue rings mostly when no trade is possible, and doubles every real entry (orchestrator.py:678-683; alerts.py) — reproduced
- The comment says "only when a trade is actually possible", but the condition checks only `health_ok and not position_active`. It ignores the stop and profit cooldowns, LEVELS_STALE and premium rejects.
- When every gate is open, the entry fires **on the same round** as the approach, so the cue always lands together with the entry cue:
  - `t_base.py`: `approach … ts 1792381888250` + `entry … ts 1792381888250`.
  - `a10.py`: during the stop cooldown → `('approach', 'NIFTY upper level 25115 (7 pts)')` with no trade.
- In practice the approach cue is either redundant (it duplicates entry) or misleading (no trade follows). In "all" mode that breaks rule 8 ("no unnecessary alert sounds"). The JS migrates a 1.x `vajra_sound=1` setting straight to "all" (dashboard.py:538-539).
- Fix: emit `approach` only when `gate is None` and the entry did not fire this round (or drop the kind). Also do not migrate legacy users to "all".

### [MEDIUM] Garbage cash spot inside the sanity thresholds still triggers entries (feed_health.py:73-79) — reproduced
- The gate catches jumps over 40 pts per round or |basis| over 45. A spot that walks +30 then +14 (true market flat; options price F = 25000) passes both, and the engine buys.
- `garb.py`: `feed {'ok': True, 'basis': -44.0}` / `orders [('ENTRY', 25050, 'CE', 40.5)]`.
- The recorded garbage (+185/+240/−134) is caught, so this is residual risk only.
- Fix: also gate the entry on |basis − basis_ewma| (for example over 15 pts) rather than |basis| alone. Clean intraday basis drifts slowly.

### [MEDIUM] A single bad ltp print forces an exit; an up-spike exits as a TRAIL_SL *loss* with no cooldown (execution.py:236-260; orchestrator.py:791-798) — reproduced
- `A9_spike_tick`: one ltp=200 print (entry 100.5) sets peak 200 and floor 188. The next real tick then gives `EXIT … TRAIL_SL -975`, `stop-cooldown False`.
- `A9b_dip_tick`: one ltp=0.05 print gives `SL_HIT -975` and imposes an 11-minute cooldown.
- `A13`: a gap through the trail floor with a wide bid gives `TRAIL_SL -10,237`, and neither cooldown applies, because cooldowns key on the *reason* (SL_HIT), not the sign of the P&L.
- 1.0 is identical. The recorded tapes show no such spikes (`quotes_scan.py`; the only >10% one-round moves are the 15:20 garbage resume).
- Fix: require two consecutive confirming prints (or ltp within the bid/ask) before a stop or peak update. Decide explicitly whether a losing TRAIL_SL gets the 11-minute cooldown.

### [LOW] The trail does not arm at exactly +10% (float edge) (execution.py:246) — reproduced
- `100*1.1 = 110.00000000000001`, so a peak of exactly 110.00 (on a 0.05 tick grid) does not arm.
- `A14`: `peak 110.0 armed False`, the price then falls to 105 (below the +7% floor that should have applied) and is held, and the trade ends `SL_HIT -11,700` instead of being booked around +5%.
- 1.0 is identical.
- Fix: `peak >= entry*(1+act) - 1e-9`, or compare in paise.

### [LOW] Config foot-guns
- **(a) `LIVE_ORDERS: "false"` (quoted) → `bool("false")` = **True** (execution.py:105, config.py:466).**
  - The same yaml teaches quoting: `"on"`/`"off"` for `feed_sanity.enabled`, `hold_while_in_trade` and `sounds.*`.
  - `garb.py`: `LIVE_ORDERS: "false" -> True`. In live mode that routes all-in orders to ICICI.
  - Severity is LOW only because it needs a config edit; the impact is real money.
- **(b) `last_entry` later than `flatten_before` (2.0-only key).**
  - `A12`: 15:30 → **403 orders** after 15:25: enter → EXPIRY_FLATTEN → enter … every round.
  - orchestrator.py:99-100 still says "last_entry (15:10)" while the yaml says 15:25.
- **(c) A time string with seconds.**
  - `trade_start: "09:21:30"` raises `ValueError` in `parse_hhmm` on every round. That kills the run at the first round (`A12b`; 1.0 too).
- Fix: a typed config validator at startup — booleans must be YAML bools, `last_entry <= flatten_before`, HH:MM regex.

### [LOW] Sound edge cases
- A zero-P&L exit plays `exit_profit` (`pnl >= 0`, orchestrator.py:789; `A_zero_pnl_sound`).
- A `finalize()` END_OF_SESSION exit plays no sound at all (orchestrator.py:961-964).
- `feed_alarm` is a TRADE_KIND in the JS (dashboard.py:532), so it plays even in "trades only" mode. That is defensible, but the README says trades-only means entry and exits.

### [LOW] SSE fan-out cost scales with tabs (dashboard.py:68-96)
- Each client does its own `_clean` + `json.dumps` per round. On a real 22-instrument state (`real.py`): full = 20.7 KB at 7.8 ms, hot = 2.9 KB at 0.76 ms. That is about 11 ms of CPU per client per second, on the same GIL as the engine thread.
- With 60 clients the round rate halved in my test, but the box load average was about 39, so this is not attributable. Plausible only with many tabs.
- Fix: serialise once per round and fan the bytes out.

## Attacks 2.0 survived (and 1.0 did not, unless stated)
- **Garbage or frozen 15:15-15:25 cash feed**: the 2.0 gate closes (`A24b`: `gate FEED_UNHEALTHY`, no order). 1.0 entered (`IN TRADE UP CE 25000`). The recorded 1.0 tapes show the same: 10-16 15:24:47 `ENTRY 23300 PE` on the garbage feed, and 15:20 entries on all three days. On the real 10-16 15:00–15:30 window (`late.py`), 2.0 placed **0 orders**: the feed alarm rang once (15:15:33 SPOT_FROZEN) and the gate held through the garbage.
- **Stop when the basis/band fails on a round**: `mgap.py` (ATM CE quote missing, held strike collapsing −20%): 2.0 `EXIT SL_HIT`; 1.0 holds and **misses the stop**.
- **Clock edges** (`A5`, both versions): 15:24:59.9 enters, 15:25:00.0 blocked, 09:20:59.9 blocked, 09:21:00.0 enters. 00:00:00 and 23:59:59.9 rounds are no-ops, with no crash. A second position is impossible (POSITION_ACTIVE is the first gate, and `enter()` re-checks). The cooldown blocks a re-entry 200 s after an SL_HIT (`a10.py`: `entry_block POST-STOP COOLDOWN 445s`, no order). All-in sizing is correct: 15 lots on ₹1 L at 100.5 (`t_base.py`); `capital_fraction` is pinned to 1.0 in every regime (regime.py:58-63).
- **Dashboard HTTP**:
  - 1,000 concurrent `/api/state` → `{200: 1000}`.
  - Traversal on `/sounds/../credentials.py` (plain, `%2f`, `%2e%2e`, `%5c`) → 404.
  - `/docs` and `/openapi.json` → 404; POST → 405.
  - NaN in state is nulled by `_clean` (the 1.0 path would 500).
- **Bad seed / bad instrument tick**: wrapped. INSTRUMENT_ERROR is latched once, not re-fired every round.

## What 2.0 does better than 1.0 (evidence)
- Feed-sanity gate (the 15:20 garbage), management on every NIFTY round, buying the current ATM strike, parquet I/O off the engine thread, backend-decided sounds with rate limits (sister breaches silent), and a "why no trade" gate text. Evidence is in the section above.

## What 2.0 does worse than 1.0 (evidence)
- ReplaySource prefers the parquet: a restarted day replays only the last session (300 of 1300 rounds), and a killed day crashes (`ArrowInvalid`), where 1.0 replays both from the jsonl.
- The new `last_entry` key enables an enter/flatten churn when misconfigured (403 orders).
- The new approach cue rings during cooldowns and doubles every entry.

## Tests / checks you ran
- `python3 -B attacks.py <repo>` on both repos: 15 attack functions (A1, A1b, A2, A2b, A3, A4, A5, A9, A9b, A10, A12, A12b, A13, A14, A_zero), plus `a10.py`, `stale.py`, `mgap.py`, `garb.py`, `t_base.py`.
- `kill.py` (hard kill with `os._exit`) and the `rs/` restart test, run against `vajra.tape.TapeWriter` + `ReplaySource`.
- `quotes_scan.py` over the 10-14 and 10-16 tick tapes (NaN / book / spike scan), and `spike.py` for 10-16 14:27:45 (no spike).
- Dashboard on :8929 (`dash.py`): `hammer.py` (1,000 requests), `sse.py` (1 / 5 / 15 / 60 clients), traversal curls.
- `late.py` on the real 10-16 15:00–15:30 window (2.0).
- `real.py`: 1,200 real rounds of 10-16 through 2.0 (p50 37 ms per round on the loaded box), plus SSE serialisation cost.
- `pytest tests/test_v2.py tests/test_execution_risk.py tests/test_timing.py` → 41 passed.
- No full-day replay was used.
