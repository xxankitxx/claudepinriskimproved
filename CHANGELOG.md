# 2.1.1 — the 3 Nov morning: spot frozen 36 min, socket dropped every 30 s (3 Nov 2026, intraday fix)

Diagnosed live from the 3 Nov tape / SDK log while the engine was running.
- **Spot frozen since 09:23.** The SDK's `get_stock_token_value` reads `self.interval`, an attribute the SDK
  only assigns inside `subscribe_feeds` -> the very FIRST token lookup raised, and the first subscription is
  the NIFTY spot. It was subscribed through the SDK's own path (so it ticked) but was never in our token set:
  the first rebuilt socket (09:30, `resubscribed:849`) re-joined everything except the spot. Fixes: the
  attribute is primed at session time; a subscription the SDK resolves itself is learned back from the
  handler's tokenlist; the old handler's tokenlist is absorbed before it is torn down.
- **"4.1!False" joined 74 times.** A contract missing from the SDK's script list is spelled as the literal
  token `4.1!False` (sister ATM±1 strikes for an expiry the sisters do not have). It is now refused as
  `unknown contract` (counted in `sub_errors`) and filtered from every re-join.
- **Transport error every ~30 s.** python-engineio's write loop aborts the socket when its send queue has
  been empty for max(pingInterval, pingTimeout)+5 s = 30 s on Breeze (25 s / 20 s). Only the server's PINGs
  feed that queue, so when the read loop lags behind an opening-bell tick flood the client kills its own
  connection, re-joins 849 tokens, gets a fresh flood, and repeats. Fixes: an idempotent single-token re-join
  every `feed.ws_keepalive_secs` (10) on the supervisor thread, and the client's copy of pingTimeout is raised
  to 90 s after every connect (the server's own timeout is untouched). `ws.keepalives` in health.
- Operator note (RUN_LIVE): three other engines (apex, mythos, trinetra) were running their own Breeze feeds on
  this laptop; one session key per login, so check they are not sharing VAJRA's key.

**Evening pass (the full-day tape of the restarted session, 10:24-15:40)**
- **The ticker was 15-25 s behind the exchange all day** (NIFTY cash `recv_ms - ltt`: median 11-26 s per
  10-min bucket, max 71 s; each socket kick came when the backlog reached ~38 s). Root cause: python-engineio
  starts a NEW OS THREAD for every socket.io message, so a burst of 850 re-join snapshots meant hundreds of
  threads fighting for the GIL and for our lock (ticks could even be applied out of order), and the read loop
  fell so far behind that the PONG to the server's PING was late enough to be dropped (every ~2 min).
  Fix (`vajra/breeze.py`): the engine.io 'message' event is intercepted with an O(1) deque append and ONE
  consumer thread dispatches in receive order; the SDK's per-tick `parse_data` + reverse lookup + strftime
  are replaced by a fast parser keyed off OUR subscription map (322 -> ~100 us/tick in isolation); gauges
  `ws.backlog / backlog_max / inbox_dropped / exch_lag_s / threads / fast_parsed` in health and the WS chip.
  `logs/engineio.log` now records WHICH read-loop exit fired (server kick vs client self-abort).
- The SDK downloads its 30 MB security master at IMPORT (3.5 min on 3 Nov evening) and twice more at session
  time: `vajra/sdk_boot.py` caches it under `tapes/_cache` for the trading day. Stock codes are resolved in
  parallel and cached (`tapes/_cache/isec_codes.json`). Subscriptions go out as ONE packet per batch
  (`subscribe_many`; the restart made ~700 single subscribes on the engine thread).
- A near-the-money quote that goes quiet for `feed.quote_stale_resub_secs` (45) is re-subscribed (a stream
  that died after its first tick was never detected before): `FEED_GAP quote_stale_resubscribe`.
- `_step_maintenance` no longer waits 20 ms per round on the pool futures.
- **Tapes**: leftover parts of a killed session are merged on a background thread from a snapshot (the restart
  spent ~2 min on it before the first round); parts a live writer has open are never touched by a reader;
  one lock per tape prevents two threads merging the same tape; row groups are 2,048 rows (was 256; ~8,000
  tiny groups made the close merge take 10:09 -> 10:13); the six close merges run side by side; the raw
  recorder converts 4k-row batches (20k held the GIL 60-150 ms).
- **Restart correctness**: the flatten's EXIT row is flushed BEFORE `SHUTDOWN` (a window-close could leave a
  SHUTDOWN without the EXIT -> a phantom open position at the next start); the basis history is restored from
  the parquet (the default config writes no basis jsonl, so every 2.1 restart came up with none -> no entries
  15:15-15:29); a Ctrl+C before the first round writes `SHUTDOWN stopped_before_first_round`; a repeated
  round error keeps the cockpit clock advancing; SIGBREAK is ignored during the close too; GC pauses are
  measured into `round.gc` and the standing buffers are re-frozen every ~10 min.
