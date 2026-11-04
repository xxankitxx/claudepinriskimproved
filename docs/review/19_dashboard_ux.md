# 19 — Dashboard UX reviewer (R19): findings

Scope: `vajra/dashboard.py` read line by line (JS 415-996), plus orchestrator `_update_view/_compute_alarm`, feed.py, breeze.py, app.py for what the page consumes.
Evidence tags: [RUN] confirmed by running, [READ] confirmed by reading, [PLAUS] plausible.
Scripts: `/home/claude/vajra2_review/scratch/19/nan_test.py`, `sound_sim.py`.

## 0. Headline numbers (measured)

**Sound count the current JS edge logic would have produced on the three recorded days** [RUN, `sound_sim.py`; one poll per round = upper bound, stale-carry not excluded]:

| day | trades | NIFTY approach dings | sister_breach dings | top offenders |
|---|---|---|---|---|
| 10-14 | 1 | 1 | 28 | BANKBARODA 10, BAJAJFINSV 3 |
| 10-15 | 2 | 0 | **241** | ITC 109, BHARTIARTL 104, BANKBARODA 19 |
| 10-16 | 2 | 0 | **112** | BANKBARODA 67, BHARTIARTL 30, FEDERALBNK 8 |

The "ting, ting, ting" is almost entirely `sister_breach.mp3` (4.0 s clip), fired once per new (non-NIFTY) instrument breach edge. Sister/stock bands re-anchor every 3 min and flicker in and out of BREACH. Approach/trade/exit sounds are rare. Clip lengths: approach_lower 2.4 s, approach_upper 2.8 s, profit 3.5 s, sister 4.0 s, trade_taken 5.0 s, stoploss 6.0 s. The 241 rings on 10-15 are about one per 90 s, each 4 s long. The six clips are not normalised and overlap freely.

## 1. Correctness defects (most severe first)

### [HIGH] One NaN anywhere in `latest_state` blanks the whole cockpit with a false "DATA FROZEN"  (dashboard.py:32, 639; breeze.py:99-110, 257-261)  [RUN]
- `normalize_vix(None)` returns `nan` (breeze.py:106). `_on_ticks` carry-forward uses `if not norm.get(fld) and prev.get(fld)`, and `not nan` is False, so a NaN VIX is never replaced by the last good value. NaN then flows `_build_snapshot` -> `snap.vix` -> `instruments_view[*]["vix"]` and `_nifty_head["vix"]`.
- Starlette `JSONResponse` serialises with `allow_nan=False`. Test: `/api/state` with `vix=nan` returns **HTTP 500**. The JS then fails in `.json()` and the catch calls `applyWatchdog(true)`, which prints "DATA FROZEN — no response from engine" and dims the page (opacity .35), while the engine is perfectly healthy and may be holding a position.
- Fix (server): serve through a sanitiser, `json.dumps(obj, default=str)` then map non-finite floats to null (recursive `_clean()`), and fix `normalize_vix` to return None. Fix (client): check `r.ok`. Show "SERVER ERROR 500 (state not serialisable)" separately from "NO RESPONSE". Keep rendering the last good state, dimmed.

### [HIGH] Sound edge logic is client-side and sampled, so it both floods and misses  (dashboard.py:968-989, 995)  [READ + RUN-sim]
1. Sister rings per new breach edge for each of 23 instruments with no hysteresis, no per-instrument or global cooldown, and no weight filter (ITC/BHARTI/BANKBARODA chatter drove 100+ rings/day). See section 0.
2. Approach sound re-arms whenever `a.state` leaves ABOUT_TO_BREACH, so spot jitter around the 7-pt zone edge retriggers it. It also fires during the 11-min post-stop cooldown and 15 s profit cooldown, when no trade can happen. That is a pure nuisance ding.
3. Trade-taken detection depends on `arming.entry_block==='ENTERED'`, which is true for exactly **one engine round** (orchestrator.py:582-606 resets `_entry_block` each round). A 250 ms `setInterval` poll can skip that round (jitter, GC, tab throttling) and the "trade taken" sound never plays. The dedupe key `strike+right+entry_fill` (line 974) is never cleared on exit, so a re-entry at the same strike and fill price is silent.
4. Background/occluded tab: Chrome throttles timers to 1 Hz, then 1/min (intensive throttling). The operator is on the ICICI window mirroring trades, so edges are missed exactly then.
5. Exit sounds key off the last closed trade only. If two exits land between polls, one is lost.
- Fix: make sounds **server-authoritative**. The orchestrator keeps `sound_events: deque(maxlen=50)` of `{seq, ts, kind, text}` generated once, with cooldowns, in one tested place. The client keeps `lastSeq`, plays each unseen event (and nothing retroactive on first load: initialise `lastSeq` to the current max). Section 3B gives the policy.

