# 01 — Orchestrator round loop & entry-gate order: teardown
Verdict: Mostly ready for Monday. 2.0 is better than 1.0 here: it always manages an open position, re-anchors before the entry check when a cooldown ends, and gates on feed health. But a single exception after the per-instrument loop still ends the whole day, the new LEVELS_STALE gate barely does anything, and the approach sound plays at the wrong times.
Score (your area): 1.0 = 5/10, 2.0 = 7/10

Scope: `vajra/orchestrator.py` (all 1402 lines), plus the parts it calls in `execution.py`, `feed_health.py`, `freshness_engine.py`, `alerts.py`, `regime.py`, `trigger_engine.py` and `vajra_config.yaml`. I diffed it against 1.0 (`diff --strip-trailing-cr`, 487 lines). Scratch scripts are in `/home/claude/teardown/scratch/01/`:
- `harness.py` plus `scen_A..H.py`: a synthetic NIFTY chain on Mon 2026-10-19 (expiry regime). Run as `python3 -B harness.py <root> <scen>`, so the same scenario runs on both 1.0 and 2.0.
- `win.py`: a windowed replay on the real tape (streamed).
- `fh.py`: FeedHealth run over the recorded NIFTY basis_tape.

## Findings (most severe first)

### [HIGH] One exception after the per-instrument loop ends the whole trading day (orchestrator.py:612-820, 934-956) — reproduced
- The per-instrument work is isolated by a try/except (l.570). Everything after it is not: decision.evaluate, `_approach_entry`, `executor.enter/manage`, `_emit_orders`, `_compute_alarm`, `_update_view`, `_idle_diagnostics`. Any exception there goes up to `run()`'s outer `except`. That writes SESSION_ERROR, breaks the loop and calls finalize (flatten + SHUTDOWN). In live mode `app.run` then returns: the dashboard stops and the engine is gone for the day.
- `scen_F.py`: I injected a single `ValueError` into the 200th `_compute_alarm` call.
  ```
  last round processed: 09:23:18.000
  [(..'ENTRY', 25050, 'CE'..), (..'EXIT', 25050, 'CE', 'END_OF_SESSION', -1235)]
  [('SESSION_ERROR', 'run_loop', "ValueError('one bad value')"), ('SHUTDOWN', None, 'session_end')]
  ```
  The day ends at 09:23. 1.0 behaves the same, so this is not a regression. But rule 9 is "no hiccups Monday", and 2.0 added a lot of new code inside the unprotected section (gate ladder, sounds, idle diagnostics, regime, feed-health dicts).
- A plausible trigger: a NaN `bid`/`ask` makes `fill` NaN, so `capital` becomes NaN. The next `compute_lots` then raises `int(NaN // x)` ValueError. Nothing in `feed.py:416` checks for finite values.
- Fix: wrap the post-loop section in try/except. On error, emit a latched `ANOMALY ROUND_ERROR` and still write the decision_tape row. Put `executor.manage` (l.769-803) in its own try, so a crash in display or entry code can never stop stop-losses and the 15:25 flatten. Restrict the outer `run()` except to feed or iterator errors.

### [MEDIUM] LEVELS_STALE is only set for one round, so the comment at l.656-657 is false: the engine can still enter on an 11-minute-old band, or on yesterday's seed (l.494-500, 661-668, 868-892) — reproduced
- `_levels_stale` is cleared on any round where a held band exists and no recompute is due (l.496-500). After a failed re-anchor, `mark_attempt` holds off the retry for 30 s. So the flag is true for exactly one round, the gate opens on the next round, and entries resume against the old band.
- **Cooldown-end re-anchor lost** (`scen_H.py`): an SL at 09:21:31 starts the 11-min cooldown. At 09:32:31, the round the cooldown ends, NIFTY basis fails for that one round (ATM PE missing). The re-anchor is refused with STALE_INPUTS and the cooldown edge is used up. Result: ENTRY at 09:32:35 against the 09:21:31 post-exit band, with no `post_cooldown` level row ever written:
  ```
  levels [('09:20:00','open',25050.0), ('09:21:31','post_exit',25044.02)]
  FEED_GAP [('09:32:31','basis','MISSING_ATM_QUOTE'), ('09:32:31','post_exit_band','STALE_INPUTS')]
  ENTRY 1792382555000 (=09:32:35) 25050 CE
  ```
  The FEED_GAP stage is also mislabelled: it says `post_exit_band` for a post-cooldown recompute (l.877).
