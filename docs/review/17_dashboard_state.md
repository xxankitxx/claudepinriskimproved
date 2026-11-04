# 17 — Dashboard backend/state reviewer: findings

Scope: `orchestrator.latest_state`, `_update_view`, `_compute_alarm`, `_capital_block`, `dashboard.py` (Python + the JS that consumes it).
Scripts and raw output: `/home/claude/vajra2_review/scratch/17/probe1.py` (live-object probes) and `probe2.py` (3 recorded days, NIFTY rounds 09:21-15:25).
Certainty tags: **run** = confirmed by running something, **read** = confirmed by reading, **plaus** = plausible.

Headline numbers (probe2, current rules, 09:21-15:25):

| day | % time STAND_BY | % ABOUT/BREACH | NIFTY approach edges | sister-breach "tings" | of which weight>=5% |
|---|---|---|---|---|---|
| 10-14 | 99.9 | 0.0 | 1 | 28 | 7 |
| 10-15 | 98.1 | 1.4 | 6 | 241 | 108 |
| 10-16 | 98.8 | 0.9 | 2 | 112 | 31 |

So the cockpit reads "STAND-BY — no NIFTY level in range" for ~99% of a non-expiry session (the idle feeling), and the "ting, ting, ting" is almost entirely the sister-breach sound (112-241 per day), not NIFTY approaches (1-6 per day).

## 1. Correctness defects (most severe first)

### [HIGH] Entry strike is derived from the ANCHOR-time ATM, while the SCREAM shows the CURRENT ATM  (execution.py:184, orchestrator.py:596-602, 992/1010)  certainty: read + run
- `Executor.enter()` calls `select_strike(direction, band.atm_strike, ...)`; `band` is the HELD band, so `atm_strike` is the ATM at the last re-anchor (up to 20 min old). `_compute_alarm` shows `BUY NIFTY {_nifty_head['atm']}` = ATM of the current spot.
- probe2: in 100% of entry-zone rounds (12/12, 1139/1139, 556/556) anchor ATM != current ATM. Recorded strikes confirm it (22600 PE, 22750 CE, 22950 CE, 23100 CE, 23300 PE).
- Why it matters: on expiry day the band hugs spot so the error was 0-1 strike. Off expiry (spot must travel ~100 pts to the level) or with any v2 narrowing that holds levels, the bot buys a 1-3-strike ITM option while the cockpit promises ATM; premium, delta and sizing differ from what the operator mirrors on ICICI.
- Fix: pass the live ATM (`br.atm_strike` of the current round) to `enter()`; keep `band` only for levels. Show `target.strike` from the same value the executor uses (compute once, store in `entry`).

### [HIGH] `/api/state` returns HTTP 500 on any NaN/inf in the state  (dashboard.py:33; breeze.py:99-110,257-261)  certainty: run (500) / plaus (trigger)
- Starlette `JSONResponse` uses `allow_nan=False`. probe1: `vix=NaN` in `state['nifty']` -> `ValueError: Out of range float values are not JSON compliant`. The JS then reads every poll as a failure and paints "DATA FROZEN — no response from engine" while the engine is fine.
- Trigger path: `normalize_vix(None)` returns NaN; `_on_ticks` carry-forward uses `not norm.get(fld)` and NaN is truthy, so NaN is never replaced by the previous good value; `_build_snapshot` only tests `is not None`. `snap.vix` -> `instruments_view[..]['vix']` and `_nifty_head['vix']`. (No NaN in the 3 recorded tapes: 0 rows, so this is latent.)
- Fix (two layers): (a) `sanitize(state)` before publish: recursively replace non-finite floats by `None` (cost ~0.3 ms; or use `json.dumps(..., default=..)` with a finite-check walk); (b) `_build_snapshot`: `vix = ltp if finite and >0 else last_good`. Also `tape.py` writes bare `NaN` into jsonl (invalid strict JSON) - same sanitiser.