- **Dashboard**: the SSE generator is woken by the orchestrator (`state_seq` / `state_wake`) instead of polling
  every 20 ms, pre-open frames go out too, orjson serialises once per round (the full frame only when it is
  sent), the no-cache middleware is a pure-ASGI wrapper (behind Starlette's BaseHTTPMiddleware a closed
  client's stream never ended: every page reload left a zombie serialising 4x/s all day), the beacon write
  leaves the event loop, `/api/health` reports `streams` / `ser_ms` / `round`.
- **Page**: the measured "1 round per second, 1 s old" was the HIDDEN-tab population (70% of the day) -- the
  page now paints the newest state the moment it is revealed, never schedules paints while hidden, never
  downgrades SSE -> poll from a frozen tab, and reports `pub_lag_*` / `paint_*` in the beacon. The heavy
  tables are keyed rows with per-cell gated writes (was innerHTML of ~1,500 nodes once a second: 50-200 ms
  stalls that coalesced one round in four away); clock columns tick from a 1-s timer; fixed table layout,
  layout containment, transforms instead of layout-animating `left`/`width`.

# 2.1 — live-feed hardening + crash-safe tapes + new cockpit (2 Nov 2026)

Built from the 2 Nov 2026 live tape (the session died at 15:28:10 with a paper position open, every
parquet tape unreadable, 6,655 broker subscription refusals, five websocket silences and four failed
reconnects that each froze the engine 3-5.5 s) plus a line-by-line read of the feed path.

**Websocket (the "packets dropped / ticker stuck" fixes)**
- The broker socket is supervised on its own thread (`vajra/breeze.py`). The engine's watchdog only
  REQUESTS a reconnect; it never waits on the socket (2.0 blocked the round loop for the whole
  disconnect + reconnect + 885 re-subscribes: no stops, no square-off while it waited).
- Every subscription is remembered as its broker stream token; after any reconnect the whole set is
  re-joined in ONE packet (the SDK's own `rewatch` path). Nothing is silently lost.
- The SDK latched `authentication=False` on the first `connect_error` and then refused EVERY new
  subscription for the rest of the day ("Could not authenticate credentials": the 21 Oct 09:50 and
  2 Nov failures). The latch is cleared on every (re)connect and before every subscribe; a genuinely
  dead session key now shows as a socket that never comes back, with a loud console line.
- A dead socket.io client is shut down (its reconnect thread aborted) instead of being orphaned while
  a second client logs in with the same key -- the orphan's rejected reconnects were what tripped the latch.
- The SDK's own reconnect is given a 10 s grace window before the socket is rebuilt (`feed.sdk_reconnect_grace_secs`).
- Sisters/stocks: an ATM±1 strike with no tick 60 s after subscribing is re-subscribed (silently, at most
  every 2 min); NIFTY keeps the 2.0.3 30 s report + 60 s retry.
- A 0 bid / ask / volume is a real value, not a missing one (2.0 carried the previous value forward, so
  a quote that went to no-bid kept showing the old bid). Malformed ticks are counted, never raise.
- Strike-step REST retries run on a pool thread and are collected on a later round (they stalled the
  loop 1-18 s, teardown T14).
- Console output from the engine thread goes through a queue (`vajra/console.py`): a blocked Windows
  console can no longer freeze the round loop. Heartbeat every ~30 s with the socket state.
- The Breeze session is generated once; only the socket retries on a connect failure.

**Tapes (crash-safe)**
- Parquet tapes are written as closed PART files, one per `tape.rotate_secs` (60 s), merged into
  `<tape>.parquet` at a clean close or at the next start / read. A hard kill loses at most 60 s of rows
  (2 Nov: 2.0's single open writer left EVERY tape without a footer).
- The tick/level/basis jsonl sidecars (~9 GB/day) are off by default (`tape.sidecar_jsonl: small`);
  decision/order/signal keep theirs. Parquet uses zstd (about half the size).
- `raw_ticks.parquet` is rotated the same way. Readers (replay, prev-close seed, EOD report) merge
  leftovers automatically. `tools/repair_tapes.py <day> --rebuild-from-jsonl` rebuilds a 2.0
  footer-less file from its jsonl.
- Shutdown is sequenced: a second Ctrl+C is ignored while the tapes close; each step is isolated; the
  socket is always torn down; the close time is printed.

**Dashboard**
- Rebuilt as a static app (`vajra/static/`): `index.html` / `styles.css` / `ui.js` (presentation),
  `data.js` (frozen transport + watchdog + beacon), `sound.js` (frozen sound-council player).
  Contract: `docs/DASHBOARD_STATE_CONTRACT.md`. The state gains `ws` (socket health) and `round`.
- SSE frames are serialised once per round and shared by every client.
- Built by a lead designer and torn down by six specialist reviewers (visual, trader UX, front-end,
  data correctness, accessibility, layout) before the fix pass: position first and the stop distance
  loudest while in a trade, a mirror-ticket line ("BUY NIFTY 03-NOV 23000 CE · 11 lots (715) @ 134.95"),
  BUY never shown on a rejected or blocked entry (the engine now nulls the target too), a standing red
  FEED FROZEN state outside a trade, the open position stays readable under DATA FROZEN, AA contrast,
  pulses that stop, tabular numbers, print styles, fold-safe below 1200 px.
- Engine: the expiry-day badge names the configured weekday (it always said "Tue"); the journal and
  trade count survive a same-day restart; a 0 P&L counts as a loss everywhere (as the sound does).
- `python -m vajra.app --mode replay --date D --pace 1 --port 8790` replays a recorded day at tape pace.

**Power loss (the actual 2 Nov cause: the laptop switched off at 15:28 with a position open)**
- Live mode blocks Windows idle sleep for the session (`vajra/power.py`) and samples the power state every
  30 s: running on battery is an ANOMALY event, a forced alarm cue, a console shout and a red POWER chip.
- At start the engine reports a previous session that ended abnormally (no SHUTDOWN), with the last
  round time and the paper position that was still open, so ICICI can be reconciled first.
- The same-day restart path was proven on the real 2 Nov tape: capital, the open position, 9 closed
  trades, cooldowns and the pre-freeze basis come back; a restored position is squared off at 15:38.
- RUN_LIVE.md has the power plan commands and the restart procedure.

**Trading rules unchanged.**

# 2.0.4 — NSE closing auction: trade until 15:29, square off by 15:40 (21 Oct 2026)
- Operator rule: last entry 15:29, square-off 15:38 (F&O closes 15:40 under the closing auction);
  the engine, the feed loop and the dashboard now run to the F&O close instead of 15:30.
- From 15:15 the NIFTY index is frozen / the indicative auction close (+435 pts on 21 Oct), so the
  engine's NIFTY spot = option put-call-parity forward (median of 3 strikes) - the median
  (parity - index) of the 5 minutes before 15:15. On the 21 Oct tape parity stayed within ~1 pt of the
  futures all through 15:15-15:30 (the future's own LTP sat still for 25 s, so parity is used in both
  basis modes). The NIFTY strike window now follows the future in the auction (centred on the
  indicative index it dropped every near-the-money strike at 15:20). Raw index kept in tick_tape;
  basis_tape gains index_spot, spot_source, parity_basis.
  No pre-freeze basis (restart after 15:15 with no tape) -> no closing-session entries.
- Expiry day: same times (operator decision), knowing the expiring options settle on the auction close
  and follow the indicative close after ~15:16 (19 Oct: 22900 CE 15 -> 46 -> 0.1 in ten minutes).
- Session times are resolved by date (closing_session.schedule): pre-CAS days keep 15:25; from
  7 Dec 2026 (SEBI 15 Oct circular) the auction is 15:30-15:40, index F&O close 15:45, expiring index
  contracts 15:30 (provisional -- re-check NSE's circular before then).
- Launcher stops relaunching from 15:38.

# 2.0.3 — fixes from the 21 Oct live tape (21 Oct 2026)
- No feed alarm for NSE's closing session: from 15:15 the NIFTY index freezes, then jumps to indicative
  values at 15:20 (seen on 19 and 21 Oct). Logged as CLOSING_SESSION_INDEX; no sound.
- Breeze silently refused every NEW subscription after 09:50 ("Could not authenticate credentials").
  NIFTY options are now subscribed ATM +/-14 up front; a NIFTY strike with no data 30 s after subscribing
  is reported (tape + console) and retried every 60 s instead of being assumed subscribed.
- Dashboard beacon and audio-resume are driven by the data stream, so a minimised window still reports
  every 10 s (it was once a minute); launcher disables Chrome's intensive background throttling.
- The pre-cooldown cue says "cooldown ends in 10s" (the level can re-anchor at the cooldown end).
- Tape housekeeping: at a clean close the tick/level/basis/decision .jsonl sidecars (~9 GB/day) are
  deleted only when their .parquet reads back with the identical row count (tapes.drop_verified_jsonl).
- Trading rules unchanged.

# 2.0.2 — sound council fixes (20 Oct 2026)
- New attention-grabbing CC0 MP3 set (downloaded; credits in vajra/sounds/CREDITS.md); 1.0 sounds in vajra1_backup/.
- Removed every generated sound: the Python-made feed_alarm, browser tones, and Windows playback. Only MP3s play.
- Sound ON by default and stays on until the operator turns it off (no timers); the 1.0 setting key is ignored.
- Locked page: red banner + tab title, missed trade cues play on unlock; a click on the LOCKED pill never changes the mode.
- Priority player (exit > entry > alarm > approach), each cue's own length paces the next; no overlap.
- Files fetched no-cache and re-loaded when they change; broken file -> red banner + pill.
- Staleness judged on the browser clock; trade cues kept 120 s; first load plays the newest cue of the last 15 s.
- One tab plays (leader lease); every cue's outcome is beaconed; the engine console warns when no page can play.
- Engine: approach warning not used up while an entry is impossible; "entry possible" cue 10 s before a
  cooldown ends at a level; approach cooldown restarts on every trade and re-anchor; P&L of 0 = loss sound;
  feed alarm never swallowed by the 3 s gap; forced alarms on EXIT REJECTED and POSITION LEFT OPEN;
  09:20 "armed" bell (armed.mp3); cues pushed on the error/shutdown paths.
- run_vajra.ps1 opens the dashboard in a sound-enabled Chrome/Edge app window (-NoBrowser to skip).
- Trading rules unchanged.

# VAJRA changelog

## 2.0.1 — 2026-10-17 (independent 30-reviewer teardown)

A second, independent 30-member team tore 2.0.0 down one area each (reports in `docs/teardown/`).
No reviewer found a Monday blocker; every HIGH they reported is fixed below or listed as an operator
decision. Operator-locked rules were not changed (one 2.0.0 deviation was reverted).

**Reverted to the operator rule**
- NIFTY re-anchors every 20 min even while a trade is open (`freshness.hold_while_in_trade: "off"`).

**Money / safety**
- A NaN, zero or garbage option price can no longer size, fill or poison capital (a NaN ask sized
  30,769 lots; a 0.05 bid booked −₹98k). Fills outside 0.5–1.5× LTP fall back to LTP.
- `LIVE_ORDERS` needs the YAML boolean `true` **and** `LIVE_ORDERS_CONFIRM: "I UNDERSTAND REAL MONEY"`
  (a quoted "false" used to turn real orders ON). Verify gate 6 fails on anything but `false`.
- Same-day restart rebuilds capital, the open paper position, both cooldowns and the PAPER id counter
  from today's order tape (it used to reset to ₹1,00,000 and orphan the trade). `run_vajra.ps1`
  relaunches the engine if it dies during market hours.
- One exception in a round no longer ends the trading day: the round is skipped, logged, and the open
  position is still managed.
- A full disk no longer ends the session; tape errors are counted and shouted on the terminal.
- No fictional flatten: if NIFTY quotes stop > 60 s before the end, the position is left open with a
  CRITICAL message; an exit priced off a quote frozen > 30 s is flagged.
- The held strike keeps streaming even if spot runs past the ATM±6 window (it used to lose its stop).
- An exact +10% peak arms the trail (float edge). `last_entry` can never be later than the flatten.

**Feed-sanity gate (rewritten from measured tapes)**
- Jump test fires only when spot jumps and the option-implied future does not (real breakouts pass).
- Relative basis test (20 pts off its own normal for 2 s) catches garbage under the absolute limit.
- Recovery 60 s after frozen/implausible (2 s after a pure jump); frozen needs ≥ 20 unchanged rounds.
- Result on 14/15/16 Oct: one closure per day at 15:15–15:16, stays closed through 15:25, zero false
  alarms before 15:15 (2.0.0 re-opened three times inside the 14 Oct garbage).
- A re-anchor the gate blocked is done the moment it re-opens; "levels stale" now holds until a
  re-anchor actually succeeds.

**Sounds**
- Approach cue is an early warning at 15 pts (`sounds.approach_warn_pts`), silent when no trade is
  possible and on the entry round itself (it used to ring with every entry and during cooldowns).
- The first cue after an engine restart is no longer lost (per-process `boot_id`). Shutdown flattens
  get their exit cue. 1.x "sound on" migrates to "trades only".

**Dashboard**
- Never shows "BUY NIFTY …" while a gate blocks the entry (it says NO TRADE and why).
- Pre-open shows live spot and the seeded levels; date/expiry/regime badge from start-up; a closed
  trade shows as flat; EXPIRY badge notes the holiday shift; non-expiry badge is muted.
- Stream falls back to polling at once when closed or silent > 6 s; lag pill measures engine → browser
  and turns red "NO DATA"; heavy tables repaint with each full push.

**Events / tapes**
- Alert dedupe per event type (a persistent reject used to flood 2 events per round); feed build errors
  latched to once a minute; a fault that changes mid-episode is logged; REGIME stamped with round time.
- Reject rows written to order_tape at most once a minute per reason; P&L rounded to the paisa.
- Replay prefers the complete jsonl on a restarted day and falls back to it when the parquet is
  unreadable; the parquet queue is bounded.

**Operations / Windows**
- Ctrl+C / closing the console window stop cleanly (flatten + close tapes); QuickEdit disabled.
- Websocket silence watchdog: FEED_GAP + reconnect + re-subscribe.
- `.token` read BOM/UTF-16-safe; token source printed (never the value); taken port is reported;
  started after the close says so; < 20 GB free warns; year-end missing-holidays warning.
- Strike-step table complete for all 21 stocks; a contradicted table step is reported once.
- MTM non-expiry level: rounding can no longer widen past legacy or break the 25-pt floor; NaN VWAP
  is "missing", never a NaN level.

**End-of-day forensics (new)**
- `raw_ticks.parquet`: every broker websocket tick with its receive time (background thread, bounded).
- `round_tape.jsonl`: one line per engine round (lag, processing time, feed health, gate, cockpit state).
- `SOUND` events in `event_tape.jsonl`; `client_tape.jsonl`: the browser's own 10-s beacons (rounds
  painted, lag, sounds played, mode, hidden/locked).
- `tools/eod_report.py tapes/<date> --zip`: the EOD_REPORT.md and a forensics zip to send for analysis.

**Tests**: 101 → 143, including the reviewers' 19 end-to-end operator-rule tests.

## 2.0.0 — 2026-10-17

Built from a 30-role review of VAJRA 1.0 and the three recorded non-expiry days (14/15/16 Oct 2026).
Full reasoning and evidence: [`docs/REVIEW_DOSSIER.md`](docs/REVIEW_DOSSIER.md) and the reviewers'
files in [`docs/review/`](docs/review/).

### Result on the recorded days
See the 2.0.1 section above (full-day replays, all-in, no limits, 15:25 cutoff).

### Non-expiry trading (the "sitting idle" fix)
- **New regime** (`vajra/regime.py`): EXPIRY vs NON_EXPIRY, with trading days to expiry. The dashboard
  shows `NON-EXPIRY · T-n trading days · mtm band`.
- **NIFTY levels off expiry use the mark-to-market pain level** (`nonexpiry.band_method: mtm`). This is
  the spot at which the 1-ITM writers would be 20% underwater on their VWAP *now*. On the recorded days
  1.0 levels sat 90–132 pts away (approached in 0 of 33 twenty-minute windows); 2.0 levels measured 23/25/44/109/140 pts at the 10/25/50/75/90th percentile (teardown T26). On the trending side the level is usually the 25-pt floor (the option already trades above its VWAP), so off expiry the engine behaves as a ~18-25-pt momentum trigger in the trend direction. A level is **never further** from
  spot than 1.0 would have placed it (narrow-only), and never closer than 25 pts. `max_half_width_pts`
  adds an optional hard cap.
- **Expiry-day band formula is unchanged** (the exact 1.0 band call). Not everything on expiry day is
  unchanged: the strike bought (current ATM, switchable via `execution.strike_from`), the feed-sanity
  gate and the bug fixes below apply every day.
- Sizing is unchanged: **all-in on every trade** (capital + running P&L), no trade cap, no daily loss stop
  (operator-locked; an earlier 2.0 draft added these without approval and they were removed).

### Broken-feed protection
- **Feed-sanity gate** (`vajra/feed_health.py`, `feed_sanity:`). It trips on any of: NIFTY spot unchanged
  ≥ 30 s, |spot − implied future| > 45 pts, or a one-round jump > 40 pts. While tripped (and for 30 s of
  clean data after) there are **no new entries and no level re-anchors**. Open positions are still
  managed. On the recorded days it raised no false alarm: every fault began 15:15–15:16.
- An open position is managed (stops, 15:25 flatten) **every** round, even when that round's band or
  basis failed. 1.0 skipped those rounds.

### Correctness
- The entry buys the **current** ATM, not the ATM from when the levels were anchored (up to 20 minutes
  stale).
- A cooldown ending now re-anchors **before** the entry is evaluated.
- The interval re-anchor no longer moves the NIFTY band mid-trade (`freshness.hold_while_in_trade`).
- `trigger.pad_floor` is now tunable below 0.10. 1.0 silently clamped it.
- Code defaults match the operator yaml: 09:21, 660 s, 15 s, 1200 s. 1.0 fell back to
  superseded values whenever a key was missing.
- `/api/state` no longer returns HTTP 500 on NaN, which used to produce a false "DATA FROZEN".
- The startup banner no longer crashes on a non-UTF-8 Windows console.
- `replay.py`: streams the tape instead of needing 11–34 GB of RAM, no longer crashes on
  `holidays_verified`, and refuses to write into the recorded folder or to append into a used folder.
- `--mode replay` now writes to `tapes_replay/`. 1.0 renamed the recorded parquet to `.bak` and
  appended to the recorded jsonl.
- `--date` is refused in live mode.
- An undeclared event type is logged instead of ending the session. `post_cooldown` and the executor
  reject reasons are now declared in the schema.

### Sounds (the "ting, ting, ting" fix)
- The engine decides when a sound is due (`vajra/alerts.py`); the dashboard only plays engine-issued
  events, each exactly once.
- There are five cues: entry, exit-profit, exit-loss, NIFTY approach (once per approach episode, at
  most every 2 min) and feed alarm (at most every 5 min). There is a 3 s gap between cues; trade cues
  are never dropped.
- **Sister/stock breach sound removed.** It was most of the noise: 28 / 241 / 112 rings a day on the
  recorded tapes.
- New short sound set (0.34–0.94 s, loudness-matched), regenerable with `tools/make_sounds.py`. The six
  old 2.4–6 s jingles are retired.
- Per-kind on/off and cooldowns are under `sounds:`. Shift-click the 🔔 pill to audition all cues.
- **Player rebuilt on Web Audio**: sounds arrive with the push (no waiting for a repaint, works in a
  background tab), play from a queue (never overlap or cut each other off), and anything older than 20 s
  is skipped instead of played as a burst. The pill cycles **off → trades only → all cues**, and shows
  **🔒 sound LOCKED — click** when the browser has blocked audio (1.x kept saying "sound on").
- Verified in headless Chromium on the real 15 Oct 09:41–09:58 data: 8 sounds in 17 minutes, all
  trade-related (approach → entry → exit-profit per trade); zero sister/stock sounds.

### Live data to the browser (the "ticks arrive late / dropped" fix)
- **Push, not poll.** 1.x made the browser download the whole state (≈20–60 KB) every 250 ms and rebuild
  ~15 tables each time; slow repaints queued up, background tabs were throttled to 1/s (then 1/min) and
  one-round events were simply missed. 2.0 streams every engine round over Server-Sent Events
  (`/api/stream`): a ~5 KB "hot" update per round, the full state once a second, a keep-alive every 2 s.
  Repaints are coalesced to one per animation frame; the heavy tables repaint at most once a second.
  Polling remains as an automatic fallback.
- **Engine stalls removed from the hot path.** The tape writer used to flush the disk after every row
  (~50 per round) and convert parquet on the engine thread; it now flushes once per round and writes
  parquet on a background thread. Python garbage-collection pauses are frozen out at live start.
- Measured on real 15 Oct data at live pace (`tools/latency_bench.py`): rounds never delivered
  0.5% → **0%**; delivery delay median/p95/worst 38/68/264 ms → **8/18/20 ms**; payload 18 → 5 KB;
  engine round p99/worst 20/250 ms → **10/82 ms**. In headless Chromium the page painted every pushed round.
- Header shows a live **lag pill** (`live 8 ms · paint 1 ms`) so you can see delivery health at a glance.

### Dashboard
- A **WHY NO TRADE** strip shows the blocking gate, the nearer level and its distance (pts and %), the
  band width compared with what 1.0 would have used, the re-anchor countdown, trades against the cap,
  and the last-entry time.
- A feed-health chip goes red on FROZEN or IMPLAUSIBLE.
- The proximity colours follow the engine's real entry distance instead of hard-coded 2/5 pts.
- The polling loop has an in-flight guard.
- `/docs` is off, and `/api/health` reports the feed state.

### Universe and weights
- Universe unchanged (operator-locked: 3 indices + 21 stocks). TCS now has a weight row (UNVERIFIED — check
  it against the NSE factsheet).
- Strike steps for 17 stocks are a measured table in the config and are never auto-changed (1.0's runtime
  "corrections" got 8 of 17 wrong); the remaining 4 still resolve at runtime.
- Sister levels round to 0.1 × their own strike step (1.0 rounded every instrument to 5 pts, so low-priced
  stocks' bands were "born breached"). Sister re-anchor stays at the operator's 3 minutes.

### Event hygiene
- REVERSAL_WATCH needs |bias| ≥ 0.125 and fires at most once every 5 min (was 1,351 and 821 a day).
- FALSE_BREAK fires at most once a minute.
- APPROACH_ALERT, ENTRY_REJECTED and EXIT_REJECTED fire on change or once a minute.
- The INSTRUMENT_ERROR latch has been fixed.

### Tapes (schema 2.0.0)
- `level_tape` gains `regime, band_method, legacy_upper, legacy_lower, narrowed`.
- `decision_tape` gains `regime, feed_ok, nearest_level_dist`.
- New events: `REGIME`, `FEED_HEALTH`, and `FEED_GAP` with `stage=feed_sanity`.
- `tick_tape` is unchanged, so 1.0 tapes replay through 2.0.

### Operations
- The daily token no longer needs a `.py` edit. Use `--token <apisession>` (or `-Token` in PowerShell),
  env `VAJRA_SESSION_KEY`, or a one-line git-ignored `vajra/.token`.
- `run_vajra.ps1` is ASCII-only (PowerShell 5.1-safe), adds `-SkipVerify` for mid-day restarts, and
  refuses `-Date` in live mode.
- The prev-close seed reads the previous day's **parquet** and skips garbage rows. 1.0 needed the
  multi-GB jsonl, which the manual says to delete.
- Verify gate 7 replays a bundled 2 MB real fixture from 15 Oct and asserts the non-expiry behaviour.
- Tests: 77 → 140 (2.0.1).

### Not changed (operator-locked)
- Expiry-day band formula, 7-pt anticipatory entry and the "no veto" rule.
- Exit ladder: −12% hard stop, +10% arm, +7% floor, 6% give-back.
- 15:25 flatten, 11-min and 15-s cooldowns, NIFTY-only execution, paper-always.
- 20-minute NIFTY re-anchor.