- **Trading yesterday's seed** (`scen_G.py`): there is a prev-close seed, and every intraday re-anchor fails (ITM PE missing). Only one round (09:21:00) was blocked with LEVELS_STALE. 2.0 then bought 24900 PE at 09:21:13 off the seed lower 24905, exactly as 1.0 did. A failed interval re-anchor in `_process_instrument` also writes **no event at all** (l.423-426); only a tape-only flag flickers.
- Fix:
  - Make staleness sticky. Add `FreshnessEngine.is_stale` (`_last_success_ms is None or < _last_attempt_ms`) and gate on it for NIFTY.
  - On a cooldown-end recompute that fails, keep a `_pending_reanchor` flag and retry every round until it succeeds, rather than consuming the edge.
  - Emit an edge-latched `FEED_GAP stage=interval_band` event.
  - Accept the trade-off: a persistently missing ITM strike will then block entries, so it must be loud.

### [MEDIUM] The approach sound only ever plays when it is redundant or when no trade is possible — goes against rule 8 (orchestrator.py:678-683) — reproduced
- When `entry is not None and health_ok and flat`, the only gates left are LEVELS_STALE, EXPIRY_GATE and the two cooldowns. Two cases follow:
  - All gates open: the trade fires in the same round, so `approach` and `entry` play together (the entry sound skips the global gap).
  - A gate is closed: the cue says "approach" for a trade that cannot happen. The comment says "only when a trade is actually possible", which is false.
- `scen_A`: `('09:21:29','approach'), ('09:21:29','entry')` and again at 09:32:31. That is a double sound on every entry.
- `scen_B` (spot moving back and forth at the level during the 11-min stop cooldown): `('09:24:50','approach'), ('09:27:52','approach'), ('09:30:54','approach')`. All three decision rows say POST_STOP_COOLDOWN.
- Fix: compute `gate` first. Play `approach` only when `gate is None` and the entry did not fire this round. Or drop the approach cue entirely, since entry fires at the same 7-pt distance.

### [MEDIUM→LOW] FEED_UNHEALTHY's 30 s recovery window can delay a legitimate entry (orchestrator.py:701; feed_health.py) — reproduced (synthetic) / measured (tapes)
- `fh.py` over all three recorded days: the gate was **never closed before 15:15**. Episodes were only the known 15:15+ freeze or garbage:
  - 10-14: 15:15:45 SPOT_FROZEN through 15:27 IMPLAUSIBLE
  - 10-15: 15:16:16 FROZEN
  - 10-16: 15:15:33 FROZEN
- On those days it blocks nothing legitimate.
- In `scen_D`, a 60 s frozen spot followed by a resume delayed the entry from about 15:02:05 to 15:02:51 (RECOVERING). That is by design.
- Monday-specific risk (plausible): SPOT_JUMP fires on more than 40 pts between consecutive distinct spot values. A real burst on expiry day, or NIFTY coming back after a websocket reconnect, would then shut entries for 30 s right during the move the engine exists to catch. Consider not applying SPOT_JUMP when the option-implied future moved by about the same amount, i.e. checking `|Δspot − Δfuture|` instead of `|Δspot|`.

### [LOW] hold_while_in_trade departs from the "re-anchor every 20 min" rule (orchestrator.py:414-416)
- While a trade is open the interval re-anchor is skipped and nothing is recorded as an attempt. `need_recompute` stays True every round and the panel shows `next_reanchor_secs = 0`.
- It has no effect on trading: exits are priced off the premium, and a post-exit re-anchor always follows. It does change level_tape and dashboard behaviour against the operator rule. Either document it or turn it off.

### [LOW] Smaller issues
- **TRAIL_SL can close at a loss with no cooldown.** `scen_E`: a spike then a gap back gave TRAIL_SL pnl −1235 and a re-entry the next round. This matches the literal rules (cooldowns only after an SL loss or a profit), but the operator should know.
- **Exit round uses the pre-exit band for display.** On the exit round, `_nearest_dist` / `_compute_alarm` use the band from before the exit (l.805-815). Cosmetic.
- **APPROACH_ALERT dedup can drop a real entry's alert.** The dedup slot (l.324-331) is shared by APPROACH_ALERT, ENTRY_REJECTED and EXIT_REJECTED, keyed by direction, with a 60 s window. A second same-direction entry 15 s after a profit exit gets no APPROACH_ALERT in event_tape. Fix: always emit when the order is ENTRY.
- **REJECT rows written every round.** A persistent PREMIUM_TOO_THIN / INSUFFICIENT_CAPITAL still calls `enter()` every round, writing an order_tape REJECT row and growing `trades_log` without limit (about 4 rows/s). 1.0 does the same.
- **Stale comment.** l.99-101 says new entries stop at 15:10. The yaml sets `last_entry: "15:25"` and the code default is `flatten_at`, so behaviour is correct (rule 3), but the comment misleads.

