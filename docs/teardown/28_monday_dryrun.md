# 28 — Monday 19 Oct 2026 expiry-day dry run: teardown
Verdict: **Ready to run Monday, with no crash, exception or tape problem in a full 09:10→15:30 rehearsal. Better than 1.0** (it no longer trades or re-anchors on the 15:16+ garbage feed, and it buys the current ATM). But the **dashboard tells the operator things that are wrong** at three moments that matter to someone mirroring by hand: pre-open, after an exit, and the 15:20–15:30 garbage tail.
Score (your area): 1.0 = 5/10, 2.0 = 7/10

## How the rehearsal was built (so findings can be weighed)
* **Session resolved** with the real `build_calendar`/`resolve_regime`: `2026-10-19 Mon → expiry 2026-10-19 IMPLIED, is_expiry_day=True, shifted=True (Tue 20 holiday), near-month fut 2026-10-27, regime EXPIRY T-0 legacy band`. For comparison, Tue 20 is CLOSED and Wed 21 resolves to the 27-Oct expiry with ACTUAL basis. `tests/test_expiry.py`, `tests/test_timing.py`: 8 passed.
* **Market:** `scratch/28/t28_market.py`. It starts from the **real 15-Oct tick_tape**, sampled every 2 s (10,443 rounds, all 24 instruments; streamed by `t28_prepass.py`).
  * Timeline: real 09:42–15:30 is moved onto Monday. Real 09:42–10:14 is replayed as 09:10–09:42, offset so NIFTY joins up continuously.
  * NIFTY spot is the *recorded* spot. That keeps the real Breeze cash freeze at 15:16 and the +100-pt garbage tail.
  * The NIFTY chain is **re-priced for expiry day**: Black-76 on the recorded option-implied forward, IV taken from the recorded ATM straddle, trading time to Monday 15:30. Basis converges to 0.
  * Session VWAP per strike is the time-weighted average since 09:15. Sisters and stocks are the unchanged recorded rows.
  * Resulting ATM premiums: 09:20 ≈ 74/76; 12:00 ≈ 49/78; 14:30 ≈ 32/35; 15:00 ≈ 27/19; 15:20 ≈ 9/18; after 15:24 ≈ 0.05 / intrinsic. Time value is about 0 into the close.
* **Full app path:** `scratch/28/t28_run.py` calls the real `vajra.app.run(mode="live")`. Only these are faked: `datetime.now` (set to Mon 09:10 IST), `_connect_breeze` (a fake with `disconnect()`), and `_build_source`.
  * The source is my market plus the **real** `LiveBreezeSource.prev_close_snapshot()`, reading the real 16-Oct parquet.
  * Everything else is real: banner, gc.freeze, heartbeat, Orchestrator, Executor, real TapeWriter into a temp dir, uvicorn dashboard on **:8928**.
* **Browser:** headless Chromium via Playwright (`t28_browser.py`), timezone Asia/Kolkata, consuming `/api/stream` over SSE. Sound mode was set to "all cues" and unlocked by a click. `_playNow` was hooked to log every cue the page actually plays.
* **Runs:**
  1. One full day, 09:10–15:30, at about 14× real time (1,477 s wall).
  2. A browser window 09:10–09:24.
  3. A browser window 14:58–15:30 with `entry.proximity_pts=80`. This is contrived: it forces the afternoon entries, ITM fallback and floor-30 paths that the real price path did not reach.
  4. A direct-Orchestrator 15:17–15:30 run with the exits disabled, to prove the 15:25 flatten.