### [HIGH] `levels_stale` blinks for ONE round instead of persisting  (orchestrator.py:387-395, 738-747)  certainty: run
- After a failed re-anchor the flag is set True, then the next round's `elif band is not None: self._levels_stale = False` clears it (a held band is "valid"). The retry only happens every `retry_secs` (30 s). probe1 (forced `compute_band` failure): `levels_stale` over 10 rounds = `[True, False, True, False, ...]` (alternating with the retry cadence). In live (4 Hz, 30 s retry) the badge shows for 250 ms every 30 s while the levels are minutes old. Same for `_recompute_nifty_band` failure.
- The council fix F9 conflated two cases: a NIFTY-missing *round* (transient, should clear on recovery) vs a failed *re-anchor* (must persist until a success).
- Fix: `levels_stale = fe._last_success_ms is None or fe._last_success_ms < fe._last_attempt_ms or held_age > 1.5*hold_secs`, plus `or self._nifty_gap_on`. Expose `held_age_secs` and `stale_reason` in `idle_diagnostics` (below).

### [HIGH] No detection of a frozen/implausible feed anywhere in the cockpit; `TICK STALE` badge and the JS watchdog cannot fire for the real failure  (breeze.py:247-261, feed.py:352-356, dashboard.py:522,487-489)  certainty: read + run
- Ticks carry no receive timestamp; `MarketSnapshot` has no `spot_ts`. `round_ts` is wall-clock at each round, so the watchdog (judges `round_ts` change) stays green when the cash feed is frozen. `instruments.NIFTY.stale` is set only when the snapshot is missing (never after the first tick, because `latest` carries values forward), so `TICK STALE` is effectively dead.
- probe2 (A2 evidence): longest NIFTY spot-unchanged run is 286 s / 265 s / 306 s, all starting 15:15; the longest run anywhere else is 4.1 s / 9.8 s / 16.0 s (p99.9 run ~2.0-2.5 s). So a value-based freeze detector with a 30 s threshold has zero false positives on these tapes and fires 30 s into the 15:15 freeze. |basis| pre-15:14: p99.9 = 17.5 / 15.5 / 27.8; the broken resume prints 116-283.
- Fix: design in section 2 (`feed_health`). Value-based detector is replayable (pure function of the stream), so the lead can measure it on tapes; add `recv_ms` to the normalised tick as a second, live-only signal.

### [MEDIUM] SCREAM text says "-> BUY ..." when the engine will NOT buy  (orchestrator.py:1008-1032)  certainty: read
- `ABOUT_TO_BREACH`/`BREACHED` is purely geometric. `_entry_block` is only set when `fire` is true and the message only explains INSUFFICIENT_CAPITAL / PREMIUM_TOO_THIN. Not explained: expiry gate closed (`nonexpiry_mode observe/off`), silent post-profit cooldown (also green `nbuy` box shows BUY), outside `[trade_start, entry_cutoff)` (09:20-09:21 and 15:25-15:30), feed unsafe, NO_QUOTE / ORDER_PLACEMENT_FAILED (badge shows it, message does not).
- Fix: build `why` from the ordered `gates` list of `idle_diagnostics` (single source of truth shared with `fire`); when any gate blocks, state stays ABOUT_TO_BREACH but action text becomes "NO TRADE: <gate>". Never render the BUY callout while a gate is closed.

### [MEDIUM] Alarm state machine has no hysteresis and a regime-blind WATCHING threshold  (orchestrator.py:980-1037)  certainty: read + run
- State is a pure function of (spot, levels): flickers at the 7-pt boundary at 4 Hz. WATCHING = `dist <= prox*2.5` = 17.5 pts is meaningless off expiry (nearest level p50 = 67-100 pts), hence 98-99.9% STAND_BY above. STAND_BY text omits the nearest level, distance and next re-anchor.
- `band is None` (no seed, failed first calc) falls into STAND_BY ("no NIFTY level in range; tracking the universe") - a dead engine state rendered as calm. Needs a `NO_LEVELS` state. Also no `band_valid` check (`upper > lower`); no inverted bands in the 3 tapes (0), but a lagging VWAP on a trend day can produce one and `du/dl` signs then invert.
- Fix: states `NO_LEVELS | STAND_BY | WATCHING | ABOUT_TO_BREACH | BREACHED | IN_TRADE`; WATCHING at `max(25, 0.4*half_width)` pts; enter ABOUT_TO_BREACH at `prox`, leave at `prox+3` (hysteresis) and not before 3 s dwell.

