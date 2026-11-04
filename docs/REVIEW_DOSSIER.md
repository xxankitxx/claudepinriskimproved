# VAJRA 2.0 — Review Dossier

A 30-role review team read the whole VAJRA 1.0 codebase (`D:\ClaudePinRisk`) and the three recorded
live days (Wed 14, Thu 15, Fri 16 Oct 2026 — all **non-expiry** days; the active expiry was Mon 19 Oct
because Tue 20 Oct is Dussehra). Each reviewer wrote a findings file; the 25 that were completed are
in [`docs/review/`](review/). This document is the synthesis: what was wrong, what changed in 2.0,
and what was deliberately not changed.

| # | role | file |
|---|---|---|
| R01 | gamma-trigger band quant (owner of the non-expiry narrowing design) | `01_trigger_band_quant.md` |
| R03 | freshness / re-anchor cadence | `03_freshness_reanchor.md` |
| R04 | basis engine + basis-change monitor | `04_basis.md` |
| R05 | OI magnets / pin locator | `05_oi_magnets.md` |
| R06 | breach + continuation validator | `06_continuation.md` |
| R07 | cross-instrument decision-maker / confluence | `07_decision_confluence.md` |
| R08 | orchestrator entry path + session lifecycle | `08_orchestrator_entry.md` |
| R11 | Breeze SDK wrapper | `11_breeze_wrapper.md` |
| R12 | live feed source + startup | `12_live_feed_startup.md` |
| R13 | expiry calendar + regime | `13_expiry_regime.md` |
| R14 | configuration + defaults | `14_config.md` |
| R15 | tape + schema | `15_tape_schema.md` |
| R16 | replay + verification tooling | `16_replay_verify.md` |
| R17 | dashboard state / API | `17_dashboard_state.md` |
| R18 | alert-sound policy | `18_sound_policy.md` |
| R19 | dashboard UX | `19_dashboard_ux.md` |
| R20 | sound assets | `20_sound_assets.md` |
| R21 | test suite | `21_tests.md` |
| R22 | static analysis / code quality | `22_static_quality.md` |
| R23 | documentation + operations | `23_docs_ops.md` |
| R25 | event hygiene | `25_events.md` |
| R26 | performance + storage | `26_performance.md` |
| R27 | determinism + replay parity | `27_determinism_parity.md` |
| R28 | sister / stock universe | `28_sisters.md` |
| R29 | synthetic data + fixtures | `29_synthetic_fixtures.md` |

R02 (options theory), R09 (execution/exits), R10 (feed integrity), R24 (risk) and R30 (chief
architect) were cut off by a model usage limit before writing their files. Their subjects are each
covered by two to four of the reports above (R01/R04/R13 for theory, R08/R17/R21 for execution,
R03/R04/R11/R15/R17/R29 for feed integrity, R01/R08/R21 for risk), and the lead took the
chief-architect role.

---

## 1. Why VAJRA sat idle on non-expiry days (operator complaint A)

**Root cause: the band was 90–135 pts from spot off expiry.** The level is
`K ± 1.1 × VWAP(1-ITM option) − basis`. On expiry day that VWAP is almost pure intrinsic, so the band
hugs spot (±25–35 pts). Off expiry the 1-ITM option carries 70–130 pts of time value, and every point
of it pushes the level further out. Because NIFTY re-anchors every 20 minutes *around the current
spot*, price had to travel ~100 pts inside one window to trigger:

| day | days to expiry | real range (excl. broken 15:15+ feed) | 1.x half-width (median) | legitimate approaches |
|---|---|---|---|---|
| 14 Oct | 3 | ~97 pts | 132 | 0 |
| 15 Oct | 2 | ~280 pts (trend) | 115 | 1 (10:00) |
| 16 Oct | 1 | ~80 pts | 90 | 0 |

`routing.nonexpiry_mode: trade_small` only opened the gate — it never sized anything down.

**Worse: four of the five recorded "trades" were taken on a broken feed.** On all three days the
Breeze cash feed froze from 15:15:00 to ~15:20 (spot unchanged ~290 s while options kept ticking),
then resumed with garbage (spot +185 / +240 / −134 pts in one tick while the option-implied future
moved < 10 pts; |basis| up to 283). VAJRA entered at 15:20 on every day and was flattened at 15:25.
It also re-anchored its levels on that garbage and, on 16 Oct, bought a strike 192 pts in the money.
There was no frozen-spot detector, no plausibility check and no last-entry cutoff.

