# VAJRA 2.0 — Operator's Manual

*Last updated: 2026-10-17 (VAJRA 2.0 — see [CHANGELOG.md](CHANGELOG.md)). The complete guide to running, maintaining, and analyzing the VAJRA
pin-risk / gamma-explosion paper-trading engine. For the short pre-open checklist, see
[RUN_LIVE.md](RUN_LIVE.md); this document is the full reference.*

---

## 1. What VAJRA is

VAJRA watches NIFTY plus 23 other instruments (BANKNIFTY, FINNIFTY, 21 heavyweight stocks) live via
the ICICI Breeze feed, computes **pin-risk trigger levels** from ITM option VWAPs, and **paper-trades
NIFTY options** on anticipatory breaches of those levels. It never places a real broker order
(`LIVE_ORDERS: false` — permanent); the human operator mirrors its paper trades manually on ICICI.
Everything it sees and decides is recorded to per-day **tapes**, which are the binding audit record.

**Key strategy facts (operator-locked):**
- Executes **NIFTY only**; everything else feeds the advisory cross-instrument confluence/lean.
- All-in sizing on EVERY trade, every regime: `lots = floor(capital / (premium × 65))`, capital = ₹1,00,000
  starting + running P&L (compounds through the day). No trade cap, no daily loss stop (operator-locked).
- Entry: **no veto, no latch** — enters on every approach within 7 pts of a held level, 09:21 until the
  15:25 flatten.
- Exits: trail arms at +10% peak, **flat 6% give-back**, +7% minimum-profit floor, hard stop −12%
  (pre-arm only), force-flatten 15:25. **No breakeven.**
- Cooldowns: **11 min** after a hard-stop loss (loud violet countdown banner); **15 s silently**
  after a profitable exit. Fresh levels recompute after every exit AND when each cooldown ends.
- Levels: NIFTY re-anchors every **20 min** (+ post-exit/post-cooldown; not mid-trade); sisters/stocks every **3 min**.
- Premium floor 45 (non-expiry) / 30 (expiry); expiry-day-only nearest-ITM fallback.
- **2.0: non-expiry days trade too.** Off expiry the NIFTY levels are the *mark-to-market pain level*
  (§1a below): median ~44 pts from spot (often exactly 25 on the trending side) instead of 1.x's 90–135, never further than 1.x, never
  closer than 25 pts. Sizing stays all-in; no limits.
- **2.0: feed-sanity gate** — frozen spot ≥ 30 s, |spot − implied future| > 45 pts or a 40-pt one-tick
  jump pauses entries and re-anchors (the Breeze cash feed froze 15:15–15:20 on 14/15/16 Oct).

### 1a. Non-expiry regime (new in 2.0)
On a non-expiry day the 1-ITM option still carries 70–130 pts of time value, which is why the 1.x band
(`K ± 1.1·VWAP − basis`) sat 90–135 pts away and NIFTY touched it about once in three sessions. 2.0 asks
the same question the expiry-day rule asks — *where do the strongest writers start hurting?* — but at
today's clock: the spot at which the 1-ITM option would trade `mtm_pad` (20%) above its session VWAP now,
`UPPER = S + max(((1.2·VWAP_CE − LTP_CE)/Δ_CE), 25)` (lower symmetric). With zero time value this
equals the 1.x formula, so expiry day is unaffected (it runs the exact 1.x code).

| key (`vajra_config.yaml → nonexpiry:`) | default | meaning |
|---|---|---|
| `band_method` | `mtm` | `legacy` restores 1.x levels off expiry |
| `mtm_pad` | 0.20 | how far underwater the writers must be (higher = wider band, fewer trades) |
| `mtm_floor_pts` | 25 | a level is never closer than this at anchor time |
| `max_half_width_pts` | 0 (off) | optional hard cap on distance from spot |

Evidence: see the replay table in CHANGELOG.md. Three days is not proof; tune on more tapes.

### 1b. Sound policy (new in 2.0)
The engine decides when a sound is due; the dashboard only plays it, once. Five cues:
**entry** (rising marimba), **exit profit** (soft chime), **exit loss** (low thud), **NIFTY approach**
(soft tick, high = upper / low = lower; once per approach episode, max every 2 min) and **feed alarm**
(two low buzzes; max every 5 min). Sister/stock breaches never make a sound. Switch any cue off with
`sounds: <kind>: "off"`; shift-click the 🔔 pill to hear all five. Click the pill to cycle
**off → trades only → all cues**; if it shows **🔒 sound LOCKED — click**, the browser has blocked audio
until you click anywhere on the page (a browser rule after every page load).