### [MEDIUM] Sound "ENTERED" trigger is a one-round window; exits/approach rely on foreground 250 ms polling  (orchestrator.py:582, dashboard.py:974-975, 995)  certainty: read (+ plaus for miss rate)
- `_entry_block = None` is reset every round and `ENTERED` exists in exactly one published state (250 ms). The JS plays "trade taken" only if a poll lands in that state; the poll period (setInterval 250 ms + fetch latency) equals the round period, so beat-skipping misses some, and a hidden tab (browsers throttle timers to ~1/s, much less after minutes hidden) misses most. Same weakness for any short ABOUT_TO_BREACH episode. Key `strike+right+entry_fill` also suppresses a second trade at identical strike/fill.
- Fix: server-side `sound_events` queue (section 2.4).
- Related UI truth bug: the pill says "sound on" after a reload even though audio is locked until a user gesture (play() rejections are swallowed at dashboard.py:462). Pill must show "click to arm" until a play() resolves.

### [MEDIUM] Day state is not restart-safe  (orchestrator.py:158-166, execution.py:108)  certainty: read
- journal, `trades_today`, realized P&L, `_equity_high`, `_last_stop_ms` (stop cooldown) and `Executor.capital` all reset on a restart. 10-16 had 4 STARTUP_CONFIG events. After a restart the cockpit shows ₹1,00,000 and 0 trades; the engine re-risks full capital and forgets an active 11-min stop cooldown. Manual says only the *position* is lost; in fact P&L and cooldown are too.
- Fix: on start, rehydrate from `tapes/<date>/order_tape.jsonl` (durable sidecar): rebuild journal, capital, last SL_HIT ts; flag `rehydrated: true` and show an "open position lost" warning if the last order row is an unmatched ENTRY.

### [MEDIUM] Pre-open `latest_state` is nearly empty  (orchestrator.py:131-132, 426-427)  certainty: run
- probe1: before 09:20 the state only has `decision, events, instruments, position, round_ts`. No `session_date`, `expiry`, `live_orders`, `capital`, `nifty` (seeded levels), `level_history`. The header shows `date …/expiry …/LIVE_ORDERS …` and the banner never says "levels seeded" (it reads `level_history`), contrary to OPERATOR_MANUAL 2.3. Pre-09:20 feed events (STARTUP/FEED_GAP/step resolution) are not mirrored either.
- Fix: call `_update_view` (or a light `_publish_static()`) from `__init__`, after `seed_from_prev_close`, and on every pre-calc round.

### [MEDIUM] Capital block / P&L is gross and mark-to-LTP  (orchestrator.py:1051-1073, execution.py:70-76,270)  certainty: read
- Arithmetic is internally consistent (cash = cap - deployed; equity = cap + unrealized; pnl = equity - start; dd from high-water). Verified by reading; the 2026-07 note about the double count is correctly fixed.
- But: (1) no brokerage/STT/exchange/GST/stamp: roughly ₹230-300 per round trip on ₹1L all-in (~0.25-0.3% of capital) vs a 7% minimum-profit target; (2) `unrealized` marks at LTP while exit fills at bid, so equity while in trade is optimistic by about spread x qty; (3) `last_premium` is frozen when the quote is missing, with no staleness flag; (4) `_equity_high` is created lazily with `getattr` (works, but initialise in `__init__`; it is also never persisted). Add `capital.fees_est`, `capital.net_pnl`, `capital.mark_basis: "LTP"|"BID"`.

### [MEDIUM] NIFTY-missing early return publishes a half-stale state with no flag on NIFTY  (orchestrator.py:431-441)  certainty: read
- Returns `{**latest_state, round_ts, events, levels_stale: True}`: arming, nifty spot, position, `instruments.NIFTY` (no `stale`) are carried unchanged while `round_ts` advances, so the watchdog is happy and `TICK STALE` does not show. In a trade, `manage()` does not run in that round. Mark `instruments.NIFTY.stale=True`, set `feed_health.status=STALE`, freeze `arming.state` to `NO_DATA`.

### [LOW] Stop-cooldown-end recompute is not used by that round's entry  (orchestrator.py:554, 564-579)
- `entry` and `fire` use the local `nifty_band` computed before `_recompute_nifty_band(..., "post_cooldown")`, so the round in which the cooldown ends still trades the OLD band; the new band applies from the next round. Recompute before `_approach_entry` (or skip entry that round). certainty: read.

