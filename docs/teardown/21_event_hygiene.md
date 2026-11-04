# 21 — Event hygiene: teardown
Verdict: Mostly ready and clearly better than 1.0 (the REVERSAL_WATCH / FALSE_BREAK floods are gone, sounds are disciplined). But the new APPROACH_ALERT / ENTRY_REJECTED dedup is broken in exactly the case it was written for. That case is a persistent entry reject such as the expiry premium floor, which is likely on Monday afternoon, and it still floods 2 events per round. Fix this one before Monday.
Score (your area): 1.0 = 3/10, 2.0 = 6.5/10

## Baseline (1.0 recorded event_tape.jsonl) vs 2.0 replays

1.0 as recorded live (`/home/claude/vajra2/tapes/<d>/event_tape.jsonl`):

| day | total | REVERSAL_WATCH | FEED_GAP | ANOMALY (FALSE_BREAK) | APPROACH_ALERT | ENTRY/EXIT_REJECTED |
|---|---|---|---|---|---|---|
| 10-14 | 54 | 0 | 33 | 15 (1) | 1 | 0 |
| 10-15 | 1414 | **1351** (09:33–13:16, ~95–111 per 15 min) | 33 | 23 (**12 in 23 s**, 10:01:20–10:01:43) | 2 | 0 |
| 10-16 | 892 | **821** | 34 | 15 | 2 | 0 |

The 1.0 FEED_GAPs are almost all edge-latched sister `MISSING_ATM_QUOTE` (24/day) plus step resolution. Neither ENTRY_REJECTED nor EXIT_REJECTED ever fired in 1.0, because no reject happened.

2.0 windowed replays. I used a streaming windowed harness `/home/claude/teardown/scratch/21/winreplay.py`, which starts mid-session with no prev-close seed. So every window shows one artificial `SESSION_ERROR prev_close_seed` and a `REGIME` stamped with wall-clock time. Ignore both.

| window | 1.0 | 2.0 |
|---|---|---|
| 10-15 09:55–10:10 | **97 REVERSAL_WATCH** (1.0 code replayed on the same window), 0 trades | **1 REVERSAL_WATCH**, 1 APPROACH_ALERT, 1 entry + TRAIL_SL exit; sounds: approach 1, entry 1, exit_profit 1 |
| 10-16 11:30–11:50 | ~220 REVERSAL_WATCH recorded (109 + 119 per 15 min) | **0 REVERSAL_WATCH**; 9 FEED_GAP (7 of them AXISBANK MISSING_ATM_QUOTE in 41 s, a flicker); 0 sounds |
| 10-15 15:08–15:27 | APPROACH_ALERT at 15:20:11 on the garbage tick (dist −82), recorded | 1 FEED_GAP feed_sanity SPOT_FROZEN at 15:16:16, then nothing more; gate FEED_UNHEALTHY to 15:25; 1 feed_alarm sound |
| 10-16 15:08–15:27 | 2 APPROACH_ALERT recorded (15:20:09 dist −37.55 on the garbage tick, and 15:24:47) | 1 FEED_GAP feed_sanity SPOT_FROZEN, no later escalation or recovery event; 9 sister MISSING_ATM_QUOTE; 1 feed_alarm; 0 trades |
| 10-14 15:08–15:27 | APPROACH_ALERT 15:20:03 + FALSE_BREAK, recorded | 4 FEED_GAP feed_sanity + 3 FEED_HEALTH RECOVERED (flapping); feed_alarm: 2 played, 2 suppressed |
| 10-15 09:58–10:04 with `execution.min_premium_nonexpiry=500` (forced persistent reject) | n/a (1.0 also emits every round, see F1) | **684 APPROACH_ALERT + 684 ENTRY_REJECTED + 684 order_tape REJECT rows in 6 min**; the 40-slot cockpit strip was full |

I also tried the single full-day replay (10-15), but the shared box OOM-killed it at about 09:48. That was box memory pressure, not the engine. Up to the kill, 2.0 had emitted 1 REVERSAL_WATCH where 1.0 recorded 41 for the same span.

## Findings (most severe first)

### [HIGH] APPROACH_ALERT / ENTRY_REJECTED "on change or once a minute" dedup is defeated by its own shared key (orchestrator.py:324-331, 723, 755) — reproduced
- `_emit_alert_event` keeps **one** `_last_alert_key` for all three types. On a rejected entry, each round emits `APPROACH_ALERT(UP)` then `ENTRY_REJECTED(PREMIUM_TOO_THIN)`. Each call sees a key different from the previous call, so both events fire every round. The docstring and CHANGELOG ("fire on change or once a minute", explicitly citing the premium floor) are false for this case.
- Command:
  `python3 -B winreplay.py /home/claude/teardown/vajra2 /home/claude/vajra2/tapes/2026-10-15 09:58 10:04 out execution.min_premium_nonexpiry=500`
  Output: `684 APPROACH_ALERT / 684 ENTRY_REJECTED / orders {('REJECT','PREMIUM_TOO_THIN'): 684} / strip 40`. The events alternate 10:00:17, 10:00:18, 10:00:19 ×3, and so on.