### [MEDIUM] Sound pill lies: "sound on" while the browser is blocking audio  (dashboard.py:462, 467-473)  [READ]
- `a.play().catch(()=>{})` swallows `NotAllowedError`. After a reload the setting restores to ON from localStorage but audio is locked until a gesture, and the pill still says "🔔 sound on". Clicking the pill to "check" **toggles it OFF**. The one-shot `pointerdown` unlock only touches `SND.trade`.
- Fix: track `audioUnlocked`. On a rejected `play()` set pill to "🔕 BLOCKED — click to enable" (amber, pulsing). Pill click when ON-but-locked should unlock, not toggle.

### [MEDIUM] Hero panel thresholds disagree with the engine trigger  (dashboard.py:569-571)  [READ]
- Hard-coded `<=2` critical and `<=5` approach, but the engine enters at `entry.proximity_pts = 7` (`arming.proximity_pts` is already sent). At 6.4 pts the top banner says "ABOUT TO BREACH ... BUY" and an entry is already firing, while the hero banner still reads blue "6.4 PTS TO NEAREST PIN" (safe). The operator will also change proximity for non-expiry days.
- Fix: `const prox=a.proximity_pts||7;` critical `<=prox*0.4`, approach `<=prox`, add "watch" tier `<=prox*2.5` (the same WATCHING tier `_compute_alarm` uses).

### [MEDIUM] Hero shows distance only in points, measured to the level, not to the trigger  (dashboard.py:577-587)  [READ]
- No normalisation by a typical move. On non-expiry days the nearest level is 67-100 pts away, which is about 1.3-2 sigma of a 20-min move. The operator cannot see that the system is waiting for a 2-sigma event. It also never subtracts the 7-pt zone, so "67 pts" is really 60 pts to go.
- See the WHY-NO-TRADE strip below (client-computable from `nifty.vix`, `spot`, `dist_*`, `held_age_secs`, `hold_secs`, `proximity_pts`).

### [MEDIUM] Watchdog cannot detect a frozen feed, only a hung engine; pre-open "engine live" is unverifiable  (dashboard.py:481-508; feed.py:386-387, 421-426; orchestrator.py:426-427)  [READ]
- `round_ts` is stamped by the wall clock every poll even if no tick arrived (feed.py:387). So `_wdLastRtsChange` advances through the 15:15-15:20 cash-feed freeze and the banner stays calm. The text "feed stalled or engine hung" over-promises.
- Before 09:20 `process_round` returns without touching `latest_state`, so `round_ts` stays null. Line 717-724 renders "PRE-OPEN — engine live" permanently with no liveness signal. A hung websocket before 09:20 looks identical to a healthy one. Add `latest_state.heartbeat_ts` and `feed_health` updated outside the 09:20 gate.
- `breeze._normalize_tick` stores no receive time, so per-instrument tick age cannot be computed. Add `rx_ms` and `chg_ms` (time of last value change) to each `latest[k]`. Also note `ltp = last or ltp or close` (breeze.py:268): a tick missing `last` is priced at the **previous close**. That is a plausible source of the 15:20 "garbage" jumps and worth checking against prev-close on the tapes (not verified by me).
- `i.stale` and `tickstale` mean only "snapshot not built this round", not "quote is old". `tickstale` (line 522) therefore never lights during a freeze with a still-present spot.

### [MEDIUM] Browser clock/timezone used for market logic  (dashboard.py:478-480, 491, 662-663, 707-709)  [READ]
- `marketPhase()`, `todHM` (SESSION CLOSED test `>=15:29`), "close in", and "FLATTEN in" all use browser-local `getHours()`, while the server is explicitly IST. `round_ts` is a true UTC epoch, so correctness depends on the browser being in IST. On a UTC/VPN/travel laptop SESSION CLOSED never shows and the 15:30 shutdown paints red "DATA FROZEN" and the muted-pill pulse is wrong.
- Fix: format with `Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',hour12:false,...})` (a single `istParts(ms)` helper), or have the server send `ist_hms` and `secs_to_close`. All `toLocaleTimeString()` calls should pass `timeZone:'Asia/Kolkata'`.

### [MEDIUM] Banner/watchdog flicker and fetch pile-up  (dashboard.py:638-639, 995)  [READ]
- `setInterval(tick,250)` with no in-flight guard. If the server stalls (parquet flush, GC), requests queue (6/host) and then 40 renders run back to back. Out-of-order responses can render old state over new.
- A single failed fetch immediately paints the red banner (no debounce); it self-clears on the next success, so it flashes.
- Fix: `let busy=false;` skip when busy; use `AbortController` with a 2 s timeout; use recursive `setTimeout`; require 2 consecutive failures (about 1 s) before red; show "last good age" counter.