### [LOW] Other
- `expiry` badge `T-N` counts calendar days (JS `dashboard.py:657`): shows T-5/T-4/T-3 on 10-14/15/16 where trading days to expiry are 3/2/1 (probe1: 5 calendar, 3 trading; 10-20 is a holiday). Send `dte_trading` from the server.
- Events strip dedup only collapses *consecutive* identical rows; two alternating types (REVERSAL_WATCH / FALSE_BREAK) defeat it. Dict entries are mutated in place (`count`, `ts`) while the HTTP thread may serialise them: torn row, never an exception (json encoder snapshots items under the GIL). certainty: read.
- `/api/health` is static `{"ok": true}`; cannot tell a hung engine from a live one. Return `last_round_age_ms` and 503 when > 5 s.
- Browser-local timezone is used for session clock, countdown to 15:30, `endOfDay` and "ago" strings (dashboard.py:424,478,491,662). Wrong on a non-IST machine and in `replay_browser`. Send `hhmmss_ist`, `secs_to_close` from the server.
- `setInterval(tick, 250)` has no in-flight guard: a slow response lets requests overlap and render out of order. Use chained `setTimeout` after completion and a sequence check (`s.round_ts` must be >= last rendered).
- `instruments.NIFTY.freshness_age_secs` is the pre-recompute age on the re-anchor round while `nifty.held_age_secs` is zeroed (inconsistent for one round).
- `secs_since_stop` (orchestrator.py:562) is in milliseconds; rename.
- TCS zero-weight is flagged only in the tape/stderr; show a header chip.

## 2. Improvements / design proposals (ranked)

### 2.0 Cost, thread-safety and polling verdicts (what was asked)
- Rebuild cost is negligible: `_update_view` 0.06 ms/round, JSON dump of the whole state 0.40 ms (probe1, synthetic 24 instruments), vs 3.3 ms/round total for `process_round`. The `[-80:]` slices copy at most 80 pointers; `level_history` only holds NIFTY anchors (~20-30/day), so it never reaches 80.
- Payload: 28 KB (synthetic, 150 rounds) with `instruments` 10.3 KB, `burst_board` 5.3 KB, chain 3.2 KB, breach_alerts 2.9 KB, journal 2.2 KB. Real-day growth: journal <= 80 rows (~6 KB), events 40 rows, so expect ~40-60 KB. At 4 Hz = 0.16-0.24 MB/s per tab, trivial on localhost, wasteful over LAN/VPN. `burst_board`/`breach_alerts` duplicate `instruments`.
- Thread-safety: copy-on-publish is sound. `self.latest_state = {...}` is an atomic attribute swap and all nested objects are fresh each round except `events` rows (in-place `count/ts`, benign). The one rule for v2: do NOT iterate a `deque`/list owned by the engine from the HTTP thread (`list(deque)` raises "deque mutated during iteration" under concurrent append). Engine publishes an immutable `tuple` snapshot of the sound ring each round. The JSON dump holds the GIL (~0.4 ms per request), harmless.
- Polling: 4 Hz equals the engine cadence so it is not too fast for the data, but (a) payload is mostly slow-changing; (b) no overlap guard; (c) edge logic in the browser is wrong architecture (background throttling, one-round windows). Recommendation: `/api/hot` (~1.5 KB: round_ts, spot, delta, levels, distances, arming, position headline, capital headline, feed_health.status, cooldown, `sound_events.cursor`) polled 4 Hz in foreground, 1 Hz hidden; `/api/state` (slow blocks) at 1 Hz with `ETag` = `round_ts//1000`; `/api/events?after=<seq>` or SSE for `sound_events`. SSE is the best fit (events delivered on arrival even in a hidden tab).

### 2.1 `regime` block (new)
Computed once per round in `_update_view` from `self.expiry`, config and the held band.
```json
"regime": {
  "mode": "NON_EXPIRY",                       // EXPIRY | NON_EXPIRY
  "label": "NON-EXPIRY · 3 trading days to expiry (Mon 19 Oct)",
  "session_date": "2026-10-14", "expiry_date": "2026-10-19",
  "dte_trading": 3, "dte_calendar": 5, "t_years": 0.0137,   // to 15:30 of expiry date
  "is_monthly": false, "basis_mode": "IMPLIED",
  "nonexpiry_mode": "trade_small", "gate_open": true, "size_mult": 0.5,
  "band_method": "ITM_VWAP",                  // v2 id from the band reviewers, e.g. CAPPED_ITM_VWAP
  "band_params": {"pad": 0.10, "cap_pts": 35},
  "half_width_raw": 132.0, "half_width_eff": 35.0, "narrowed": true,
  "extrinsic": {"ce_1itm": 83.5, "pe_1itm": 77.0},          // time value that inflates the raw band
  "proximity_pts": 7.0, "min_premium": 45.0,
  "last_entry_hhmm": "15:00", "flatten_hhmm": "15:25"
}
```
`dte_trading` = count of `cal.is_trading_day` after session_date up to and including expiry_date (needs the calendar object kept on the orchestrator; today only the resolution is kept).

