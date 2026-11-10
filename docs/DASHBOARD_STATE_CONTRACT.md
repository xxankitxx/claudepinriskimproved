# VAJRA dashboard — data contract (2.1, with the 2.2 additions at the end)

The cockpit is a static page served by `vajra/dashboard.py` from `vajra/static/`:

| file | owner | role |
|---|---|---|
| `index.html` | UI team | markup only; must keep the element ids listed under **Fixed ids** |
| `styles.css` | UI team | all styling |
| `ui.js` | UI team | `window.VAJRA_UI.render(state, heavy)` — pure presentation |
| `data.js` | engine (frozen) | SSE/poll transport, watchdog, lag pill, client beacon; calls `VAJRA_UI.render` |
| `sound.js` | engine (frozen, "sound council" contract) | Web-Audio player for engine-issued cues |

`data.js` calls `window.VAJRA_UI.render(state, heavy)` on every coalesced repaint (`heavy` is true
at most once a second → repaint the big tables only then) and `window.VAJRA_UI.watchdog(status)`
whenever the feed watchdog changes (`status` = `{kind:'ok'|'closed'|'frozen', text, detail, lastRoundTs}`).
Everything else (pills in the header for sound/lag, the red locked banner) is driven by the frozen
scripts through the **Fixed ids**.

## Endpoints (unchanged from 2.0)
* `GET /api/stream` — Server-Sent Events: `hot` every engine round (~5 KB, no heavy keys), `full` at most once a
  second (whole state), `beat` every 2 s when idle. `data.js` handles it; falls back to polling `GET /api/state`.
* `GET /api/state` — whole state as JSON (NaN → null).
* `POST /api/client` — the page's 10-s beacon (data.js).
* `GET /sounds/<file>.mp3` — no-cache cue files (sound.js).
* `GET /static/<file>` — the page assets.

## The state object (`orch.latest_state`, produced in `vajra/orchestrator.py: _update_view`)

Top level:

```
round_ts            int ms epoch of the engine round (null before 09:20 = PRE-OPEN; then `preopen_ts` ticks)
preopen_ts          int ms (pre-open only)
session_date        "2026-11-03"
version             "2.0"            boot_id  "<pid>-<ms>" (changes on engine restart)
live_orders         bool             (always false = paper)
nifty               {...}            the NIFTY headline (below)
nifty_chain         [ {strike,is_atm,off,ce_ltp,ce_vwap,ce_oi,ce_vol,ce_bid,ce_ask,pe_ltp,pe_vwap,pe_oi,pe_vol,pe_bid,pe_ask} ]  ATM±6
arming              {...}            the SCREAM (below)
cooldown            {stop_cooldown_active: bool, stop_cooldown_remaining_secs: int}
burst_board         [ {instrument,spot,state,dir,dist_up,dist_dn,heat(0..1),breached:'UP'|'DOWN'|null,breach_ts,weight,strength,stale} ]  ranked
breach_alerts       [ same rows, only breached & live, newest breach first ]
events              [ {ts,type,summary,count} ]   last 40 engine events (FEED_GAP / ANOMALY / APPROACH_ALERT / ENTRY_REJECTED / EXIT_REJECTED / REVERSAL_WATCH / FEED_HEALTH / SOUND ...)
levels_stale        bool             held NIFTY levels failed to re-anchor (feed gap)
level_history       [ {ts,trigger,upper,lower,atm,k_ce,vwap_ce,k_pe,vwap_pe,basis,basis_mode,band_method,narrowed,legacy_upper,legacy_lower,anchor_spot} ]  last 80 recomputes
instruments         { NAME: {spot,delta,atm,future,basis,basis_mode,upper,lower,dist_up,dist_dn,breached,heat,breach_ts,state,dir,strength,max_oi_ce,max_oi_pe,vol_ratio,vix,weight,is_index,freshness_age_secs,stale?} }  all 24
decision            {decision:'FIRE_UP'|'FIRE_DOWN'|'HOLD', reason, bias_ewma, raw_bias, conviction, threshold, routing_mode, divergence, reversal_freeze, expiry_gate, channels[], lead}
position            null | {open,direction:'UP'|'DOWN',right:'CE'|'PE',strike,lots,qty_remaining,qty_total,entry_ts,entry_fill,last_premium,peak_premium,trail_floor,trail_armed,cost,unrealized_pnl,realized_pnl,exit_plan:{hard_sl,trail_activate,min_profit}}
capital             {starting,cash,unrealized,equity,pnl,pnl_pct,deployed,equity_high,dd_pct}
journal             [ {entry_ts,exit_ts,direction,right,strike,qty,lots,entry_price,exit_price,pnl,pnl_pct,exit_reason,status:'OPEN'|'CLOSED',verdict,same_side[],opp_side[],conflicted} ]  last 80 (open trade last, live)
trades_summary      {trades_today,closed,wins,losses,realized_pnl,win_rate_pct,streak,avg_win,avg_loss}
expiry              {date,basis_mode:'IMPLIED'|'ACTUAL',is_expiry_day,is_monthly,shifted_from_holiday}
regime              {name:'EXPIRY'|'NON_EXPIRY', dte_trading, is_expiry_day, band_method:'legacy'|'mtm', last_entry:'15:29', entries_today,
                     closing:{session_date,rule_from,index_frozen_from,fo_close,session_close,last_entry,flatten_before,is_expiry_day,spot_proxy}}
feed_health         {ok, reason:null|'SPOT_FROZEN'|'SPOT_IMPLAUSIBLE'|'SPOT_JUMP'|'RECOVERING', spot_unchanged_secs, basis}
idle                {gate, text, nearest_side, nearest_dist, nearest_pct, half_width, legacy_half_width, band_method, next_reanchor_secs, proximity_pts}   WHY NO TRADE
sound_events        [ {seq,ts,kind,text,prio} ]  (sound.js only)
tape_errors         int
ws                  null | {connected, sdk_reconnecting, reconnecting, tick_age_s, ticks, bad_ticks, tokens, connects, drops, connect_errors,
                            reconnects_ok, reconnects_failed, sub_errors, keepalives, last_error, silent, option_subs, nifty_silent_strikes, other_resubscribes,
                            backlog, backlog_max, inbox_dropped, fast_parsed, exch_lag_s, threads, stale_resubscribes}   (2.1, live only; 2.1.1 intake gauges:
                            exch_lag_s = NIFTY cash receive time minus exchange time, the "stuck ticker" number; backlog = packets waiting for the consumer)
round               {proc_ms, published_ms, gc:{gen,ms,count,max_ms}|null}      (2.1) engine work per round, when it was published, last GC pause (2.1.1)
power               null | {on_battery: bool|null, battery_pct: int|null, ac: bool|null}   (2.1, live on Windows; sampled every 30 s)
server_ms, sounds_rev, snd_fallback             transport fields (frozen scripts)
```