- The cockpit strip's run-length collapse (orchestrator.py:316) needs *consecutive identical* events. Strict alternation defeats it, so the 40 strip slots fill with A/R pairs and push any FEED_GAP off the strip. Each event also does its own `fh.flush()` (tape.py:162).
- Monday scenario: on expiry the floor is `min_premium_expiry: 30`. After about 14:00 on a DTE-0 day the ATM premium regularly sits near or below 30. Every approach within 7 pts of a level is then rejected every 250 ms round for as long as spot lingers there, which can be thousands of events and REJECT rows per episode. The same pattern applies to NO_QUOTE rejects. There is no sound (good), but the event feed and strip become useless right when the operator is mirroring trades.
- This is no worse than 1.0, which emitted both every round (1.0 orchestrator.py:586, 614). But 2.0 claims a fix it does not deliver, and no test exercises it: `test_reversal_watch_needs_real_bias_and_is_rate_limited` only asserts the config values.
- Fix: keep the state per event type, `self._alert_state[event_type] = (key, ms)`, and compare against that type's own last key and time. Use key `(direction, round(level))` for APPROACH_ALERT and `(reason, direction, round(level))` for ENTRY_REJECTED. Consider latching the order_tape REJECT row the same way, or carrying a `repeat_count` instead. Add a test that runs 8 reject rounds and expects ≤ 2 events.

### [MEDIUM] A real second entry's APPROACH_ALERT is suppressed if it comes within 60 s of the previous one in the same direction (orchestrator.py:328) — reproduced (unit probe)
- With the 15 s post-profit cooldown, a TRAIL_SL exit followed by a re-entry in the same direction 16–59 s after the first alert is normal trend-following. The second alert is dropped.
- Probe: `_emit_alert_event('APPROACH_ALERT','UP',…,0); …(36_000)` → only `[(0,'APPROACH_ALERT')]` emitted.
- The trade is still in order_tape and signal_tape, and the entry sound still plays. But the event tape and strip lose the "scream" for that trade: the level, the confluence verdict, and the strike to buy.
- Fix: never dedup an APPROACH_ALERT whose entry actually filled (ENTRY). Dedup only repeated *rejected* attempts, or key on the level as in the fix above.

### [MEDIUM] Live-feed `FEED_GAP stage=build_snapshot` (and `step_maintenance`) is not latched, so it floods at 4/sec per bad instrument (feed.py:444-456) — reproduced
- `python3 -B probe_feed.py /home/claude/teardown/vajra2`: a stub `_build_snapshot` raising for ITC gave `rounds 40 events {('FEED_GAP','build_snapshot','ITC'): 40}`. One persistently malformed instrument therefore produces about 14,400 events/hour, each flushed.
- With two failing instruments the summaries alternate, so the strip collapse fails and the 40 slots fill. This is unchanged from 1.0 (1.0 feed.py:405-407).
- Fix: use a per-(instrument, stage) latch like `_basis_gap` and clear it on the first good build. Do the same for `step_maintenance`.

### [MEDIUM] Feed-sanity escalation inside one fault episode is never reported (orchestrator.py:372-382, feed_health.py:86-91) — reproduced
- On 10-15 the only event is `15:16:16 FEED_GAP feed_sanity SPOT_FROZEN`. At 15:20:11 the feed resumed with garbage: spot 22939.95 → 23138.85 → 23219.45, basis −204 → −283. That is a much more dangerous condition, but `started` only fires when `prev is None`. The tape and strip still say "SPOT_FROZEN", and no FEED_HEALTH RECOVERED ever arrives before the 15:25 cutoff.
- The live idle widget (`feed_health.reason`, dashboard.py:754-758) does show the current reason, so the operator is not blind live. The post-hoc audit trail, however, is wrong.
- Fix: emit `FEED_GAP feed_sanity` again when the raw fault reason *changes* to a different non-RECOVERING reason (no sound needed). Optionally emit a 60 s "still unhealthy" heartbeat.

### [LOW] FEED_GAP / FEED_HEALTH flaps at the 45-pt basis threshold (feed_health.py:76-91) — reproduced
- On 10-14 between 15:20:38 and 15:22:43 there were 3 RECOVERED / IMPLAUSIBLE cycles at basis −50.3, −45.75 and −45.75. The 300 s sound cooldown held it to 2 alarms (2 suppressed), so this is not a noise problem, just strip churn.
- Fix: add hysteresis, for example re-arm only after |basis| < 40.