### 2.2 `idle_diagnostics` block (new) - the anti-idle panel
```json
"idle_diagnostics": {
  "verdict": "WAITING_FOR_PRICE",   // WAITING_FOR_PRICE | LEVELS_NOT_READY | FEED_UNSAFE | GATE_CLOSED | COOLDOWN | CUTOFF | PRE_WINDOW | IN_TRADE | ENTRY_BLOCKED
  "headline": "NIFTY 67 pts (0.28%) above LOWER 24,310 · re-anchor in 13m 8s · all gates open",
  "nearest": {"side":"LOWER","level":24310.0,"dist_pts":67.2,"dist_pct":0.278,"to_trigger_pts":60.2,
              "other_side":"UPPER","other_level":24520.0,"other_dist_pts":142.8},
  "band": {"upper":24520.0,"lower":24310.0,"half_width":105.0,"spot_pos_pct":32,"valid":true,
           "anchor_ts":1760433000000,"anchor_trigger":"interval","anchor_age_secs":412},
  "reanchor": {"interval_secs":1200,"next_in_secs":788,"stale":false,"stale_reason":null},
  "velocity": {"pts_per_min_60s":3.1,"toward":"LOWER","eta_secs":1300},
  "gates": [   // evaluated in this order; `fire` must use the same list
    {"id":"FEED_SANITY","ok":true,"detail":"spot age 0.2s, |spot-implied| 3.1"},
    {"id":"SESSION_WINDOW","ok":true,"detail":"09:21-15:25"},
    {"id":"LAST_ENTRY","ok":true,"detail":"cutoff 15:00, 4h12m left"},
    {"id":"EXPIRY_GATE","ok":true,"detail":"trade_small"},
    {"id":"STOP_COOLDOWN","ok":true,"detail":null},
    {"id":"PROFIT_COOLDOWN","ok":true,"detail":null},
    {"id":"POSITION","ok":true,"detail":"flat"},
    {"id":"PREMIUM_FLOOR","ok":true,"detail":"ATM CE 142.5 >= 45"},
    {"id":"CAPITAL","ok":true,"detail":"lots=7"},
    {"id":"PROXIMITY","ok":false,"detail":"67.2 pts > 7.0"}
  ],
  "blocking": "PROXIMITY",
  "today": {"approaches":3,"entries":0,"rejects":0,"min_dist_pts":9.5,"max_excursion_pts":190,
            "time_in_watch_secs":410}
}
```
Distance in % matters because the same 67 pts means different things at 22,600 vs 25,000. `today.min_dist_pts` shows near-misses ("closest you got was 9.5 pts at 11:42") so a no-trade day is explainable.