## Checklist results
| Item | Result |
|---|---|
| Startup banner | ✅ `⚡ VAJRA 2.0 LIVE \| 2026-10-19 \| expiry 2026-10-19 IMPLIED \| regime EXPIRY (T-0, legacy band) \| last entry 15:25 \| cadence 0.25s \| LIVE_ORDERS=False (paper) \| http://127.0.0.1:8928`. EXPIRY_RESOLVED and HOLIDAY_DECISION are on the tape. The header shows `⚡ EXPIRY DAY`, `expiry 2026-10-19 IMPLIED`. |
| Pre-open state | ⚠️ The seed was built (`live_tape_parquet:2026-10-16`, took 20.9 s), but **nothing is shown**: spot blank, "waiting for levels", date/expiry "—" (see F2). |
| 09:20 first calc | ✅ 09:20:00 legacy band: `K_CE 22600 VWAP 97.52 → U 22716.5; K_PE 22700 VWAP 108.42 → L 22591.5; basis −11.5`. Hand check: 22600+round(107.27→105)+11.5 = 22716.5 ✓; 22700−round(119.26→120)+11.5 = 22591.5 ✓. Labelled `interval`, not `open` (F7). |
| 09:21 first possible entry | ✅ decision_tape shows BEFORE_TRADE_START ×30 (09:20:00–09:20:58), then NO_APPROACH from 09:21:00. First real approach and entry: 09:27:54, 22700 CE @ 80.05, 19 lots = floor(100000/(80.05×65)) ✓. |
| Expiry-day legacy band | ✅ `band_method=legacy, regime=EXPIRY` on every NIFTY level row. 53 NIFTY re-anchors (interval / post_exit / post_cooldown). |
| ITM fallback | ✅ (window 3) 14:58 ATM 22950 CE ≈ 27 < 30, so it bought **22900 CE @ 56.65** (1-ITM). 15:14: **23000 PE** for ATM 22950. |
| Premium floor 30 | ✅ 15:02:16: ATM 22950 CE @ 31.45 (≥ 30) taken at ATM. No PREMIUM_TOO_THIN in any run, because the 6-strike ITM walk always found ≥ 30. |
| Exits | ✅ Full day: 21 round trips; 16 TRAIL_SL, 5 SL_HIT, each followed by a 660 s cooldown. Re-anchor after every exit and after the 15 s profit pause. Capital 1,00,000 → 1,80,441 (synthetic, not evidence of edge). |
| 15:25 flatten | ✅ `15:17 ENTRY 22900 CE` → `15:25:00 EXIT EXPIRY_FLATTEN @ 45.95` (bid). Gate shows AFTER_LAST_ENTRY from 15:25:00. |
| 15:30 stop | ✅ Last round 15:29:58; SHUTDOWN; finalize; fake breeze disconnect; browser shows `SESSION CLOSED — last round 3:29:58 PM · tapes closed`. |
| Tapes closed / readable | ✅ All 6 parquet files open; parquet rows = jsonl rows (tick 266,360 / level 266,403 / basis 266,360 / decision 11,100 / order 42 / signal 21); event_tape 76 rows. |
| Exceptions | ✅ 0 `Traceback` / logged errors in 3 runs; 0 INSTRUMENT_ERROR / SESSION_ERROR events. |
| Sounds only for real events | ✅ mostly. Full day: 22 approach, 21 entry, 16 exit_profit, 5 exit_loss, 1 feed_alarm (15:16:20 SPOT_FROZEN). 0 sister/stock cues. In window 3 the browser played exactly the 16 cues the engine queued, in order. 2 avoidable noise patterns (F4). |
| SSE / tick delay | ✅ Lag pill 1–180 ms under load average 20–40; no false DATA FROZEN during the session. |

## Findings (most severe first)

### [HIGH] Banner and BUY box say "BUY NIFTY 23050 CE" while entries are blocked (feed unhealthy, cooldown, after 15:25) (`vajra/orchestrator.py:1127-1193` `_compute_alarm`) — reproduced
- Scenario: the real 15:20 Breeze cash garbage tail puts spot 45 pts above the held upper level. The engine is correctly gated (FEED_UNHEALTHY, then AFTER_LAST_ENTRY). But `_compute_alarm` ignores every gate, so the cockpit shows a red 🚨 `BREACHED UPPER 22995 by 44.5 pts -> BUY NIFTY 23050 CE`, a green **BUY NIFTY 23050 CE** box, and an ATM CE quoted at **0.05**.
- Evidence: screenshot `scratch/28/work/shot_1524.png`, taken with `FEED SPOT_IMPLAUSIBLE` and POST-STOP COOLDOWN shown. Browser samples 15:25–15:29 read `BREACHED UPPER 22995 by 31.3 pts -> BUY NIFTY 23050 CE` next to the strip `past the entry window (flatten time)`.
- Not an artefact of the contrived proximity: with the real `proximity_pts=7`, the full-day run has spot 23015 > U 22991.85 at 15:25, so the same text appears.
- Impact: an operator who mirrors by hand is told to buy a worthless call into the expiry close. 1.0 also showed this banner, and 1.0 actually traded it.
- Fix: in `_compute_alarm`, when `self._entry_gate` is any of {FEED_UNHEALTHY, AFTER_LAST_ENTRY, BEFORE_TRADE_START, POST_*_COOLDOWN, LEVELS_STALE}, set `target=None`, drop the "-> BUY" text, and show the gate text instead. Have the dashboard hide `#nbuy` whenever `arming.target` is null.