`nifty`:
```
spot, delta, future, future_mode:'ACTUAL'|'IMPLIED', basis, atm, vix, lower, upper, dist_up, dist_dn,
max_oi_ce, max_oi_pe, k_ce, k_pe, atm_ce, atm_pe,
calc {ts,trigger,upper,lower,atm,k_ce,vwap_ce,k_pe,vwap_pe,basis,...}, calc_prev,
held_age_secs, hold_secs (1200), spot_source:'INDEX'|'CLOSING_PROXY', index_spot, preopen (pre-open only)
```

`arming` (the SCREAM):
```
state: 'STAND_BY'|'WATCHING'|'ABOUT_TO_BREACH'|'BREACHED'|'IN_TRADE'
lean: 'UP'|'DOWN'|null      in_trade bool      message str      target: null|{action:'BUY',instrument,right,strike,label}
entry_block: null|'ENTERED'|'PREMIUM_TOO_THIN'|'INSUFFICIENT_CAPITAL'|'POST-STOP COOLDOWN 37s'|...
dist_to_level, side:'UPPER'|'LOWER', proximity_pts (7)
confluence {verdict:'STRONG'|'MILD'|'LONE'|'CONFLICTED', same_side[], opp_side[], confidence, conflicted, same_pull, opp_pull, net_pull, absolute_net_pull, pulls_by_instrument{}}
```

Gate codes (`idle.gate`, in order): POSITION_ACTIVE, BEFORE_TRADE_START, AFTER_LAST_ENTRY, NO_LEVELS, FEED_UNHEALTHY,
LEVELS_STALE, EXPIRY_GATE_BLOCKED, POST_STOP_COOLDOWN, POST_PROFIT_COOLDOWN, NO_APPROACH (null = an entry fired).

Instrument `state`: NEUTRAL, APPROACH_UP/DOWN, BREACH_UP/DOWN, CONFIRM_UP/DOWN, N/A.
Exit reasons: TRAIL_SL (profit trail), SL_HIT (hard stop −12%), EXPIRY_FLATTEN (square-off), END_OF_SESSION.

## Fixed ids (the frozen scripts write into these; keep them in index.html)
* `sndbtn` — the sound pill (text/class/title set by sound.js; click handler attached by sound.js)
* `sndbanner` — the fixed red "SOUND LOCKED / broken file" banner (sound.js shows/hides it)
* `lagpill` — delivery lag pill (data.js writes text + class `feed-ok|snd-muted|feed-bad`)
* `document.title` is managed by sound.js (lock state prefix)

Class names the frozen scripts apply: `pill`, `snd-on`, `snd-muted`, `snd-bad`, `feed-ok`, `feed-bad`, and
`body.frozen` (data.js adds it while the watchdog says FROZEN; style it as a dimmed page).

## Operator rules the design must honour
* The NIFTY level band, spot, distance-to-level, the SCREAM, the open position (distance to the next stop) and
  WHY-NO-TRADE are the glance-critical items: above the fold at 1080p, readable from two metres.