### 2.3 `feed_health` block (new)
```json
"feed_health": {
  "status": "OK",                    // OK | DEGRADED | STALE | FROZEN
  "reasons": [],                     // SPOT_FROZEN | SPOT_JUMP_UNCONFIRMED | IMPLIED_DIVERGENCE | OPTIONS_STALE | INSTRUMENTS_STALE | ROUND_LAG | WS_DOWN
  "entry_blocked": false,
  "spot": {"value":24415.2,"age_ms":180,"unchanged_secs":0.4,"frozen":false,"last_change_ts":1760433119800},
  "implied": {"future":24418.3,"basis":3.1,"basis_ewma":2.9,"dev_from_ewma":0.2},
  "divergence": {"spot_vs_implied_pts":3.1,"limit_pts":25,"jump_spot_1s":0.5,"jump_implied_1s":0.4,"ok":true},
  "options": {"atm_ce_age_ms":150,"atm_pe_age_ms":170,"chain_quotes":26,"chain_expected":26},
  "instruments": {"total":24,"live":23,"stale":1,"stale_list":["LT"],"stale_weight_pct":4.4,"never_seen":[]},
  "round": {"lag_ms":12,"max_gap_ms_60s":260,"rate_hz":4.0},
  "ws": {"connected":true,"ticks_per_sec":410,"last_tick_age_ms":40,"reconnects":0}
}
```
Data sources: `unchanged_secs` is a pure function of the (round_ts, spot) stream, so it is replayable; `age_ms` needs `recv_ms` added in `_normalize_tick`, a `spot_ts_ms` field on `MarketSnapshot`, and `ws` counters on `BreezeWrapper` (all live-only). Proposed thresholds from the tapes (defer final gate numbers to the feed-sanity reviewer):
- FROZEN: spot unchanged >= 30 s (tape false-positive max 16 s outside the 15:15 window; p99.9 2.5 s). DEGRADED at 10 s.
- IMPLIED_DIVERGENCE: |basis - basis_ewma| > 25 pts, or one-tick |dspot| > 25 pts with |dimplied| < 0.25 x |dspot| (the 15:20 resume: spot +185/+240/-134, implied < 10). Normal |basis| p99.9 is 15-28 pts, so a bare |basis| limit must be >= 40.
- Hold `entry_blocked` until the divergence has stayed under the limit for 30 s.
- Edge-log changes of `status` to the event tape (`FEED_HEALTH`, must be added to `schema.EVENT_TYPES` and `RECALC_TRIGGERS` gets `post_cooldown`).

### 2.4 `sound_events` (new) - server-side edge queue
Engine-owned ring (max 64), monotonically increasing `seq` from 1, `epoch` = process start token (client resets its cursor when `epoch` changes, so a restart cannot make it ignore new events). Published each round as an immutable tuple.
```json
"sound_events": {
  "epoch": "20261014T091103-a91f", "cursor": 187, "oldest": 130,
  "events": [
    {"seq":186,"ts":1760433120250,"kind":"APPROACH","side":"UPPER","sound":"approach","priority":2,
     "ttl_ms":15000,"text":"NIFTY 4.8 pts below UPPER 24,520","data":{"level":24520.0,"dist":4.8}},
    {"seq":187,"ts":1760433121000,"kind":"ENTRY","sound":"entry","priority":3,"ttl_ms":15000,
     "text":"BOUGHT NIFTY 24400 CE x 7 lots @ 142.5","data":{"strike":24400,"right":"CE"}}
  ]
}
```
Kinds (minimal set, final policy belongs to reviewer B): `ENTRY`, `EXIT_WIN`, `EXIT_LOSS`, `APPROACH` (NIFTY inside trigger distance, once per episode), `FEED_FAULT` (status leaves OK, once, repeat <= 3 x 60 s), `SISTER_CONFIRM` (only if weighted pull >= `strong_pull` AND NIFTY within 25 pts; aggregated list; default silent otherwise), `STOP_COOLDOWN_END` (visual only by default).
Client contract (exactly once): keep `lastSeq` in memory; on first response set `lastSeq = cursor` (no retro sounds); each poll process events with `seq > lastSeq` in order, set `lastSeq = max(seq)`; drop events with `now - ts > ttl_ms` (tab waking from background must not replay a burst: play only the highest-priority surviving event); if `oldest > lastSeq + 1` a gap occurred, show a toast, do not replay. The server never mutates or re-orders delivered events. `ENTRY`/`EXIT` events are created inside `_emit_orders`, which removes the one-round window.
Server state machine (pure function of `round_ts`, no wall clock, so replays reproduce it): APPROACH arms at `dist <= prox`, re-arms only after `dist >= prox + 8` AND >= 60 s since the last cue; SISTER debounce: breach must persist >= 3 s, per-instrument re-arm >= 120 s after clearing, global cap 1 cue / 30 s. Measured on the tapes: approach cues with (7/15 pts, 60 s) hysteresis 1/3/2 per day (vs edges 1/6/2); sister cues are the problem (28/241/112 raw edges, 7/108/31 for weight >= 5%) - with the debounce above plus the weight and NIFTY-proximity filters the expected count is single digits per day. Write each cue to the event tape (`SOUND_CUE`) so the policy is auditable and countable in the lead's replays.

