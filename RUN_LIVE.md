# VAJRA 2.0 — LIVE RUN CHECKLIST (trading day)

The whole live capture is built: `vajra/feed.py` (LiveBreezeSource) subscribes to Breeze for
**NIFTY + BANKNIFTY + FINNIFTY + the 21 stocks** — spot + near-month future + the full ATM±6
option chain (ltp / bid / ask / OI / volume) per instrument — and the orchestrator writes it all
to the 7 tapes. A live session captures EVERYTHING on real data (no Black-Scholes, no N/A — those
were only the old recorded-tape replay).

> READINESS (last confirmed 2026-07-01): the live path is PROVEN, not just hardened — real attended
> sessions on 2026-06-29 and 2026-06-30 produced real NIFTY spot, populated chains, and real round-trip
> trades (54 on 06-30 alone). Run it ATTENDED regardless; watch the dashboard, don't walk away.
> PAPER-ALWAYS: `LIVE_ORDERS=false` — VAJRA never places an ICICI order; you place orders yourself.

## 1. BEFORE 09:15 IST — refresh the Breeze session token  (#1 failure point)
API key/secret are long-lived; **SESSION_KEY expires daily** and MUST be refreshed:
  1. Open the Breeze login URL with the api_key (see `vajra/credentials.py` docstring).
  2. Log in → the redirect URL contains `apisession=XXXXXXXX`.
  3. Start with it: `.\run_vajra.ps1 -Token XXXXXXXX` (2.0 — no file edit; env `VAJRA_SESSION_KEY` or a
     one-line `vajra\.token` also work).
Confirm the day is not an NSE holiday (the app now aborts on a non-trading day).

## 2. START (around 09:10–09:15)
```
.\run_vajra.ps1 -Token XXXXXXXX       # mid-day restart: add -SkipVerify
```
- Connects to Breeze, resolves stock codes at runtime, subscribes to all instruments.
- A startup banner prints: mode / expiry / basis / LIVE_ORDERS=false / dashboard URL.
- Dashboard: **http://127.0.0.1:8766** — open it and **click the 🔔 sound pill once** to unlock
  audio (browsers block sound until a click). 2.0 plays only five short engine-decided cues; shift-click
  the pill to hear them all.
- A terminal heartbeat prints every ~30 rounds (instr count · NIFTY spot · state · trades · events).

## 3. WATCH AT STARTUP (first ~15s)
- Terminal banner shows the right expiry + basis_mode and `regime EXPIRY` / `NON_EXPIRY`
  (STARTUP_CONFIG / EXPIRY_RESOLVED are written to the event tape, not the dashboard strip).
- NIFTY headline shows a **real spot** and a **populated option chain** within ~10s.
  - If spot stays blank → the token is stale (redo step 1) or the feed isn't flowing.
- Pre-open the band is **seeded from the previous recorded close** (operator rule a), so you'll see
  levels before 09:20; the first live recompute is at **09:20**.

## 4. TIMING (locked)
- **09:20** first intraday level compute · **09:21** earliest entry · NIFTY 20-min level interval;
  sisters/stocks re-anchor every 3 min.
- **Non-expiry day (2.0):** the header shows `NON-EXPIRY · T-n trading days · mtm band`. Levels are narrower
  (mark-to-market). Sizing is all-in as always; no trade cap, no daily loss stop.
- **Feed-sanity (2.0):** if the header chip turns red (`FEED SPOT_FROZEN` / `SPOT_IMPLAUSIBLE`), VAJRA has
  paused entries and level re-anchors because the NIFTY cash feed is frozen or printing garbage. That is
  expected around 15:15–15:20 on Breeze. An open trade is still managed.
- **15:25** flatten any open position · **15:30** the live loop stops itself, finalizes
  (force-flatten + SHUTDOWN), flushes + closes the tapes, disconnects Breeze. (Ctrl+C only if needed.)
- Exits: trailing (arms +10%, +7% min floor, **flat 6% give-back**) + hard stop (−12%). **No breakeven.**
  (The adaptive 12/9/6 tiers were tried 2026-07-01 and reverted 2026-07-02 — flat 6% beat them in every
  matched pairing on the binding 06-30 tape.)