## What 2.0 does better than 1.0 (evidence)
- **Manages the position even when NIFTY's band or basis fails** (l.771). `scen_D` part G: the ATM PE is missing from 15:24:50.
  - 2.0: `15:25:00 EXIT 25050 CE EXPIRY_FLATTEN`.
  - 1.0: still `POSITION_ACTIVE` at 15:25:10 with no flatten, because 1.0 needs `nifty_band is not None` before it calls manage.
- **Feed-sanity gate stops trading on the 15:15+ garbage feed.** Real-tape window 10-16 15:10-15:27 (`win.py`):
  - 1.0: `15:20:09 ENTRY 23100 CE` (frozen spot) and `15:24:47 ENTRY 23300 PE` on a band re-anchored on garbage. Its level rows jump to 23483/23228.
  - 2.0: 2223 rounds of FEED_UNHEALTHY, 0 orders, one `feed_alarm`, no garbage re-anchor.
- **Cooldown-end re-anchor runs before the entry check** (l.656-668), so the round where a cooldown ends evaluates against the fresh band (`scen_A`: `09:32:31 post_cooldown` level row, then ENTRY on it). The exception is the gap case in finding 2.
- **Buys the current ATM, not the anchor-time ATM.** `scen_A`: 2.0 bought 25050 CE with spot 25045; 1.0 bought 25000 CE.
- **Gate ladder with a "why no trade" reason on every decision row; all-in sizing preserved.** Every decision row carries the reason, and regime `capital_fraction` = 1.0 in both regimes (rule 1 is respected). No trade cap and no loss stop in the gate ladder.
- **Event hygiene.** INSTRUMENT_ERROR has its own latch. REVERSAL_WATCH and FALSE_BREAK have refractory windows. ENTRY_REJECTED is no longer emitted every round.
- **Code defaults match the yaml** (09:21 / 660 s / 15 s / 180 s). 1.0 fell back to 09:30 / 180 / 120 when a key was missing.

## What 2.0 does worse than 1.0 (evidence)
- **More new code in the unprotected post-loop section**, so the "one exception kills the day" exposure is larger (finding 1).
- **New double sound on every entry**, and approach cues during cooldowns (finding 3). 1.0 left sound to the browser, and the brief says that was noisy too, but this specific double sound is new.
- **New gate that only claims protection.** LEVELS_STALE adds a gate and a claim ("can never enter on the stale pre-cooldown band") that do not hold (finding 2). It does not block legitimate trades vs 1.0; it just gives no real protection.
- **Nothing I found blocks a legitimate trade that 1.0 would take, except the feed-health gate.** On the recorded days that gate only closed at 15:15 or later.

## Tests / checks you ran
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_entry_path.py tests/test_v2.py tests/test_execution_risk.py`: **57 passed**.
- Synthetic scenarios, each run on both 2.0 and 1.0:

| Scenario | What it covers | Result |
|---|---|---|
| A | entry, SL, 660 s cooldown, re-anchor, re-entry | OK on both; double sound in 2.0 |
| B | sounds during cooldown | 3 approach cues while POST_STOP_COOLDOWN |
| C | failed 09:40 interval re-anchor | LEVELS_STALE for 1 round only |
| D | frozen spot gate, and flatten with basis failing | 2.0 flattens; 1.0 does not |
| E | hold_while_in_trade, profit exit | TRAIL_SL at a loss with no cooldown |
| F | injected exception | session ends at 09:23 |
| G | failed re-anchor with a seed present | both trade off yesterday's seed |
| H | cooldown end on a gapped round | entry on the 11-min-old band |

- `fh.py`: FeedHealth over 1.0's NIFTY basis_tape for 10-14/15/16. Zero seconds closed before 15:15.
- `win.py`: windowed real-tape replay of 10-16 15:10-15:27, on 2.0 and 1.0. No full-day replay was used.
