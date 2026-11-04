# 30 — Chief skeptic: teardown
Verdict: I tried to prove that 1.0 should keep running on Monday and could not. **Switch to 2.0 on Mon 19 Oct, with the named conditions below.** On an expiry day, 2.0 is better than 1.0: every expiry-day change either lowers risk or makes no difference, except one deliberate strategy change (the strike is now the current ATM), which is unproven on a real expiry. Confidence about 70%.
Score (whole engine, judged for Monday's expiry-day run): 1.0 = 5/10, 2.0 = 7/10

## The question I tried to answer
The CHANGELOG says "Expiry day is unchanged". That is **only true of the NIFTY band formula.** On 19 Oct, these 2.0 changes are all active:
strike selection, the feed-sanity gate, managing the position every round, the cooldown re-anchor ordering, hold-while-in-trade,
sounds, the SSE dashboard, the tape writer thread and gc.freeze. The MTM non-expiry band does **not** apply on Monday, and I proved that
in code: `resolve_regime` gives `EXPIRY, dte 0, legacy band` for 2026-10-19 (Tue 20 Oct is a holiday), and `_build_band`
(orchestrator.py ~258) routes the expiry day to the unchanged `compute_band`. `pad_floor` is 0.10 in the yaml, so removing the clamp is a no-op here.

## The decisive experiment: one full-day replay of each engine on a pseudo-expiry day
* **Day:** 15 Oct, the only tape covering a full session (09:41:55–15:29:59, 1.91 M rows). It also contains the 15:15 cash-feed freeze and the 15:20 garbage prints.
* **Same input, same config:** both engines read `/home/claude/teardown/vajra2/vajra_config.yaml` and the same streamed tick tape. I forced an
  `ExpiryResolution(is_expiry_day=True, basis_mode=IMPLIED)`, which is what the real calendar returns for Mon 19 Oct. As a result both
  engines take Monday's code path: the legacy band, the expiry premium floor of 30, the ITM walk and the EXPIRY regime. This isolates exactly the
  non-band differences that will be live on Monday. Caveat: the premiums are T-2 premiums (ATM about 134), not expiry-day premiums.
* **Command:** `python3 -B scratch/30/run_replay.py <engine_root> tapes/2026-10-15 scratch/30/out_vN vajra_config.yaml expiry`.
  The driver streams the parquet, because 1.0's own `replay.py` loads the whole day into RAM. It drops only the tick and basis output tapes, to save disk.
* **Fidelity check:** the 1.0 replay reproduces 1.0's *live* order_tape for 15 Oct exactly, to the rupee (same entries, fills, quantities and P&L).

| | 1.0 | 2.0 |
|---|---|---|
| T1 entry 10:00:14 (spot 22855.2, UPPER 22862.1, dist 6.9) | **22750 CE** ×455 @196.00 | **22850 CE** ×715 @134.40 |
| T1 exit | 10:08:19 TRAIL_SL @209.70 (+7.0%) **+6,233** | 10:08:15 TRAIL_SL @149.25 (+11.0%) **+10,618** |
| T2 entry 15:20:11 (spot **23138.85**, i.e. 82 pts *through* UPPER 23056.4) | 22950 CE ×975 @106.00 | **blocked: FEED_UNHEALTHY** (gate tripped 15:16:16, SPOT_FROZEN 30 s) |
| T2 exit | 15:25:00 EXPIRY_FLATTEN @109.10 **+3,022** | — |
| End capital | 1,09,256 | **1,10,618** |
| Blocked-entry rounds | — | FEED_UNHEALTHY 1,881; POST_PROFIT_COOLDOWN 59; AFTER_LAST_ENTRY 1,123 (after 15:25) |
| NIFTY anchors | 21 (including the 10:01:56 re-anchor *during* T1) | 18 (no mid-trade re-anchor; later bands agree with 1.0 within ±2.5 pts) |
| REVERSAL_WATCH events | 1,351 | 3 |

**Why each trade differs:**
* **T1:** both engines took the same signal and the same level. The only difference is the strike. 1.0 bought the anchor-time ATM (22750,
  which was 105 pts ITM at entry). 2.0 bought the current ATM (22850). The ATM option moved more in percentage terms, so 2.0 got a +11% peak and exit where
  1.0 exited on the +7% floor. 2.0 also held 1.57× the lots. That is a +4,385 difference from **one** sample.
* **T2:** this was the **garbage feed**, not a breakout. NIFTY spot sat frozen from 15:15:46 for 265 s, then printed +199 / +81 / +180-pt
  one-tick jumps from 15:20:11 while the option-implied future barely moved (my scan: `scratch/30/feedstats.py`). 1.0 bought all-in
  on a "breach" that never happened and was lucky to make +3,022. On a real expiry, at 15:20, all-in in an option about to expire on a fake breach is a coin
  flip with your whole capital. 2.0 refused the trade. **This is the strongest single reason not to run 1.0 on Monday.** It happened on all three recorded days
  (1.0's live tapes show 15:20 entries on 14, 15 and 16 Oct).

I did **not** replay a non-expiry day for a full session. I ran one 2.0 window, 15 Oct 09:41–10:45 in natural NON_EXPIRY mode (MTM band): 4 entries, all TRAIL_SL
winners, capital 1,00,000 → 1,48,916. That window is a trending morning and the thresholds were tuned on these same tapes, so it is
in-sample evidence that 2.0 trades off-expiry, not evidence that it has an edge (see the MEDIUM finding).

## Each behavioural difference weighed for Monday (risk vs benefit)
| Change | Monday risk | Monday benefit | Net |
|---|---|---|---|
| Feed-sanity gate (feed_health.py; orch 372, 407-416, 701) | False trip blocks entries for at least 60 s. Measured pre-15:14 headroom on all 3 tapes: longest unchanged spot 16.3 s (threshold 30), largest jump 22.25 (threshold 40), largest abs basis 29.8 (threshold 45). On expiry the implied basis is close to 0, so headroom is larger | Blocks the 15:15–15:25 garbage entries and re-anchors (reproduced) | **Clear win** |
| Manage every round (orch 769-) | None found. manage() still refuses a missing or NaN quote | Stops and the 15:25 flatten are no longer skipped on rounds where the band or basis failed | Win (rare) |
| Cooldown-end re-anchor before entry (orch 656-) | None | 1.0 could enter on the stale pre-cooldown band during the round the cooldown ended | Small win |
| hold_while_in_trade (orch 414) | None. Exits are premium-based and the band is re-anchored post-exit anyway | Cosmetic. Reproduced zero trade impact | Neutral |
| Current-ATM strike (orch 722/739, execution.py 187-189) | **Strategy change on expiry, never run on a real expiry tape.** ATM on expiry is about 2× the % volatility of a 1-ITM strike, so the −12% stop is hit by a smaller adverse move. It also leans more on the spot being plausible (see LOW-2) | Matches the meaning of "ATM". +4,385 on the one sample. On a real expiry the band sits only about 10–40 pts from spot, so anchor ATM and current ATM are often the *same* strike | Probably positive, unproven |
| Sounds (alerts.py) | Approach cue duplicates the entry cue; rings during cooldowns (LOW-1) | Sister-breach noise removed; trade cues never dropped | Win |
| SSE dashboard | New code path. I checked it: 41 pushes in 10 s, hot payload about 2.4 KB, full about 18.8 KB, /api/state 200. Polling fallback exists | Ticks are no longer late | Win |
| Tape writer thread (tape.py 126/170/178) | Parquet rows only reach disk at flush. `close()` joins with a 30 s timeout, but `flush()` already did `q.join()`, so it is safe | Fewer engine stalls | Neutral-to-win |
| gc.freeze (app.py 161) | Live-only. Neither I nor the replays exercised it. A higher gen0 threshold makes collections rarer, not unsafe | Fewer GC pauses | Neutral (unverified) |

## Findings (most severe first)
### [MEDIUM] "Expiry day is unchanged" is misleading: the strike rule changed on expiry too (orchestrator.py:722,739; execution.py:187) — reproduced
- Scenario / evidence: the pseudo-expiry replay above. Same signal, different contract (22850 CE vs 22750 CE) and different quantity (715 vs 455). The operator
  mirrors trades by hand and will see a different strike from what 1.0 habits suggest.
- Fix: no code change needed for Monday, but put one line in the OPERATOR_MANUAL: "2.0 buys the ATM at the moment of entry". Optionally add
  `execution.strike_from: current|anchor` so the old behaviour can be restored without a code edit.

### [MEDIUM] No shipped evidence that 2.0 makes money; documentation claims are unverifiable (CHANGELOG.md "Result on the recorded days") — read in code
- Evidence: that section is an empty placeholder. `docs/REVIEW_DOSSIER.md` and `docs/review/` do not exist in the snapshot. The CHANGELOG
  claims 102 tests, but `pytest tests` gives **101 passed** (235 s). The MTM non-expiry band was tuned on the same three tapes it is "validated" on,
  and every recorded day is a trending session.
- Fix: before Wed 21 Oct (the first non-expiry day 2.0 will trade), regenerate the per-day comparison and state it honestly. Treat non-expiry P&L as unproven.

### [LOW] Approach sound is redundant with the entry sound and rings when no trade is possible (orchestrator.py:678-686) — read in code
- Every tradeable approach fires an entry in the *same round*, so each trade produces "approach + entry". During the 11-min post-stop cooldown
  the approach cue still rings (it checks only `health_ok` and `not position_active`, not the cooldown gates). That breaks operator rule 8.
- Fix: move the cue after the gate block. Skip it when `fire` is true (the entry cue already rings) and when the gate is
  POST_STOP_COOLDOWN or POST_PROFIT_COOLDOWN.

### [LOW] After a feed fault clears, entries can use the pre-fault band for up to about 30 s (orchestrator.py:407-412, freshness mark_attempt) — read in code
- While the feed is unhealthy, each round calls `mark_attempt`, so the next re-anchor attempt is about 30 s after the *last* bad round. The gate re-opens after 30 s of
  clean rounds, and the valid held band clears `_levels_stale`. That leaves a short window to enter on a band anchored before the fault.
  None of the recorded faults happened intraday; they all started at 15:15.
- Fix: on `hs.ended`, force an immediate NIFTY re-anchor (call `_recompute_nifty_band(..., trigger="post_cooldown")`) before the entry gates run.

### [LOW] Current-ATM sizing now depends on the cash spot being right (orchestrator.py:722) — read in code
- `cur_atm` comes from the snapshot spot. A wrong spot that still passes the gate (error under 45 pts) can shift the strike by one step. A garbage spot
  that slipped past the gate would buy a far-OTM option all-in. The gate is now load-bearing for strike choice, not just entry timing.
- Fix: consider deriving the ATM from the implied future (`br.future`) rather than spot. Otherwise keep the gate on (`feed_sanity.enabled: on`).

### [LOW] `_emit_alert_event` can hide a real APPROACH_ALERT (orchestrator.py:324) — read in code
- The same (type, direction) within 60 s is suppressed. Two UP entries 15 s apart (the profit cooldown) leave the second entry without its APPROACH_ALERT row.
  Only order_tape and signal_tape stay complete. This is audit-only.
- Fix: never throttle APPROACH_ALERT on a round that actually fired.

### [LOW] Token precedence trap (config.py ~140) — read in code
- `vajra/.token` overrides `credentials.py`. If an old `.token` file exists and the operator edits credentials.py out of 1.0 habit, login fails at 09:15.
- Fix: launch with `-Token <today>` and delete any `vajra/.token`.

## What 2.0 does better than 1.0 (evidence)
- It refuses the 15:20 garbage-feed trade that 1.0 takes every day (reproduced on 15 Oct; the 1.0 live tapes show the same on 14 and 16 Oct).
- In the one comparable trade on the expiry path, the better strike gave +10,618 vs +6,233 (reproduced).
- Stops and the flatten are managed every round. The cooldown re-anchor comes before the entry check. Event hygiene: REVERSAL_WATCH 1,351 → 3.
- Replay works on an 8 GB box. 1.0's `replay.py` loads the full day into RAM, so I had to use my own streaming driver for 1.0.
- `--date` is refused in live mode, and replay never overwrites the recorded tapes.

## What 2.0 does worse than 1.0 (evidence)
- More new runtime machinery (SSE, tape thread, gc.freeze) that has never run a live session on Windows. My SSE check passed. gc.freeze is unverified.
- Small sound regressions against rule 8 (LOW-1). The audit can drop an APPROACH_ALERT (LOW).
- Strategy claims (non-expiry) come without shipped evidence.

## Conditions for switching on Monday
1. On the Windows box, Sunday or before 09:00: run `.\run_vajra.ps1 -Verify` (it must PASS), then a 2-minute `-Mode replay -Date 2026-10-15`. Check that the banner
   shows `regime EXPIRY` for a live start on Monday (`expiry 2026-10-19`, `T-0, legacy band`). If the banner says NON_EXPIRY, stop and use 1.0.
2. Keep the 1.0 folder untouched as a fallback. If 2.0 fails to start by 09:18, or throws in the first 10 minutes, run 1.0 and **do not mirror any 1.0
   entry after 15:15** (the garbage-feed window).
3. Start with `-Token <today>` and no `vajra\.token` file. Click the page once so audio unlocks (the pill must not show 🔒).
4. Mirror the strike the dashboard shows (current ATM), not the strike 1.0 habits suggest.
5. Expect FEED_UNHEALTHY from about 15:16 and no entries after that. That is the designed behaviour.

## Tests / checks you ran
- `python3 -B -m pytest -p no:cacheprovider -q -x tests` (2.0) → `101 passed, 1 warning in 235.61s`.
- Full-day pseudo-expiry replays of both engines on 15 Oct (each about 4,740 s wall time on the shared box): results above. Output is in `scratch/30/cmp_fullday_pseudo_expiry.txt`.
- 2.0 natural NON_EXPIRY window, 15 Oct 09:41–10:45: 4 trades, +48.9% (in-sample, trending morning).
- Feed-health headroom scan of all 3 tapes (`scratch/30/feedstats.py`): no gate trip before 15:15 on any day. Faults began at 15:15:02 / 15:15:15 / 15:15:46.
- Regime check: 16 Oct → NON_EXPIRY T-1 mtm. **19 Oct → EXPIRY T-0 legacy**. 21 Oct → NON_EXPIRY T-4 (monthly, ACTUAL basis).
- SSE smoke test (`scratch/30/sse_check.py`, port 8930): 31 hot + 10 full events in 10 s, `/api/state` returned 200 at 18.8 KB.
- Code diff 1.0 → 2.0 for orchestrator, execution, trigger_engine, tape, app, config, feed and schema (CRLF-normalised).