- **Cooldowns:** after a hard-stop (SL_HIT) loss → no trade for **11 min** (loud, shown in cockpit);
  after a profitable exit → no trade for **15 s** (SILENT — skips the same-second re-buy, catches the
  trend's next leg; moved from 120 s after the 06-30 sweep). Fresh levels recompute when each ends.
- Premium floor 45 non-expiry / 30 expiry. **Expiry-day ITM fallback is ACTIVE** (decide keep/revert —
  on the 9-day backtest it doubled expiry trades and turned them net-negative).
- NOTE: a normal Monday is not a weekly-expiry day (NIFTY weekly expiry is Tuesday), BUT when the Tuesday
  is a holiday the expiry moves to Monday. **Mon 19 Oct 2026 IS an expiry day** (Tue 20 Oct is Dussehra) →
  floor 30, ITM fallback active, the locked 1.0 band formula. The dashboard badge says `⚡ EXPIRY DAY`.

## 5. DURING — read the dashboard
NIFTY band-hero panel (spot marker vs pins) · the scream · NIFTY lean (real sister/stock chains live)
· burst board · breach alerts · live-events strip (FEED_GAP/rejections) · trade journal · level-history
(bottom). A frozen dashboard clock = possible feed stall — cross-check the terminal heartbeat and
`tapes/<date>/event_tape.jsonl` for FEED_GAP rows.

## 6. AFTER THE CLOSE
- Captured session lives in `tapes/<date>/` (tick/level/basis/decision/signal/order parquet + event jsonl).
  `tick_tape` holds the full per-strike chains for all instruments. The console prints
  `tapes closed in N s` when the parts have been merged; if the engine was killed instead, leftover
  `*.part-*.parquet` files are merged automatically the next time anything opens the day
  (`python tools/repair_tapes.py tapes/<date>` does it by hand).
- Review: `python replay_browser.py <YYYY-MM-DD>` (cockpit) or
  `python replay.py --tape tapes/<YYYY-MM-DD> --out tapes_replay` (headless; never touches `tapes/`).

## Power (2 Nov 2026: the laptop switched off at 15:28 with a position open)
- **Plug the laptop in** and run on mains for the whole session. VAJRA blocks idle sleep while live and
  shouts (console, event, alarm cue, red POWER chip) the moment it runs on battery, but it cannot survive
  a power-off: a position open at that moment is NOT squared off. Set the plan once:
  ```
  powercfg /change standby-timeout-ac 0
  powercfg /change hibernate-timeout-ac 0
  ```
  and in Settings → Power → "When I close the lid (plugged in)" = Do nothing.
- **After a power-off / kill:** start again with `.\run_vajra.ps1 -Token <key> -SkipVerify`. The engine prints
  `the session ENDED ABNORMALLY (last round HH:MM:SS) -- A paper position was OPEN: ...` and RESTORES capital,
  the open paper position and the cooldowns from today's order tape, then manages / squares it off as usual.
  If it is already past 15:38, flatten the ICICI position by hand — nothing automatic can.
  Leftover `*.part-*.parquet` files merge by themselves at that start.

## Failure notes
- Breeze rejects the session → token stale; redo step 1.
- 2.1: the header's **socket chip** shows the broker websocket (connected / tick age / reconnects). A silence
  of 20 s logs `FEED_GAP ws_silent` and asks the supervisor thread to reconnect (the engine keeps running;
  open positions keep being managed on the last quotes, the feed-sanity gate blocks new entries). If the chip
  stays red for a minute and the console says the key may have been invalidated (a second Breeze login
  elsewhere kills the first), restart with the current key — nothing else recovers from that.
- 2.1: `FEED_GAP ws_reconnect ok:true resubscribed:N` means every stream token was re-joined in one packet.
- 2.1.1 (3 Nov): `ws_dropped transport error` every ~30 s while ticks still flow = the socket client idling out
  behind a tick flood; the keepalive (`feed.ws_keepalive_secs`) stops it -- if it still cycles, look for ANOTHER
  engine on this laptop (apex / mythos / trinetra feeds) using the same Breeze session key: one key, one socket.
- 2.1.1: a frozen NIFTY spot while the option chain ticks = the spot stream was not re-joined after a rebuilt
  socket. Fixed (the first subscription's token is now remembered); if it ever recurs, restart with -SkipVerify.
- 2.1.1 (evening): **the "stuck ticker" number is `exch_lag_s`** in `/api/health` -> `ws` (NIFTY cash: receive
  time minus exchange time). Healthy: under 1.5 s. If it climbs (it was 15-25 s all day on 3 Nov before the
  intake rewrite) the consumer is behind: check `ws.backlog` / `ws.threads` and the CPU of the OTHER engines on
  this laptop. `logs/engineio.log` says why a socket died: "WebSocket connection was closed" = the server kicked
  us, "packet queue is empty" = the client idled out, "Server has stopped communicating" = no data for 45 s.
- 2.1.1: keep the dashboard window VISIBLE (not behind the ICICI window / minimised): a hidden tab is throttled
  by Chrome to one paint a second and the beacons say `hidden:true`; on reveal the page repaints at once.
- 2.1.1: the first start of a day downloads the broker's 30 MB security master into `tapes/_cache` (minutes when
  ICICI is slow); every restart the same day uses the cache and reaches the first round in seconds.
- 2.2 (9 Nov tape): **the feed pill in the header ("feed 1.2 s late") is the number to watch.** It is how far
  the NIFTY tick stream is behind the exchange clock. Green under 2 s, amber 2-6 s, red above. Above
  `feed.max_exch_lag_secs` (6 s over the day's best reading) the engine PAUSES NEW ENTRIES, rings the feed
  alarm once and shows a red banner; open positions are still managed. It re-opens by itself below 3 s.
  On 9 Nov the stream ran 25-50 s late for two hours while other jobs on the laptop had the CPU; the socket
  reader now takes one large read per wake-up (`feed.ws_buffered_reader`) and the engine runs at
  above-normal priority (`runtime.process_priority`). If the pill still goes amber: stop batch jobs / replays
  of the other engines during market hours.
- 2.2: **the window can be closed as soon as "tapes closed" is printed.** The day's part files are merged by a
  background process (`logs/tape_merge.log`). If the laptop is shut down before it finishes, the next start
  finds the parts and merges them (`python -m vajra.tape_merge tapes/<date>` does it by hand).
- 2.2: WHY-NO-TRADE can now say **WAIT**: "NIFTY not moving toward the level yet" (momentum guard) or
  "re-buy needs NIFTY beyond 24,950" (after a profitable exit the same side is bought again only when NIFTY
  passes the previous trade's best price). These are waits, not skips: the entry fires when the condition
  clears. The guards line under the strip lists what is switched on; `entry.*` in vajra_config.yaml.
- 2.2 evening review: `python tools/trade_review.py <date>` prints every trade with how it was entered and
  the day grouped by entry type; `python tools/lab.py run <name> --set key=value` replays every recorded day
  through the real engine with one setting changed (`python tools/lab.py compare base <name>`).
- 2.2.2 (12 Nov): a reconnect whose re-join the server ignores now repairs itself within ~10-30 s (event
  `FEED_GAP ws_rejoin`, then `ws_streams_missing` + a socket rebuild if needed). If the NIFTY spot on the
  dashboard stops moving while options still update for more than a minute, that repair failed: restart
  with -SkipVerify. The four engines on this laptop share one Breeze session key (the session-key tool);
  heavy work on the laptop during market hours (replay batches, labs, test suites) slows every engine.
- A stock code won't resolve → runtime `get_names()` then the `breeze.isec_override` map; a fallback is
  logged (only that stock's bloc-bias degrades — NIFTY execution is unaffected).
- Verifiable pre-market: SDK installed, credentials present, live path imports, prev-close seed builds a
  real band. NOT verifiable until the feed is open: the actual websocket tick flow.