### [MEDIUM] Expiry badge shows calendar days, not trading days, and does not make the regime obvious  (dashboard.py:654-659)  [READ]
- `T-<calendar days> to expiry` in a plain grey pill. Fri 16 Oct with expiry Mon 19 shows "T-3" (real: 1 trading day); Wed 14 shows T-5 (real: 3). Only `is_expiry_day` is highlighted (amber); the NON-expiry regime has no emphasis, and nothing tells the operator that the band will be wide and trades rare.
- Fix: server adds `expiry.dte_trading` (from `ExpiryCalendar`, not available in `ExpiryResolution` today; compute in app.py and pass to the orchestrator). Badge: `OFF-EXPIRY · 1 TRADING DAY TO EXPIRY (Mon 19 Oct)` in blue/violet with a tooltip "bands are wide; trades are rare".

### [MEDIUM] Capital is always green; direction colour missing  (dashboard.py:61, 644-645)  [READ]
- `.cap b{color:var(--up)}` and `#capital` is never re-classed. A 38% drawdown prints in green. Only the `(%)` is coloured. Fix: `capital.className=cls(cap.pnl)`.

### [MEDIUM] Whole-DOM rebuilds at 4 Hz destroy interactivity  (dashboard.py:689, 739, 795, 812, 827, 839, 884, 935, 951)  [READ]
- Every table body and the event/breach boxes are replaced via `innerHTML` four times a second. Consequences: the bid/ask `title=` tooltips on the chain LTP cells (line 846, 848) can never show (the element is replaced before the hover delay); text in tables cannot be selected/copied (strike or price the operator wants to mirror on ICICI); the `<title>` inside `#pnlspark` is wiped on first assignment (689).
- Fix: build a string and skip the DOM write when unchanged (`if(html!==el._h){el.innerHTML=el._h=html}`), and update only changed cells for the chain. This also cuts CPU.

### [LOW] 1e9 sentinel and null handling  (dashboard.py:563, 581, 796, 801)  [READ]
- The sentinel itself is handled (581 prints "waiting for levels"). But in `burst board`, line 801: when both dist values are null (N/A row) it prints "▼ — to lower" instead of "no levels"; when only `dist_dn` is null, `Math.abs(null)=0` flips the side. `near` (796) is dead code.
- `cf.net_pull>=0` with null is true, giving "+—". `f(x)` on a string yields "NaN". Add `isFinite` to `f`.

### [LOW] Unescaped strings into innerHTML  (dashboard.py:830, 703, 785)  [READ]
- `e.summary` is built from `payload.reason`, which is `repr(exception)` for FEED_GAP and can contain `<`/`&` from a broker HTML error and break the events strip. Add `esc()`.

### [LOW] localStorage unguarded  (dashboard.py:461, 469)  [READ]
- Top-level `localStorage.getItem` is outside try/catch. If storage is blocked (strict cookie policy, embedded/sandboxed browser) it throws a SecurityError and the **entire script dies**: blank cockpit. `setItem` in the click handler can throw before `_sndPill()` runs. Wrap both. Note `127.0.0.1` and `localhost` are separate origins with separate sound flags.

### [LOW] Buffers not reset on a backwards clock  (dashboard.py:429-431)  [READ]
- `demo_dashboard.py`/replay loops make `round_ts` go backwards; old entries are never trimmed, `bufRate` returns null forever, and the arrays grow without bound. Live is fine (monotonic). Reset buffers when `t < last.t`.

### [LOW] Misc
- Sound pill tooltip omits the profit/stop-loss sounds (line 244).
- Pre-open banner claims "trading 09:21" regardless of config (line 723). Read `expiry` times from state.
- `#pnlspark` re-parses SVG every tick although the cache updates once per second (689).
- SESSION CLOSED test (`>=15:29` of last round) misclassifies an engine crash at 15:29:xx as a calm close; the server should send an explicit `session_state` (`OPEN|CLOSED|ERROR`) and a SHUTDOWN marker.
- Negative: no per-tick event listeners are registered, so there are no listener leaks. Ring buffers are bounded to 5 min. Page memory is stable; the only growth path is the backwards-clock case above.
- Payload: the state includes 24 instruments, 24 burst rows, 13 chain rows, up to 80 journal and 80 level-history rows and 40 events, re-sent in full 4x/s. I did not measure bytes. Reasonable fix: poll `/api/state?lite=1` at 4 Hz and the heavy tables (`journal`, `level_history`, `instruments`) at 1 Hz, or send `ETag/If-None-Match` on a round counter.