### [MEDIUM] Pre-open screen is blank although the seed exists; the RUN_LIVE checklist tells the operator to treat a blank spot as a stale token (`orchestrator.py:531-532`, `844-866`; `RUN_LIVE.md` §3) — reproduced
- `process_round` returns before 09:20 without updating `latest_state`. The prev-close seed is written to `_level_history` but never published.
- From 09:10 to 09:20 the page shows: spot "—", "waiting for levels", "levels not yet calculated", `date —`, `expiry —`, and banner `PRE-OPEN — engine live · first calc 09:20` with **no** "levels seeded" suffix.
- The terminal heartbeat prints `NIFTY None · None` for 10 minutes. See `shot_0912.png`.
- RUN_LIVE says: "NIFTY headline shows a real spot … within ~10s. If spot stays blank → the token is stale (redo step 1)", and "you'll see levels before 09:20".
- On Monday the operator will see exactly the "stale token" symptom and may regenerate the token and restart.
- Same as 1.0. Fix: publish a pre-open `latest_state` (spot, expiry, regime, seed levels, level_history) from `process_round` before the 09:20 early return. At minimum, correct RUN_LIVE ("spot appears at 09:20").

### [MEDIUM] After the first exit the position panel shows the closed trade as live ("0/1625 partial", "HARD-STOP NEAR", "deployed Rs 1,01,725") (`dashboard.py:773, 1017`; `orchestrator.py:1360`) — reproduced
- `latest_state.position` stays non-null once any trade has happened (the `Executor.position` object is kept with `qty_remaining=0`). The page tests `!p`, so the "flat · last exit" branch is never reached after trade #1.
- See `shot_1524.png`: flat for 9 minutes, yet the panel reads `DOWN PE 23000 … 0/1625 partial`.
- Knock-on: `_crit = p && FEED_GAP within 60 s` (`dashboard.py:~868`) makes the banner flash "⚠ FEED" as if a trade were at risk.
- Same as 1.0. Fix: publish `position: None` when `not pos.open`, or test `!p || !p.open` in the page.

### [MEDIUM] On expiry day the last ~9 minutes are untradeable whenever Breeze repeats its 15:15 freeze, and a feed-alarm buzz is a predictable daily event (`feed_health.py`, `orchestrator.py:701`) — reproduced
- The full day recorded FEED_GAP `SPOT_FROZEN` at 15:16:20, then FEED_UNHEALTHY ×260 rounds until 15:25. No entries were possible after 15:16, the most gamma-rich window of an expiry.
- This is correct protective behaviour: 1.0 traded the garbage tail. But the operator should *expect* it Monday. The banner then contradicts it (see the HIGH finding).
- An open position is still managed and flattened, priced on option quotes. The 15:17–15:25 run shows the flatten fills fine.
- Fix: none required. Add a RUN_LIVE line: "expiry day: entries usually stop ≈15:16 when the cash feed freezes."

### [LOW] Avoidable sound noise: approach cue rings in the same round as the entry, and during the post-stop cooldown (`orchestrator.py:678-683`) — reproduced
- Full day: **20 of 22** approach cues fired in the *same round* as an entry. Each trade is then "tick + marimba + exit" back to back.
- **2** approach cues (10:21:12, 13:50:20) rang during POST_STOP_COOLDOWN, when no trade was possible. The comment "only when a trade is actually possible" is false: the condition checks health and position but not cooldowns.
- Only audible in "all cues" mode. The default first click selects "trades only", which excludes approach.
- Fix: emit `approach` only when the gate is None and no entry fired this round, i.e. move it after the gate block and require `gate is None and not fire`.