## 2. Why it went "ting, ting, ting" (operator complaint B)

Almost all of it was **`sister_breach.mp3`**. Simulated on the tapes, the 1.x browser logic would have
rung it **28 / 241 / 112 times** on the three days. ITC, BHARTIARTL and BANKBARODA had degenerate
"born-breached" bands that flickered in and out every few seconds, and every 3-minute sister re-anchor
created a fresh "breach". The NIFTY approach cue — the one that matters before a trade — rang 1 / 0 / 0
times. On top of that, the six sound files were 2.4–6.0 s jingles with repeated hits inside each file,
at loudness levels 7 LU apart, and the browser decided what to play by diffing state every 250 ms
(missing some events and repeating others).

## 3. Correctness defects found (fixed in 2.0)

| severity | defect | found by |
|---|---|---|
| CRITICAL | trades + level re-anchors on a frozen / garbage cash feed (15:15–15:20, all 3 days) | R08 R03 R04 R10-area R29 |
| CRITICAL | no last-entry cutoff: entries allowed until the 15:25 flatten (entered 15:24:47, flattened 13 s later) | R08 |
| CRITICAL | `--mode replay` (the documented review command) wrote INTO the recorded day: renamed the recorded parquet to `.bak` and appended to its jsonl | R12 R16 R23 |
| CRITICAL | `replay.py` loaded the whole day into RAM (11–34 GB) — could not replay a real day | R16 |
| HIGH | an open position was NOT managed (no stop, no 15:25 flatten) on any round where the NIFTY basis failed | R08 |
| HIGH | the entry bought the ANCHOR-time ATM (up to 20 min / 2 strikes stale): 10-15 10:00 bought 22750 CE with spot at 22855 | R17 R29 R01 |
| HIGH | REVERSAL_WATCH flood (1,351 / 821 per day): 7.5-s rolling std window + any bias ≥ 0 counted as "prevailing" | R04 R07 R25 |
| HIGH | `trigger.pad_floor` below 0.10 silently ignored (`max(0.10, …)`), so the obvious narrowing knob was a no-op | R14 R01 |
| HIGH | code defaults disagreed with the yaml (09:30 vs 09:21, 180 vs 660 s, 120 vs 15 s, 1800 vs 1200 s): a missing key silently revived reverted rules | R14 R22 |
| HIGH | `/api/state` returned HTTP 500 on any NaN (e.g. a VIX tick without `last`) → false "DATA FROZEN" | R17 R19 R22 |
| HIGH | strike-step "self-healing" made 8 of 17 corrections WRONG in 3 days (step doubled → 2-ITM bands) | R28 |
| HIGH | decision weights fell back to BANK/FIN sub-index weights for non-NIFTY names (weights summed to 102.5%) | R07 R28 |
| HIGH | `⚡` banner crashed with UnicodeEncodeError on a non-UTF-8 console AFTER opening the tape and broker socket | R22 |
| HIGH | `replay.py` holiday fallback crashed (`'bool' object is not iterable`) | R13 R14 R27 |
| MEDIUM | cooldown-end round evaluated the entry on the OLD band, then re-anchored | R08 |
| MEDIUM | interval re-anchor moved the band mid-trade (contradicting "the band we entered on is the reference") | R03 |
| MEDIUM | INSTRUMENT_ERROR latch cleared every round (re-fired 4×/sec); APPROACH_ALERT / ENTRY_REJECTED re-fired every round on a persistent reject | R25 |
| MEDIUM | FALSE_BREAK fired 12× in 23 s while spot jittered at the level | R06 R25 |
| MEDIUM | `RECALC_TRIGGERS` lacked `post_cooldown`; four executor reject reasons undeclared | R15 R03 R21 |
| MEDIUM | an undeclared event type raised inside the round loop (would end the trading day) | R15 R25 |
| MEDIUM | dashboard hero banner used hard-coded 2/5-pt thresholds while the engine entered at 7 | R19 |
| MEDIUM | expiry badge showed calendar days (T-3 on Fri 16 Oct, really 1 trading day) | R13 R17 R19 |
| MEDIUM | dashboard polling had no in-flight guard (out-of-order paints) | R19 R26 |
| MEDIUM | `run_vajra.ps1` had non-ASCII text (PS 5.1 parse risk), ran the 20-s gate before every restart, accepted `-Date` in live mode | R23 R12 |
| LOW | `/docs`, `/redoc`, `/openapi.json` open; static `/api/health` | R22 R25 |
| LOW | prev-close seed read only the multi-GB jsonl (which the manual says to delete) and its "close" was the garbage tail | R12 R15 R29 |