### 2.5 Smaller state additions
- `arming.gates`/`arming.why` from the same gate list; `arming.state` gains `NO_LEVELS`, `NO_DATA`.
- `capital`: `fees_est`, `net_pnl`, `mark_basis`; `rehydrated`.
- `clock`: `{hhmmss_ist, secs_to_close, secs_to_flatten}`.
- `expiry`: add `dte_trading`, `nonexpiry_mode`, `gate_open` (superseded by `regime`; keep for compatibility).
- Move builders out of `Orchestrator` into `vajra/cockpit.py` (pure functions of engine state) so they unit-test without a feed and cannot touch decision determinism.

## 3. Recommendations specific to the two operator asks
**A (idle on non-expiry days).** Cockpit side: the operator should see, every second, a one-line verdict from `idle_diagnostics` ("NIFTY 67 pts (0.28%) above LOWER · re-anchor in 13m · gates open") and the regime chip with raw vs effective half-width. With the current ITM-VWAP band the true answer on 10-14/15/16 is "waiting: nearest level 100/67/70 pts away" (lead's table); I measured the screen being in STAND_BY 98-99.9% of the time, so the diagnostics are what makes that state legible. Make WATCHING regime-aware (`max(25, 0.4 x half_width)`) so there is a visible ramp, and show `today.min_dist_pts`. Engine side: fix the HIGH items above before enabling any narrowing (strike/ATM mismatch bites hardest exactly when levels are held far from the anchor spot).
**B (sounds).** Move to `sound_events` (2.4). Root cause of "ting, ting, ting" in the data: sister-breach edges, 112-241 per day (membership of `breach_alerts` flickers per instrument at 4 Hz, including stale <-> live transitions), not NIFTY approaches (1-6). Minimum policy: 4 audible cues (ENTRY, EXIT win/loss as two timbres, APPROACH, FEED_FAULT), sisters silent unless STRONG and NIFTY within 25 pts. Add the "click to arm" pill state.

## 4. Things that are fine and should NOT be changed
- Copy-on-publish of `latest_state` (atomic swap) and fresh nested objects per round; do not add locks around it.
- `_capital_block` arithmetic (cash = cap - deployed; equity = cap + unrealized) and the all-in model (operator-locked).
- Per-instrument try/except isolation, the stale-carry universe completeness (`stale: true`), run-length dedup of the strip (tape still records every event).
- Silent 15 s post-profit cooldown (operator rule): keep it silent in audio; only fix the SCREAM text so it no longer promises a BUY.
- Violet standing post-stop banner and its priority over BREACHED.
- Rebuild frequency and cost of `_update_view` (0.06 ms); do not optimise it.

## 5. Tests I would add
1. `test_api_state_survives_nan`: put NaN/inf into vix/basis/ltp, `TestClient.get('/api/state')` returns 200 with nulls.
2. `test_levels_stale_persists_until_success`: force `compute_band` failure; flag stays True for N rounds, clears only after a success (and still clears after a one-round NIFTY gap).
3. `test_entry_strike_uses_current_atm`: held band anchored at ATM 24100, spot 24205, entry buys 24200 and `arming.target.strike == position.strike`.
4. `test_scream_never_says_buy_when_gated`: for each gate (expiry off, profit cooldown, 15:26, feed frozen) message contains "NO TRADE" and `target is None`.
5. `test_sound_events_exactly_once`: simulate a client polling at random intervals incl. 5 s gaps; each seq delivered once in order; epoch change resets; ttl drops stale cues.
6. `test_sound_policy_replay_counts`: replay the 3 tapes (lead harness), assert cues/day <= 12 and APPROACH cues == hysteresis count; deterministic across two runs.
7. `test_freeze_detector_on_tapes`: spot unchanged >= 30 s flagged only inside 15:15-15:21 on the 3 days; `entry_blocked` true until divergence clears.
8. `test_preopen_state_complete`: after `seed_from_prev_close`, `latest_state` has `session_date, expiry, live_orders, capital, nifty.upper/lower, level_history`.
9. `test_restart_rehydrates_day`: write order_tape rows, construct Orchestrator, journal/capital/stop-cooldown restored.
10. `test_dte_trading`: 10-14 -> 3, 10-15 -> 2, 10-16 -> 1 (Dussehra-aware).
11. `test_state_threadsafe_publish`: hammer `/api/state` from threads while a synthetic day runs; no exception, `round_ts` monotone per client.