* Post-stop cooldown = violet, standing banner for the whole window (never red/green). Post-profit cooldown is SILENT (no banner).
* A frozen engine / stalled feed must be unmissable (watchdog `frozen`), a closed session calm (`closed`).
* Never show "BUY NIFTY …" while a gate blocks the entry (`arming.target` is already null then; `entry_block` says why).
* Level history goes at the very bottom. Sister/stock breaches never make a sound.
* Times are IST (the machine clock); format `HH:MM:SS`; money in ₹ with Indian grouping (`toLocaleString('en-IN')`).

## 2.2 additions (feed lateness, entry guards, journal context, stop ladder)

Everything below is optional: any field may be null / absent (replay has `ws: null` and `feed_lag.lag_s: null`);
`ui.js` then renders a dash or nothing, never `undefined` / `NaN`.

State fields `ui.js` now reads:

```
ws.exch_lag_s, ws.drops, ws.backlog      header pill `exlagpill` ("feed 1.2 s late": calm < 2 s, amber 2-6 s, red > 6 s or while closed;
                                         drops + backlog in its tooltip). Falls back to feed_lag.lag_s when ws has no number.
feed_lag            {closed: bool, limit_s: number, lag_s: number|null}
                    closed=true -> the standing `lagbanner` "ENTRIES PAUSED: NIFTY feed is N s behind the exchange";
                    with idle.gate FEED_UNHEALTHY and feed_health.ok != false the WHY-NO-TRADE text says "entries paused - NIFTY feed N s late".
guards              {retreat_secs, retreat_pts, momentum_min_pts, rebuy: bool, max_breach_pts,
                     rebuy_wait: null|{dir:'UP'|'DOWN', beyond}, breakeven: null|{arm_pct, lock_pct}}   (0 / false / null = off)
                    -> the quiet `guards` line under WHY-NO-TRADE: "momentum: +3 pts / 30s" (momentum_min_pts > 0) or "no-retreat 30s",
                       "re-buy needs new high/low" (or "re-buy ▲ needs NIFTY above 24,950" while rebuy_wait is set), "max breach N pts",
                       "protective stop +6% → +1%", "late-feed pause 6 s" (from feed_lag.limit_s). The guard holding an entry right now lights up.
journal[].peak_pct, .low_pct             option premium best / worst vs entry, in % (closed rows and the live open row)
journal[].ctx       null | {move_30s, move_120s, secs_since_exit, after:'profit'|'stop'|null, exch_lag_s, breached: bool}
                    -> context chips: "re-buy 15s after profit" (same side as the previous trade; "flip ..." otherwise; only within 15 min),
                       "40s after stop", "feed 8s late" (>= 2 s), "breached", "moving away −3.4" (move_30s <= −2); the rest is in the tooltip.
position.exit_plan  {hard_sl, trail_activate, min_profit, be_arm, be_lock, be_armed}
                    active stop = hard_sl -> be_lock once be_armed -> trail_floor once trail_armed (be_arm / be_lock null = rule off)
```

Gate codes added to `idle.gate` (after POST_PROFIT_COOLDOWN / NO_APPROACH): ENTRY_RETREATING, REBUY_WAIT, BREACH_TOO_DEEP.
They are "wait" states: WHY-NO-TRADE shows `WAIT — <reason>` in the cooldown violet (class `gate-cool gate-wait`), never as an error.
The reason is `arming.entry_block` when it starts with "WAIT" (displayed as it arrives), else a built-in sentence
(`idle.text` is ignored for these three gates). Exit reason added: BE_STOP ("Protective stop", coloured by the P&L sign).

New element ids owned by `ui.js` (none of them is touched by the frozen scripts):
* `exlagpill` — exchange-feed lateness pill (classes `xl-na|xl-ok|xl-warn|xl-bad`). `lagpill` stays data.js's delivery-lag pill.
* `lagbanner`, `lagbanner-text`, `lagbanner-sub` — the ENTRIES PAUSED banner (between the header and the SCREAM).
* `guards`, `guards-list` — the guards line.
* `ladder`, `lad-risk`, `lad-lock`, `lad-gap`, `lad-sl`, `lad-entry`, `lad-minp`, `lad-arm`, `lad-bearm`, `lad-belock`, `lad-floor`,
  `lad-peak`, `lad-now` (+ `-l` label spans), `lad-cap-stop`, `lad-cap-lock` — the stop ladder in the position card
  (static nodes; markers move by `transform` only).
* `jr-n`, `jr-real`, `jr-closed`, `jr-wl`, `jr-wr`, `jr-streak`, `jr-avgw`, `jr-avgl` — the journal day summary; `jr-spark` is the
  equity sparkline (redrawn only when the closed-trade set changes). `jr-summary` is gone. The journal table has 15 columns
  (entry, exit, hold, dir, strike, size, entry ₹, exit ₹, P&L %, P&L ₹, peak %, low %, exit reason, context, confluence).