---

## 2. Daily operation

### 2.1 Before 09:15 IST — get today's Breeze session token (the #1 failure point)
The API key/secret are long-lived; **the session key expires daily.**
1. Open the Breeze login URL with the api_key (see the docstring in `vajra/credentials.py`).
2. Log in → the redirect URL contains `apisession=XXXXXXXX`.
3. 2.0: pass it at start — `.\run_vajra.ps1 -Token XXXXXXXX` (or `--token` / env `VAJRA_SESSION_KEY` /
   a one-line `vajra\.token` file, which is git-ignored). Editing `SESSION_KEY` in credentials.py still works.

### 2.2 First time only: set up the 2.0 folder
2.0 lives in its own folder (`D:\ClaudePinRiskImproved`). Before the first live run, copy from the 1.0
folder (`D:\ClaudePinRisk`): `vajra\credentials.py` (your Breeze keys) and the most recent recorded day
under `tapes\` (e.g. `tapes\2026-10-16\`, for the pre-open level seed). Without the tapes folder the
engine still runs, but logs CRITICAL "no prev-close seed" and shows no levels until the 09:20 calc.
Do not `pip install -U` the packages the weekend before a live day.

### 2.3 Start (09:10–09:15)
```
cd D:\ClaudePinRiskImproved
.\run_vajra.ps1 -Token XXXXXXXX            # add -SkipVerify on a mid-day restart
```
Then open **http://127.0.0.1:8766** in ONE browser tab (each tab holds one of the browser's 6 connections to the engine; a 7th tab will not load) — the header **lag pill** should read `live <n> ms` (data is pushed
to the page every round; `poll` means it fell back to polling, red means delivery is late) — and
**click the 🔔 sound pill once** (browsers require one click
to unlock audio; the setting persists across refreshes and the pill pulses amber if muted during
market hours). Shift-click it to audition the five cues.

### 2.4 What a healthy startup looks like
- Terminal banner: mode/date/expiry/basis, `LIVE_ORDERS=False`, cadence 0.25s.
- Dashboard banner: `PRE-OPEN — engine live · levels seeded from prev close · first calc 09:20 · trading 09:21`.
- Within ~60s of the 09:15 open, expect `STEP_RESOLVED_LATE` / `STRIKE_STEP_CORRECTED` events in the
  strip — that is the strike-grid **self-healing working**, not an error.
- Timeline: 09:20 first level calc → 09:21 first possible trade → 14:30 caution window →
  15:25 flatten → 15:30 self-stop, finalize, tapes closed.

### 2.4 During the session — the banner is the single source of truth
| Banner | Meaning | Action |
|---|---|---|
| gray `PRE-OPEN — engine live…` | before 09:20, levels seeded | none |
| `STAND-BY / WATCHING …` | tracking, no level in range | read the **WHY NO TRADE** strip below the banner: blocking gate, nearest level + distance, band width vs 1.x, re-anchor countdown |
| red `FEED SPOT_FROZEN / SPOT_IMPLAUSIBLE` chip | NIFTY cash feed frozen / garbage — entries + re-anchors paused, open trade still managed | check the ICICI price yourself; it clears 30 s after the feed is sane |
| amber pulsing `⚠ … APPROACHING` | spot within ~5 pts of a level | attention |
| green/red flashing `🚨 BREACHED …` | level broken | attention |
| blue `IN TRADE — … · stop in X pts · held Xm` | open paper position | mirror/manage on ICICI |
| flashing + `⚠ FEED` prefix while in trade | feed gap / exit rejection **during an open trade** | check ICICI position manually |
| violet flashing `⏸ POST-STOP COOLDOWN — Xm remaining` | deliberate 11-min pause after a stop | none — this is intended |
| red `🚨 DATA FROZEN …` + dimmed page | engine dead or feed stalled | check the engine terminal / restart |
| gray `SESSION CLOSED — last round 15:29:59` | normal after 15:30 | none |

Mid-session restarts (2.0.1): tapes back up (never truncate), and the engine rebuilds **capital
(start + today's booked P&L), the open paper position and both cooldowns** from today's order tape
(terminal line `vajra · RESTART: capital Rs …`, event `RESTORED`). The one thing it cannot rebuild is the
open trade's running peak, so a trail that was already armed restarts from the entry price (the -12%
hard stop applies again until +10% is re-reached). `run_vajra.ps1` relaunches the engine by itself if it
dies during market hours. Stop it with ONE Ctrl+C (it flattens and closes the tapes); closing the
window also stops it cleanly.

### 2.5 After the close — the end-of-day forensic pack (2.0.1)
Everything needed to audit the day is written while it runs, in `tapes\<date>\`:
- `tick_tape.parquet` (every round's chains — replays the engine exactly), `level/basis/decision/signal/order_tape.parquet`
- `raw_ticks.parquet` — **every broker websocket tick with its receive time** (feed latency, silences, garbage prints)
- `round_tape.jsonl` — one line per engine round: timing, feed health, gate, what the cockpit showed
- `event_tape.jsonl` — every event, including every **engine sound event** (`SOUND`)
- `client_tape.jsonl` — what the **browser** painted and played (a beacon every 10 s from the open page)

Then run `python tools\eod_report.py tapes\<date> --zip`. It writes `EOD_REPORT.md` (rounds, gaps, feed
episodes, trades, engine sounds vs browser sounds, rounds pushed vs painted, level anchors) and a
`<date>_forensics.zip` (~100–500 MB) that holds everything needed to replay and audit the day. Send that
zip to be analysed. The multi-GB `tick/level/basis_tape.jsonl` sidecars are not needed (the parquet copies
replay byte-for-byte) and can be deleted once the parquet files are confirmed readable.

### 2.6 After the close (housekeeping)
- Tapes live in `tapes/<YYYY-MM-DD>/` (see §5).
- Review the day in the browser: `python replay_browser.py <YYYY-MM-DD>` → http://127.0.0.1:8770.
- Headless replay / what-if: `python replay.py --tape tapes/<date> --out tapes_replay [--set key=val]`
  (2.0 streams the tape and never writes into `tapes/`).

---

## 3. Expiry & holiday maintenance  ← how expiry dates actually work

**You never type expiry dates anywhere.** VAJRA computes them from three rules in
`vajra_config.yaml` → the `expiry:` block:

1. **Weekly expiry weekday** — `weekly_weekday: TUE`. The active weekly expiry is the next Tuesday
   on/after the session date.
2. **Monthly expiry** — always the **last** `weekly_weekday` of the calendar month (no setting;
   derived). The week where weekly == monthly runs ACTUAL basis; all other weeks run IMPLIED.
3. **Holiday shift** — if the computed expiry lands on a listed holiday (or weekend), it shifts to
   the **previous trading day** (`shift_if_holiday_to: prev_trading_day`).

So the only things you ever maintain:

### 3.1 If NSE changes the weekly expiry weekday again (e.g. Tuesday → Thursday)
Change **one line** in `vajra_config.yaml`:
```yaml
expiry:
  weekly_weekday: THU        # was TUE