## 2. Improvements / design proposals (ranked)

1. **WHY-NO-TRADE strip** (below). Highest value for the idle-time complaint.
2. **Server-authoritative sound events + visible sound policy panel** (3B).
3. **`feed_health` block + red indicator** (3B).
4. Server sends the single source of truth for thresholds (`proximity_pts`, `hold_secs`, `trade_start`, `flatten_before`, `nonexpiry_mode`) so the client has no hard-coded numbers.
5. Hero band: show trigger zones, not just pins (below).
6. Poll hygiene (busy guard, timeout, debounce), DOM diffing, IST helper.

## 3. Recommendations for the two operator asks

### 3A. Complaint A: always-visible WHY NO TRADE strip (non-expiry idle time)

Place it directly under the alert banner, full width, one line (wraps on narrow screens), always visible, never dimmed except by the red feed state.

```
┌ WHY NO TRADE ─────────────────────────────────────────────────────────────────────────────┐
│ REGIME  OFF-EXPIRY · 1 trading day to expiry (Mon 19 Oct)                    [violet pill]  │
│ LEVELS  U 24 610 (+67 pts)   L 24 400 (−143 pts)   band 210 wide   trigger zone ±7          │
│ NEAREST 60 pts to trigger = 1.2σ of a 20-min move · P(touch before re-anchor) ≈ 14%         │
│ RE-ANCHOR in 12m40s  (levels move to spot; waiting gets reset)                              │
│ GATE    ● none blocking  | ◼ BLOCKED: post-stop cooldown 6m10s | ◼ before 09:21 | ◼ feed    │
│ FEED    ● OK  tick 0.3 s ago                                                                │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Markup sketch:
```html
<div id="why" class="why">
  <span class="chip regime" id="w_regime"></span>
  <span class="chip" id="w_levels"></span>
  <span class="chip" id="w_near"></span>
  <span class="chip" id="w_reanchor"></span>
  <span class="chip gate ok" id="w_gate"></span>
  <span class="chip feed ok" id="w_feed"></span>
</div>
```
Computation (client, all inputs already in state or one new field):
- `sigma20 = spot * (vix/100) * Math.sqrt(20/(375*252))` (VIX 14, spot 25 000 gives about 51 pts).
- `togo = min(dist_up, dist_dn) - prox` and `z = togo/sigma20_remaining`, where the window is `min(20 min, time to re-anchor)`: `sigma_w = spot*vix/100*sqrt(secsLeft/60/375/252)`.
- `P_touch = 2*(1-Phi(togo/sigma_w))`, a driftless-Brownian reflection estimate; label it "≈" and say "estimate".
- Gate precedence, first true wins: feed BAD, session not in `[trade_start, flatten_before)`, post-stop cooldown (`cooldown.stop_cooldown_remaining_secs`), post-profit cooldown (new field), `nonexpiry_mode in {observe, off}` (new field `gate.expiry_gate_ok`), position active, levels stale or missing, else "none: waiting for price". Today the orchestrator already knows each of these; expose one `why: {code, text, detail}` computed server-side (orchestrator.py:552-662 branches map 1:1 to reasons). Do not rely on `decision.reason` (`NO_APPROACH` is uninformative and shows only as a tiny grey badge in the LEAN panel, line 878).
- Colour: grey when waiting normally, amber when a gate blocks, red when feed BAD. When P_touch < 10 % add text "unlikely before re-anchor", which makes the idle explanation honest.

Hero band changes (client only): draw the trigger zones as shaded segments of width `prox/span` at each end plus ticks at 25/50/75 % of distance; label the band "210 pts wide". With a 260-pt band the 46-px track gives about 1.9 px per point (assuming a 500-px track), so a 7-pt zone is about 13 px: visible only if explicitly drawn. The current marker clamps to 4-96 % so a spot at the level and a spot beyond it look similar except for the banner. Show `distance` chips as `60 to go (67 to level)` and add `σ` units.

### 3B. Complaint B: sound policy and feed-health

Policy (fewer, meaningful; evidence: 241 sister dings/day):
| kind | when | cooldown / rule | sound |
|---|---|---|---|
| ENTRY | paper entry opened | never suppressed | trade_taken (shorten to under 1.2 s) |
| EXIT_WIN / EXIT_LOSS | exit | never suppressed; replace 3.5 s/6 s clips with under 1.5 s | profit / stoploss |
| NIFTY_APPROACH | first entry into the trigger zone **while flat, not in cooldown, gate open** | 90 s per side after firing; need exit from zone by `>= prox*1.5` to re-arm (hysteresis) | one neutral tick (not two per side) |
| FEED_BAD | feed health goes red | once per episode, repeat every 60 s while red | distinct low alarm |
| SISTER_BREACH | **removed**, or only a weighted aggregate: `same_pull >= strong_pull (0.35)` while in the approach window, max 1 per 120 s | 120 s global | same as approach |

Default-on: ENTRY, EXIT, FEED_BAD. Default-off (toggle): APPROACH. Sister: off. Global rule: at most one sound per 3 s, no overlap (queue or drop lower priority), master volume slider.

Server contract (new, in `latest_state`):
```json
"sound": {"policy": {"ENTRY":true,"EXIT":true,"APPROACH":false,"FEED_BAD":true,"SISTER":false},
          "last": {"seq":41,"kind":"ENTRY","ts":1760600000000},
          "cooldowns": {"APPROACH_UP":38,"APPROACH_DOWN":0},
          "events": [{"seq":41,"ts":...,"kind":"ENTRY","text":"BUY 24500 CE"}]}