### [LOW] Silent stale / skip states (orchestrator.py:405-424, 493-495) — read in code
- (a) An interval NIFTY re-anchor whose `_build_band` fails sets `_levels_stale=True`, which blocks entries via the LEVELS_STALE gate, and emits **no event**. The other failure paths do emit `FEED_GAP post_exit_band`. The dashboard badge and decision_tape show it; event_tape does not.
- (b) A re-anchor skipped because of feed health, or held while in a trade (`hold_while_in_trade`), also leaves no trace.
- Fix: edge-latched `FEED_GAP stage=interval_band reason=<band.reason>`, plus one `ANOMALY kind=REANCHOR_DEFERRED` per skip episode.

### [LOW] Refractory windows swallow a whole episode, not just its start (orchestrator.py:629-638, 443-452)
- If a REVERSAL_WATCH (300 s) or FALSE_BREAK (60 s) episode *starts* inside the refractory window, the latch is set to True and nothing is emitted, even after the window expires while the episode continues.
- REVERSAL_WATCH is advisory only: confluence never gates entry. So nothing the operator needs is lost, and I consider the 300 s / |bias| ≥ 0.125 tuning acceptable. Optionally emit when the refractory expires if the episode is still on.

### [LOW] Edge latches with no recovery events
- `round_missing_nifty`, basis `MISSING_ATM_QUOTE`, INSTRUMENT_ERROR and `manage_quote` gaps have no matching recovery event. Only NIFTY feed sanity gets FEED_HEALTH.
- Flicker re-fires on every blink: AXISBANK fired 7 times in 41 s on 10-16. That is acceptable volume, but the strip cannot tell an operator whether a gap is over.
- The dashboard "feed crit" flash (dashboard.py:865) only lasts 60 s after the gap *event*, so a persisting `manage_quote` gap during an open trade stops flashing after a minute.
- Fix: emit a `FEED_HEALTH status=RECOVERED stage=<x>` on each latch clear, and drive the flash from the latch state rather than event age.

### [LOW] INSTRUMENT_ERROR latch still re-fires on an intermittent error (orchestrator.py:574-581)
- The persistent-error flood from 1.0 is fixed: there is now an own latch, `_err_on`. But it pops on every good round, so an error every other round still emits about 2/sec.
- Fix: use a minimum interval per instrument, for example 60 s.

### [LOW] `RISK_STANDDOWN` is declared but never emitted (schema.py:244)
- This is correct given operator rule 1 (no caps; regime.py:62 sets max_trades=0, daily_loss_stop_pct=0.0). It is dead vocabulary only.

## What 2.0 does better than 1.0 (evidence)
- REVERSAL_WATCH: 97 → 1 on an identical 15-min replay window (10-15 09:55–10:10), and about 220 → 0 on 10-16 11:30–11:50. The cap is now at most 75 per day by construction (300 s refractory).
- FALSE_BREAK: an edge-triggered 60 s refractory replaces 1.0's 12 events in 23 s.
- INSTRUMENT_ERROR: a persistent post-basis error no longer floods, because it has its own `_err_on` latch.
- Feed sanity: the 15:15–15:22 freeze and garbage period is now a FEED_GAP plus one alarm, and the engine holds (FEED_UNHEALTHY). 1.0 instead raised an APPROACH_ALERT on the garbage tick on all three days and entered on it.
- Sounds: engine-decided, at most 5 kinds, with per-kind cooldowns and a 3 s global gap. Sister breaches are silent, and trade cues are never dropped. Measured in the windows: 3 sounds for a full trade, 1–2 feed alarms for the closing-feed fault, 0 in a quiet 20 min. This meets operator rule 8.
- Undeclared event types are rerouted to ANOMALY instead of raising mid-session (tape.py:147-152), and a test enforces the declarations.

## What 2.0 does worse than 1.0 (evidence)
- APPROACH_ALERT for a legitimate quick re-entry can now be suppressed (60 s same-key window). 1.0 always emitted it.
- The new dedup claim is false for its headline case (F1). Behaviour there equals 1.0's flood, not an improvement, and the CHANGELOG says otherwise.

## Tests / checks you ran
- Baseline counts of 1.0 event_type per day, with sub-kinds and time buckets (python over `tapes/*/event_tape.jsonl`).
- `scratch/21/winreplay.py` (streaming, row-group-pruned window replay), run for 2.0 on 5 windows and for 1.0 on 1 matching window (output above).
- Forced-reject replay (`execution.min_premium_nonexpiry=500`) → 684/684 flood.
- `scratch/21/probe_feed.py` → 40/40 unlatched build_snapshot FEED_GAPs over 40 rounds.
- A unit probe of `_emit_alert_event` (suppression of a re-entry, defeat on reject).
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_v2.py tests/test_entry_path.py -k "event or reversal or sound or alert or strip"` → 6 passed. None of these tests exercise alert dedup behaviour.
- The full-day 10-15 replay was OOM-killed by the shared box at about 09:48 (up to then: 1 REVERSAL_WATCH vs 1.0's 41).