```
That automatically moves the weeklies AND the monthly (last-THU of the month) AND all holiday shifts.
Verify with:
```
python -c "from datetime import date; from vajra.config import load_config; from vajra.app import build_calendar; \
r = build_calendar(load_config('vajra_config.yaml')).resolve(date(2026, 8, 6)); \
print(r.expiry_date, r.is_monthly, r.is_expiry_day, r.basis_mode)"
```

### 3.2 Every new year — add the NSE holiday list
Add a new block next to `holidays_2026` (any key starting with `holidays_` **containing a list** is
merged in):
```yaml
expiry:
  holidays_verified: true       # set true ONLY after verifying against the official NSE circular
  holidays_2026: [ ... ]        # keep old years; they are harmless
  holidays_2027:
    - 2027-01-26   # Republic Day
    - ...          # every WEEKDAY closure from the official NSE circular
```
Rules of thumb:
- Only **weekday** closures matter (weekends are excluded automatically).
- **Tuesday holidays are the critical ones** — each shifts that week's expiry to Monday, and a
  Tuesday holiday that is the LAST Tuesday of a month shifts the **monthly** expiry.
- After editing, set `holidays_verified: true` only if you cross-checked the official circular —
  while `false`, the engine prints a loud warning at every start (by design).
- 2026 note: the current list was verified 2026-07-02 against two published copies of the NSE
  circular; the five expiry-shifting Tuesdays (Mar-31, Apr-14, Oct-20, Nov-10, Nov-24) are annotated
  in the config.

### 3.3 If NSE announces a surprise mid-year holiday
Add the single date to the current year's list, save, restart the engine. Nothing else.

### 3.4 What consumes these rules downstream (automatic — listed so you know the blast radius)
Expiry resolution drives: which **option contracts** are subscribed (NIFTY = weekly; BANKNIFTY /
FINNIFTY / all stocks = **monthly-only** since NSE's Nov-2024 change — handled in code), the
ACTUAL-vs-IMPLIED basis mode, the expiry-day premium floor (30 vs 45) and ITM fallback, and the
non-trading-day guard (the app refuses to run live on a weekend/holiday).

---

## 4. Configuration reference (`vajra_config.yaml`)

**Operator-locked strategy (do not change casually — every value below was set by explicit
operator decision, several from live-tape sweeps):**
| Key | Value | Meaning |
|---|---|---|
| `execution.LIVE_ORDERS` | `false` | paper-always; never flip without a full risk review |
| `execution.lot_size / starting_capital / deploy_full_capital` | 65 / 100000 / true | all-in sizing |
| `execution.min_profit_pct / trail_activate_pct / trail_give_pct*` | 0.07 / 0.10 / 0.06 flat | exit ladder (adaptive tiers exist in code but are set flat by the 06-30 sweep) |
| `execution.hard_sl_pct` | 0.12 | hard stop before the trail arms |
| `execution.post_stop_cooldown_secs` | 660 | 11-min pause after a stop-loss (loud) |
| `execution.post_profit_cooldown_secs` | 15 | 15-s silent pause after a win (skips the same-second re-buy) |
| `execution.min_premium_nonexpiry / _expiry` | 45 / 30 | premium floors |
| `execution.flatten_before` | 15:25 | force-flatten time |
| `entry.proximity_pts` | 7 | anticipatory entry distance |
| `expiry.calc_start / trade_start` | 09:20 / 09:21 | session gates |
| `freshness.hold_min_secs / sister_hold_min_secs` | 1200 / 180 | 20-min NIFTY / 3-min sister re-anchor |

**Infrastructure (safe to touch):** `server.port` (8766), `tape.root_dir`, `feed.poll_interval_secs`
(0.25 = 4 rounds/sec), `breeze.reconnect_*`, `breeze.isec_override` (verified broker-code fallbacks),
2.1: `feed.ws_silent_secs` (20) / `feed.ws_reconnect_every_secs` (30) / `feed.sdk_reconnect_grace_secs` (10)
for the websocket supervisor, `tape.rotate_secs` (60) / `tape.sidecar_jsonl` (small) / `tape.compression` (zstd).

**Provisional (calibrate someday):** `decision.bias_threshold*` — advisory-only placeholders; the
engine flags them at startup until `thresholds_calibrated: true`.

**Weights:** `nifty_weights.csv` holds each instrument's cross-instrument decision weight. Refresh
from the NSE factsheets after each semi-annual rebalance (cut-offs ~Jan 31 / Jul 31). A stock listed
in the universe but missing here (currently TCS) contributes zero confluence voice and triggers a
loud startup warning + tape event.

---

## 5. Tapes — the binding record

Written per day to `tapes/<YYYY-MM-DD>/`, as parquet (analysis); the small tapes (decision / order /
signal) also keep a jsonl sidecar. 2.1: during the session each parquet tape is a run of closed 60-second
part files merged at the close (a killed session loses at most a minute; leftovers merge at the next start,
or with `python tools/repair_tapes.py tapes/<date>`). The tick/level/basis jsonl sidecars of 2.0 (~9 GB/day)
are off by default (`tape.sidecar_jsonl: all` brings them back).
| Tape | Contents |
|---|---|
| `tick_tape` | every round: spot/future/vix + full ATM±6 option chain (ltp/vwap/oi/vol/bid/ask) per instrument |
| `basis_tape` | per-round basis, d_basis, abrupt flags per instrument |
| `level_tape` | pin-risk U/L levels every round + every recompute trigger (`open/interval/post_exit/post_cooldown/prev_close_seed`) |
| `decision_tape` | per-round decision + reason + bias/conviction + instrument vector |
| `signal_tape` | one row per FIRED entry signal |
| `order_tape` | every paper ENTRY/EXIT/REJECT with fills and P&L (absent on a zero-trade day — correct) |
| `event_tape.jsonl` | startup config snapshot, expiry resolution, feed gaps, anomalies, alerts, shutdown |

**Disk management (important):** one live day ≈ **8 GB**, dominated by `tick_tape.jsonl` (~6.5 GB;
its parquet twin is ~0.4 GB and holds the same data). There is no auto-rotation. Periodically delete
old `tick_tape.jsonl` files (keep the parquet) or archive whole day-folders; never touch today's.
A same-day restart renames existing parquet to `*.bak` automatically (never truncates).

---

## 6. Analysis & replay tools

| Tool | Use |
|---|---|
| `python replay_browser.py [date] [port]` | watch any recorded day play through the CURRENT code in the browser cockpit (default 2026-06-30, port 8770); writes no tapes |
| `python replay.py --tape tapes/<date> [--set key=val]` | headless deterministic replay of a recorded tape into `tapes_replay/` (supports config overrides for what-if runs) |
| `python demo_dashboard.py [date]` | paced looping demo of the dashboard on synthetic/recorded data (dev-only) |
| `python -m vajra.app --mode replay --date <date>` | engine-native replay of a taped day (2.0: output goes to `tapes_replay/`, the recorded day is never touched) |
| `python verify_vajra.py` | the 7-gate acceptance harness (schemas, golden formulas, expiry mapping, determinism, non-expiry on real data) — run after ANY code change; must print ALL GATES PASS |
| `python -m pytest -q` | full regression suite (102 tests) — same rule |
| `vajra/sounds/*.mp3` | the alert sounds (CC0 MP3s, see CREDITS.md); replace any with your own MP3 of the same name |

The replay engine is deterministic: identical tape + identical config ⇒ byte-identical decisions.
That is what makes parameter sweeps on recorded days meaningful (see the 06-30 sweep notes in
ITERATIONS_LOG.md / config comments).

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Spot blank after 09:15 | stale SESSION_KEY | refresh token (§2.1), restart |
| red DATA FROZEN banner | engine died / feed stalled | check terminal; restart is safe |
| `SESSION_ERROR` at start | Breeze rejected login after retries | token again; check Breeze status |
| whole screen dim + red banner | same as DATA FROZEN | as above |
| Stock shows STALE badge all day | broker-side feed drop for that ticker (LT/INFY pattern) | nothing client-side; it stays visible and excluded from live signals |
| `STEP_RESOLVED_LATE` / `STRIKE_STEP_CORRECTED` events | strike-grid self-healing | none — working as designed |
| zero trades on a quiet day | read WHY NO TRADE: usually "waiting for NIFTY to reach a level" with the distance | correct if NIFTY never came within 7 pts; on a non-expiry day check `nonexpiry.band_method: mtm` |
| `MISSING_WEIGHT` warning | a universe stock has no row in nifty_weights.csv | add its factsheet weight |
| app refuses to start live | weekend/holiday guard | intended |

---

## 8. Sharing / backing up the code

Share everything **except**: `tapes/` (huge), `vajra/data/` (old recordings), `logs/` (SDK logs),
`__pycache__/`, `*.xlsx` outputs, and **blank the three secrets in `vajra/credentials.py` first**
(they are real broker credentials). Full list in the session notes; the code itself is a few MB.

## 9. Maintenance calendar

| When | Task |
|---|---|
| every trading day, before 09:15 | refresh `SESSION_KEY` |
| weekly-ish | check disk usage of `tapes/` (§5); archive/trim old jsonl |
| after each NSE index rebalance (~Feb / ~Aug) | refresh `nifty_weights.csv` from the new factsheets (add TCS's weight while there) |
| every December | add `holidays_<nextyear>` from the official NSE circular; keep `holidays_verified: true` honest (§3.2) |
| if NSE moves the expiry weekday | change `expiry.weekly_weekday` (§3.1) |
| after ANY code change | `python -m pytest -q` + `python verify_vajra.py` — both must pass before the next live session |