```
UI sketch (replace the lone pill by a popover pill):
```
[🔔 3/5 ▾]  → ┌ SOUND ─────────────────────────────┐
              │ ☑ Entry   ☑ Exit   ☐ Approach       │
              │ ☑ Feed-bad  ☐ Sister (off)          │
              │ vol ▇▇▇▇▁   [Test each]  [Mute 30m] │
              │ last: ENTRY 14:02:11 (4m ago)       │
              │ approach cooldown: UP 38s · DOWN 0  │
              │ audio: UNLOCKED ✓  / BLOCKED ✗      │
              └─────────────────────────────────────┘
```
The per-kind toggles persist in localStorage (try/catch). The server decides whether an event exists; the client's toggles only decide whether to play it. The pill shows 🔕 BLOCKED when autoplay fails.

Feed-health indicator: permanent chip in the header, next to the clock.
```
● FEED OK   spot 0.4 s · opt 0.2 s · basis Δ 3 pts     (green)
● FEED DEGRADED  sister gaps 3/23                      (amber)
● SPOT_FROZEN 290 s  — entries blocked                 (red, pulsing, page banner too)
● IMPLAUSIBLE  spot−future 247 pts — entries blocked   (red)
```
Contract: `feed:{status:"OK|DEGRADED|BAD", codes:["SPOT_FROZEN","IMPLAUSIBLE"], spot_unchanged_s, spot_age_s, opt_age_s, basis_pts, last_tick_ms}`; the orchestrator must also use it as a gate, so the dashboard is showing a fact the engine acts on. Suggested thresholds for the data reviewer to confirm: NIFTY spot unchanged more than 5 s in market hours while implied future moved more than 3 pts, or `|basis| > 60` pts for NIFTY, or one-tick spot jump > 3x the implied-future move.

## 4. Things that are fine and should NOT be changed

- Cooldown banner colour (violet, never red/green) and always-on for the whole window.
- Whole-page dimming plus red banner on DATA FROZEN; replay-friendly use of server `round_ts` rather than wall clock for CLOSED.
- Distance-to-stop headline in position/trade banner; flat-since text; journal with confluence columns; level history at the very bottom (operator rule).
- Event strip run-length dedup (the binding tape keeps every event).
- Edge-triggered intent of sounds; localStorage persistence of the setting (just guard it).
- No listener leaks, bounded ring buffers.

## 5. Tests I would add

1. Playwright with a stub server: state with `vix=NaN` -> assert banner says SERVER ERROR, not NO RESPONSE; assert last good state stays dimmed.
2. Contract test: `json.dumps(latest_state, allow_nan=False)` on every round of a short replay.
3. Sound policy unit test (Python, server-side): feed the three recorded days through the sound generator; assert at most N non-flat sounds/day (target: under 12 total, zero sister) and that ENTRY/EXIT counts equal order_tape.
4. Watchdog matrix (stubbed clock + `Intl` timezone UTC): SESSION CLOSED at 15:30 IST regardless of browser TZ; engine crash at 15:29 shows ERROR.
5. Hero thresholds follow `proximity_pts` (7, 15, 25).
6. DTE test: Fri 16 Oct -> 1 trading day; Wed 14 -> 3; Mon holiday weeks.
7. Poll test: server delays 3 s; assert at most one request in flight and no render burst.
8. Feed-freeze replay: inject 290 s frozen spot into a tape; assert FEED red, entries blocked, WHY strip shows SPOT_FROZEN.