### [LOW] Post-cooldown re-anchor attempted after the 15:25 flatten (`orchestrator.py:660-666`) — reproduced
- `15:25:38 FEED_GAP stage=post_cooldown_band reason=FEED_SPOT_IMPLAUSIBLE`. The post-exit path skips re-anchors after flatten, but the cooldown-edge path does not. It adds noise to the events strip at the close.
- Fix: guard the cooldown-edge recompute with `not hhmm_reached(round_ts, self.flatten_at)`.

### [LOW] Prev-close seed comes from the frozen-feed rows and takes ~21 s at startup (`feed.py:_seed_from_parquet`) — reproduced
- The seed row is 16-Oct **15:20:09**, spot 23119.0. The spot had been frozen since 15:16 (the |basis|≤45 filter passes frozen rows).
- It is harmless today because the seed is never displayed and 09:20 re-anchors. The 20.9 s parquet tail scan (on a loaded box) runs before the first round.
- If the 2.0 folder has no prior `tapes/` (fresh install path), there is no seed and the terminal prints a "CRITICAL" line, which will alarm the operator for nothing.
- Fix: also reject rows with unchanged spot for ≥30 s. Scan in a background thread.

### [LOW] Cosmetic and doc points
- The 09:20 calc is labelled `interval` (`freshness_engine.py:62-70`: the seed sets the attempt clock), so level history shows "interval" instead of "open".
- `RUN_LIVE.md` §4 still says "a normal Monday is NOT a weekly-expiry day → floor 45, no ITM fallback". True in general, wrong for *this* Monday, which the header correctly shows as EXPIRY DAY.
- The REGIME event is stamped with wall clock while the other startup events use `start_ms`. This only matters in replays.
- TRAIL_SL fills at the bid can book below the "+7% minimum" (e.g. 80.05 → 85.25 = +6.5%). This is by design of paper bid fills, but the operator may question it.

## What 2.0 does better than 1.0 (evidence)
- **No trading or re-anchoring on the 15:16+ garbage feed.** It was gated by FEED_UNHEALTHY in the rehearsal. 1.0 would have re-anchored on spot +100 and bought deep-ITM.
- **Buys the current ATM, not the anchor-time ATM** (`execution.py:188-191`). Every ENTRY in the full day was ATM at fill time.
- Manages positions every round. Cooldown-end re-anchor happens before the entry check, which is visible as `post_cooldown` anchors at entry rounds.
- Sounds are engine-decided: 0 sister cues, and every played cue maps 1:1 to an engine event.
- SSE push stayed under 200 ms lag on a heavily loaded box. Tapes flushed and closed with exact jsonl/parquet parity.
- Caveat: "expiry day unchanged" is not literally true. These changes, the feed gate and `hold_while_in_trade` all alter expiry-day behaviour, all in the operator's favour.

## What 2.0 does worse than 1.0 (evidence)
- Nothing regressed in this rehearsal. The HIGH and MEDIUM UI issues above are inherited from 1.0, not introduced by 2.0.

## Tests / checks you ran
- `python3 -B -m pytest -p no:cacheprovider -q tests/test_expiry.py tests/test_timing.py` → 8 passed.
- Calendar resolution script for 16/19/20/21/27 Oct (output quoted above).
- `scratch/28/t28_prepass.py` streamed the 15-Oct tape to 2-s rounds. `t28_check.py` sanity-checked the expiry chain.
- `T28_PACE=20 python3 -B t28_run.py` + `t28_browser.py`: full day. Results in `work/analysis.txt` and `work/result_fullday.json`; tapes in `work/fullday_tapes/`.
- `T28_START=14:58 T28_PROX=80`: afternoon window plus browser (`work/analysis_pm.txt`, `browser_pm.json`, `shot_15*.png`).
- `T28_STOP=09:24`: opening window plus browser (`browser_am.json`, `shot_0912/0920/0921/0923.png`).
- `t28_flatten.py`: 15:25 EXPIRY_FLATTEN proof.
- All scratch code is in `/home/claude/teardown/scratch/28/`.