## 4. What 2.0 does about complaint A — the non-expiry band

Five families were measured on the recorded chains (R01 §3, R03, R29, lead cross-check):

| candidate | result over the 3 days (NIFTY-only harness, locked exit ladder) | verdict |
|---|---|---|
| 1.x baseline | 1 trade | the problem |
| strip the time value (extrinsic) | band collapses onto spot, 11–17 trades/day | rejected |
| fixed ±N-pt breakout around spot | −₹37k | rejected |
| hard cap ±45–60 pts | 3–5 trades | kept as an optional extra (`max_half_width_pts`) |
| max-OI strikes as levels | walls flip with the window, sit as far as the band | rejected (R05) |
| **mark-to-market pain level (MTM)** | **15 trades, 9 wins, +₹34k (one trend day, two chop days)** | **shipped as default** |

**MTM**: the spot at which the 1-ITM writers would be `mtm_pad` (20%) underwater on their session VWAP
*right now*: `UPPER = S + max(((1+pad)·VWAP_CE − LTP_CE)/Δ_CE, 25)`, lower symmetric. It is the
1.x rule evaluated at today's clock instead of expiry's: with zero time value and delta 1 it equals
the 1.x formula exactly, so the theory (§2.5, strongest hands' pain) is preserved. 2.0 adds a
**narrow-only** guarantee: a level is never further from spot than 1.x would have put it.
**Expiry day is untouched** — it runs the exact 1.x call.

Honest caveat (R01, R07, R28): three days — one trend, two chop — cannot establish an edge. The MTM
winners came from the trend side; chop days lost. Parameters are config, and `tools/sweep` work on
more tapes is the way to tune them.

## 5. What 2.0 does about complaint B — sounds

* The **engine** decides when a sound is due (`vajra/alerts.py`); the page only plays events it has not
  heard before (sequence cursor — no replays after a refresh).
* Only five cues exist: **entry, exit-profit, exit-loss, NIFTY approach, feed alarm**. Sister/stock
  breaches never make a sound.
* NIFTY approach rings once per approach episode (hysteresis: spot must back off 8 pts past the entry
  distance to re-arm), at most once per 2 minutes. Feed alarm at most once per 5 minutes. A 3-s global
  gap between cues; trade cues are never dropped.
* New sound set: original synthesised cues, 0.34–0.94 s, loudness-matched, peak ≤ −2 dBFS
  (`tools/make_sounds.py` regenerates them). Downloading CC0 packs was attempted; every sound site
  (Kenney, Freesound, Pixabay, Mixkit…) returned 403 from the build sandbox, so the cues were
  synthesised instead (R20).
* Each kind can be switched off in `vajra_config.yaml → sounds:`; shift-click the 🔔 pill to audition.

## 6. Deliberately NOT changed (operator-locked or judged correct)

* Expiry-day band formula, half-up rounding, 7-pt anticipatory proximity, "no veto" entry, exit
  ladder (hard −12% before +10% arms; +7% floor; 6% give-back), 15:25 flatten, 11-min post-stop and
  15-s post-profit cooldowns, NIFTY-only execution, paper-always (`LIVE_ORDERS: false`).
* NIFTY 20-minute re-anchor (R01, R03: with a narrow band the re-centre is what keeps it reachable).
* Expiry calendar — R13 ran all 245 trading sessions of 2026 and checked the holiday list against the
  NSE circular: no bug.
* Cross-instrument confluence stays advisory (R07: no measurable predictive value on these days; do
  not give it a veto or sizing role until it is validated on ≥20 sessions).

## 7. Recommended next steps (not in 2.0)

From the reports, in priority order: tape the raw pre-filter snapshots and a `RUN_MANIFEST` so replay
reproduces live exactly (R27); persist position/capital/cooldowns across a mid-day restart (R08, R17);
reconnect-on-silence for the websocket and tick receive-timestamps in `breeze.py` (R11); smaller,
background-written tapes (R15, R26: 2.7–8 GB/day → ~0.1 GB); a parameter-sweep tool over many
recorded days (R16); a `holidays_2027` list before January (R13: 26 Jan 2027 is a Tuesday).
