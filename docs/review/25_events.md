# 25 — Event-tape hygiene & alerting architecture (R25): findings

Scripts: `/home/claude/vajra2_review/scratch/25/` (`ev_stats.py`, `rw_attr.py`, `rw_hyst.py`, `strip_sim.py`, `strip_alt.py`,
`sound_sim.py`, `err_latch_demo.py`). Everything below marked "confirmed-by-running" was produced by those scripts on the three tapes
(or on the synthetic generator for the one demo).

## 0. Call-site catalogue (every emitter)

`_emit_event` (orchestrator.py:230) = strip mirror + tape. `tape.event` direct = tape only (strip never sees it). `_feed_event` (feed.py:149) -> `orch._emit_event` via sink; it swallows ALL exceptions (feed.py:156).

| # | site | type / kind | payload keys | latch / edge logic | frequency risk |
|---|---|---|---|---|---|
| 1 | app.py:52-78 | STARTUP_CONFIG, EXPIRY_RESOLVED, HOLIDAY_DECISION, THRESHOLDS_PROVISIONAL, HOLIDAYS_UNVERIFIED | config snapshot / expiry fields | once per process start | 4 restarts on 10-16 = 4 copies (no session_id) |
| 2 | app.py:177,190 | SESSION_ERROR reason=missing creds / breeze_connect_failed | reason, detail | none (process exits) | low |
| 3 | orchestrator.py:112 | ANOMALY MISSING_WEIGHT | instruments, note | once at construct | low, tape-only (not in strip) |
| 4 | orch:267 | FEED_GAP reason=NONFINITE_SPOT stage=snapshot | instrument, reason, stage | `_basis_gap[inst]` latch, cleared only by a *successful basis* | low |
| 5 | orch:279 | FEED_GAP stage=basis reason=<MISSING_ATM_QUOTE/NO_FUTURE_PRICE/...> | instrument, reason, stage | `_basis_gap` latch per instrument (reason-keyed) | startup x24 (see 2.2) |
| 6 | orch:343 | ANOMALY FALSE_BREAK (NIFTY only) | instrument, spot | per-tick pulse `sig.false_break`, "latch" re-arms the next round => it is NOT a latch for flicker | 12 in 23 s on 10-15 |
| 7 | orch:437 | FEED_GAP round_missing_nifty | instrument, stage | `_nifty_gap_on` latch | once per episode; 1 at every startup |
| 8 | orch:469 | ANOMALY INSTRUMENT_ERROR | instrument, error, detail | `_basis_gap` latch **that is cleared every round** (defect D1) | **every round per failing instrument (150/150 in demo)** |
| 9 | orch:523 | REVERSAL_WATCH (NIFTY) | instrument, d_basis, dir | rising edge of `abrupt_against_bias`; re-arms on any quiet round | 1,351 / 821 per day |
| 10 | orch:531 | ANOMALY EXECUTION_CAUTION_WINDOW | after, note | `_caution_emitted` once | 1/day (fine) |
| 11 | orch:586 | APPROACH_ALERT | direction, level_side, level, spot, dist_pts, verdict, confidence, same_side, opp_side, conflicted, buy | **none** — emitted on every `fire` round | 2/day observed, but unbounded on a REJECT path (D2) |
| 12 | orch:614 | ENTRY_REJECTED | reason, direction, level_side, level, spot, dist_pts, verdict | **none** | same as 11 (D2) |
| 13 | orch:627 | FEED_GAP stage=manage_quote (open position, quote missing) | instrument, strike, right, stage | **none** — every managed round while quote missing | up to 4/s during a position (D3) |
| 14 | orch:633 | EXIT_REJECTED | strike, right, reason | **none** | every round while broker keeps rejecting an exit (D3) |
| 15 | orch:639 | ANOMALY POST_STOP_COOLDOWN | cooldown_secs, strike, right | once per SL_HIT | fine |
| 16 | orch:710,729,741 | FEED_GAP stage=prev_close_seed_band / post_exit_band (STALE_INPUTS / band.reason) | instrument, stage, reason | none | post-exit recompute fails each round? -> `mark_attempt` throttles to 30 s, ok |
| 17 | orch:768 | ANOMALY STALE_PREV_CLOSE_SEED | source | once | **ts = June 25 (seed.ts_ms), see D5** |
| 18 | orch:774,780 | SESSION_ERROR prev_close_seed CRITICAL/HIGH | stage, severity, reason | once | ts_ms=None -> wall clock |
| 19 | orch:802 | SESSION_ERROR run_loop | stage, reason | once; then finalize | ok |
| 20 | orch:816 | SHUTDOWN reason=session_end | reason | once | reason is hard-coded even on crash / Ctrl-C (D6) |
| 21 | feed.py:247-282 | FEED_GAP code_resolution / step_resolution(no_chain) / subscription / subscription_vix | instrument, stage, reason | once per instrument at startup | 6-7 per start |
| 22 | feed.py:313 | ANOMALY STEP_RESOLVED_LATE | instrument, step | once per instrument | 4-6 per start |
| 23 | feed.py:330 | ANOMALY STRIKE_STEP_CORRECTED | instrument, from_step, to_step, note | `_step_corrected` once per instrument | 3-7 (one at 15:21 on 10-16!) |
| 24 | feed.py:399 | ANOMALY UNMAPPED_WS_NAME | name, note | once per name | fine |
| 25 | feed.py:407 | FEED_GAP build_snapshot | instrument, stage, reason=repr(e) | **none** | every round per failing instrument (D1b) |
| 26 | feed.py:416 | FEED_GAP step_maintenance | stage, reason | **none** | every round if `_step_maintenance` keeps raising |
| 27 | feed.py:419/424 | FEED_GAP feed_recovered / all_instruments_gapped | stage | `_all_gapped` edge latch | 1+1 per startup |

Types in `schema.EVENT_TYPES`: 13. In use: all declared. `RECALC_TRIGGERS` lacks `post_cooldown` (not an event, a level_tape column) — known.

## 1. Correctness defects (most severe first)

### [HIGH] D1: INSTRUMENT_ERROR latch is cleared every round, so a persistent post-basis exception floods the tape at the round rate  (orchestrator.py:285, 468-472)  certainty: confirmed-by-running
- Flow: `_process_instrument` reaches line 285 `self._basis_gap.pop(inst)` (basis OK) and THEN throws later (continuation, band, view). `process_round`'s `except` checks `_basis_gap.get(inst) != "INSTRUMENT_ERROR"` -> emits and sets the latch. Next round line 285 pops it again, the same exception fires, event is emitted again.
- Demo (`err_latch_demo.py`, synthetic generator, NIFTY `continuation.update` forced to raise): 150 rounds -> **150 INSTRUMENT_ERROR events**. Live at 4 rounds/s x 23 instruments this is up to ~90 events/s.
- Fix: separate latch dicts: `_gap_latch[(inst,'basis')]`, `_gap_latch[(inst,'error')]`; clear the error latch only after a round that finished `_process_instrument` without raising. With the v2 episode model below: `episode_id` stays open until N consecutive clean rounds.
- Same family D1b (feed.py:407, 416): `FEED_GAP build_snapshot` / `step_maintenance` have no latch at all (one event per round per instrument if the exception is persistent, e.g. malformed tick object). Same fix.

### [HIGH] D2: APPROACH_ALERT + ENTRY_REJECTED re-fire every round while an entry is rejected  (orchestrator.py:577-619)  certainty: confirmed-by-reading
- `fire` stays True every round that spot is inside the 7-pt zone and no position exists. Rejects (execution.py:187/198/201/205: NO_QUOTE, PREMIUM_TOO_THIN, INSUFFICIENT_CAPITAL, ORDER_PLACEMENT_FAILED) leave no position, no cooldown. Result: 2 tape rows x 4/s (+1 `order_tape` REJECT row, + 1 decision_tape row) for as long as spot sits near the level. Not seen on the three days only because every approach filled; on a non-expiry day `min_premium_nonexpiry: 45` (vajra_config.yaml:143) makes PREMIUM_TOO_THIN far more likely once the operator makes the system productive. Also the dashboard sound `a.state==='ABOUT_TO_BREACH'` is level-triggered on `lean`, so it does not re-ring, but the tape and strip burn.
- Fix: per-(side, level) reject latch: emit ENTRY_REJECTED once per (reason, level) and then at most every 60 s with `suppressed=N`; APPROACH_ALERT only when an entry actually fills, or once per (level, direction, band_id) episode.

### [HIGH] D3: FEED_GAP manage_quote / EXIT_REJECTED have no latch, and the dashboard keys the loudest alarm off them  (orchestrator.py:627-635; dashboard.py:732)  certainty: confirmed-by-reading
- During an open position with a missing quote, one FEED_GAP per round. `_crit` flashes "FEED" while ANY FEED_GAP/EXIT_REJECTED row has `ts` within 60 s; the run-length dedup rewrites `last['ts']=ts_ms` on every repeat (orchestrator.py:244) so a flickering gap pins the banner on indefinitely. That is intended for a persistent gap, but combined with D2-type noise it makes the "critical" flash uninformative. Also `_crit` treats startup-phase FEED_GAP (e.g. `subscription`, `no_chain`) identically to a live-position gap.
- Fix: severity class in the row, flash only for `class=="ALERT"` rows that are OPEN episodes (END row clears it).

### [HIGH] D4: REVERSAL_WATCH is a pure noise generator driven by a sign-only test on a ~0.03 bias  (orchestrator.py:251-256, 521-527)  certainty: confirmed-by-running
`rw_attr.py` joined every event to the decision_tape row at the same `ts`:

| day | RW events | prev `bias_ewma` > 0 | NIFTY's own weighted contribution == 0 | prev_bias at event: p50 / p90 / max | events with prev_bias >= 0.10 / >= 0.25 | rounds with `reversal_freeze_flag` |
|---|---|---|---|---|---|---|
| 10-15 | 1,351 | 1,351 (100%) | 1,343 (99.4%) | 0.030 / 0.066 / 0.109 | 2 / 0 | 49,684 of 79,749 (62%) |
| 10-16 | 821 | 821 (100%) | 821 (100%) | 0.027 / 0.049 / 0.051 | 0 / 0 | 27,150 of 60,034 (45%) |

- Every single event has `dir = -1` (basis ticking down 1-2.5 pts) against a bias that is positive by ~0.03 (the operator's own `bias_threshold` is 0.25). `_is_reversal` only tests `_sign(prev_bias) != 0`, so a bias of +0.0001 counts as "a prevailing bias". `bias_ewma` was >= 0.25 in absolute value in only 449 / 553 rounds of the day, and never at an RW event.
- The latch re-arms on the first round whose `abrupt` flag is False. `abrupt` needs `|d_basis| >= noise_floor (1.0)` AND a z-score; with an implied-future basis ticking in 0.05-pt steps, almost every other round is below the floor, so the latch re-arms in ~2-8 s (inter-arrival p10 1.75 s, p50 7.8 s / 6.8 s, min 0.50 s; 463 / 307 events <= 5 s apart; 214 of 349 and 114 of 255 minutes contain >= 1 event).
- Also: the `reversal_freeze` it sets (decision_maker.py:153-155) is overwritten for the entry path: `_approach_entry` ignores `dr.decision` entirely, so the "freeze" gates nothing — only the audit column `reversal_freeze_flag` is true for 45-62% of all rounds. So the event neither protects the operator nor informs him.
- Fix (source-level, measured on `rw_hyst.py`): (a) require `abs(prev_bias) >= 0.5*bias_threshold` (0.125) => kills **1,349/1,351 and 821/821**; (b) require NIFTY's own contribution != 0 or `|d_basis| >= 3*noise_floor`; (c) min-interval 120 s regardless (keeps 101 / 54), 300 s keeps 42 / 22; (d) demote to INFO and emit only via the episode model (START when condition true for >= 5 s, END after >= 30 s quiet; collapses each day to 1-2 episodes with `n_pulses` and `peak_abs_d_basis`).

### [MEDIUM] D5: STALE_PREV_CLOSE_SEED is stamped with the *seed's* timestamp (June) into a today's event tape  (orchestrator.py:768; feed.py:231)  certainty: confirmed-by-running
- 10-14 event_tape row: `ts = 2026-06-25 15:30:05 IST`, between 13:26 and 15:29 rows => non-monotonic tape (`rw_attr`/`ev_stats` verified: 1 non-monotonic pair). Any sort-by-ts consumer, the strip's `s.round_ts - e.ts <= 60000` logic, or a "last event" liveness probe misreads it. Fix: `ts_ms=None` (wall clock) or the first round ts, and put the seed's own ts in the payload (`seed_ts`).

### [MEDIUM] D6: SHUTDOWN reason is always "session_end"; no run identity on any row  (orchestrator.py:816)  certainty: confirmed-by-reading + tape
- 10-16 SHUTDOWN at 15:25:52 with `session_end` (operator stop/crash path); 10-14 at 15:29:12. A SESSION_ERROR run_loop crash also ends in `SHUTDOWN session_end`. Add `reason in {session_close, operator_stop, crash, feed_dead}`, and add `run_id` (uuid per process) + `seq` (monotone per run) to *every* event row so 4 restarts in one jsonl are separable (10-16 has 4 STARTUP_CONFIG rows appended into one file).

### [MEDIUM] D7: `tape.event` raises on an undeclared type, and in `process_round` the call is outside any try/except  (tape.py:103-104; orchestrator.py:521-535,586,614)  certainty: confirmed-by-reading
- The v2 plan adds 7+ types; one typo (or a forgotten `EVENT_TYPES` entry) raises `ValueError` inside `process_round`'s body -> caught only by `run()`'s outer `except` (orchestrator.py:801) which ends the round loop => finalize => **day ends**. (Inside `_process_instrument` the per-instrument try/except would swallow it silently instead — event lost.) `_feed_event` swallows it (feed.py:156) => feed events silently vanish.
- Fix: in live mode never raise from an event write: coerce to `event_type="ANOMALY", payload={"kind":"UNDECLARED_EVENT","orig":type,...}` and `logging.error`; keep the raise under `VAJRA_STRICT_EVENTS=1` (tests/verify). Add a unit test that every literal in `_emit_event(`/`_feed_event(` call sites is in `EVENT_TYPES` (AST scan).

### [MEDIUM] D8: startup warm-up emits ~45 FEED_GAP/ANOMALY rows every start, and they pre-fill the 40-slot strip  certainty: confirmed-by-running
Per start (10-14 / 10-15 / 10-16): `basis|MISSING_ATM_QUOTE` **24 / 24 / 24** within 1-3 s, `step_resolution|no_chain` 6 / 6 / 7, `STEP_RESOLVED_LATE` 4 / 6 / 6, `STRIKE_STEP_CORRECTED` 7 / 3 / 7, plus `all_instruments_gapped` + `round_missing_nifty` + `feed_recovered`. The 24 basis gaps are each `instrument`-prefixed so the strip's run-length dedup cannot merge them: strip sim shows 6-10 rows of the 40 slots are startup FEED_GAPs evicted before anything else happens, and the same startup burst repeats at every mid-day restart (10-16: 4 restarts, 3 `breeze_connect_failed`).
- Fix: warm-up grace (no per-instrument basis FEED_GAP until the first round in which NIFTY has a full chain or 15 s elapsed), then ONE `STARTUP_SUMMARY`/`WARMUP_DONE` INFO row with `{instruments_ready, instruments_missing[], steps_resolved[], steps_corrected[]}`.

### [LOW] D9: `/api/health` returns a constant `{"ok": true}`  (dashboard.py:35-37)  certainty: confirmed-by-reading
Reports healthy while the engine thread is dead/hung (the front-end watchdog compensates by `round_ts` stall, but external monitors do not). Return `{round_ts, age_s, rounds_per_sec, instruments_live, spot_age_s, last_alert_seq}` and 503 when `age_s > 5`.

### [LOW] D10: `_basis_gap` is one dict shared by three different gap kinds
NONFINITE_SPOT (orch:266), basis reason (orch:278), INSTRUMENT_ERROR (orch:468) all read/write `_basis_gap[inst]`; a MISSING_ATM_QUOTE -> NO_FUTURE_PRICE reason flip re-emits (reason-keyed) which is intentional, but NONFINITE_SPOT <-> basis reason transitions cross-clear each other. Separate namespaces (part of D1 fix).

## 2. Per-day event counts (confirmed-by-running; `ev_stats.py`)

Total rows: 10-14 **54**, 10-15 **1,414**, 10-16 **892** (REVERSAL_WATCH = 95.5% / 92.0% of 10-15 / 10-16).

| (type, kind/stage/reason) | 10-14 | 10-15 | 10-16 | inter-arrival (s) 10-15: min / p10 / p50 / p90 / max |
|---|---|---|---|---|
| REVERSAL_WATCH (all dir=-1) | 0 | **1,351** | **821** | 0.50 / 1.75 / 7.79 / 19.8 / 76 (10-16: 0.50 / 1.75 / 6.76 / 16.6 / 48) |
| FEED_GAP basis MISSING_ATM_QUOTE | 24 | 24 | 24 | 24 within 1-4 s at startup (22-23 are <= 1 s apart) |
| ANOMALY FALSE_BREAK | 1 | **12** | 0 | 0.50 / 0.75 / 2.0 / 3.3 / 5 (10:01:20-10:01:43, spot 22867-22869, one level) |
| FEED_GAP step_resolution no_chain | 6 | 6 | 7 | all in the same second |
| ANOMALY STEP_RESOLVED_LATE | 4 | 6 | 6 | burst; 10-16 spans 90 s |
| ANOMALY STRIKE_STEP_CORRECTED | 7 | 3 | 7 | 10-16: 6 in 13 s at startup + 1 at **15:21:10** |
| APPROACH_ALERT | 1 (15:20:03) | 2 (10:00:14, 15:20:11) | 2 (15:20:09, 15:24:47) | all 15:20+ ones are the frozen-feed fills |
| FEED_GAP all_instruments_gapped / round_missing_nifty / feed_recovered | 1/1/1 | 1/1/1 | 1/1/1 | startup warm-up |
| SESSION_ERROR breeze_connect_failed | 0 | 0 | 3 | 10:24:40, 10:28:19, 10:46:11 |
| STARTUP_CONFIG (+4 siblings each) | 1 | 1 | 4 | |
| EXECUTION_CAUTION_WINDOW, MISSING_WEIGHT, SHUTDOWN, STALE_PREV_CLOSE_SEED (10-14 only) | 1,1,1,1 | 1,1,1,0 | 1,1,1,0 | |

Notes: (1) not a single FEED_GAP or any event between 09:42 and 15:20 corresponds to the 15:15-15:20 frozen cash feed — **the 5-minute frozen feed that produced all fictional fills left zero event-tape evidence** (the "no frozen-spot detector" gap in the brief, seen from the event side). (2) FALSE_BREAK on 10-14 is the 15:20:05 one that follows the garbage-feed APPROACH_ALERT. (3) The 12 FALSE_BREAKs are 12 *real* breach->snap-back cycles of spot across level+buffer; the per-tick pulse design cannot dedupe them, only an episode-level rule can.

### Cockpit strip run-length dedup (orchestrator.py:237-246)  — confirmed-by-running (`strip_alt.py`, `strip_sim.py`)
- **A B A B does not collapse**: 2,000 alternating RW / FALSE_BREAK events -> 2,000 pushed rows; the 40-slot deque holds 40 rows of the alternation and evicts everything else (FEED_GAP / EXIT_REJECTED / APPROACH_ALERT) in 20 pairs. On the real tapes the compression is excellent only because RW dominates uninterrupted (1,409 events -> 56 rows on 10-15; 872 -> 52 on 10-16); one interleaver (a FALSE_BREAK every ~20 s) would already blow it up.
- Keyed dedup (match any of the last 40 rows by (type, summary), bump its count, move it to the tail) gives 47 rows instead of 56 on 10-15 and keeps the final strip at 27 FEED_GAP / 10 ANOMALY / 1 RW instead of 23/8/6.
- **Eviction is FIFO regardless of severity**: the 24 per-instrument startup basis gaps + 6 no_chain rows occupy 30 of 40 slots at start; 6 / 10 / 7 FEED_GAP rows are evicted on the three days during normal operation (sim), e.g. the `step_resolution no_chain` rows for BSE/BAJFINANCE/... The operator who connects mid-way loses the only evidence of an unresolved strike step.
- The strip also overwrites `ts` with the LATEST repeat (orch:244): `first_ts` is lost, so duration of an episode is not recoverable from the strip. Keep `first_ts`, `last_ts`, `count`.
- Direct `tape.event` rows (MISSING_WEIGHT, startup, SESSION_ERROR at 18) never reach the strip; harmless but inconsistent.
- Fix = v2 strip below: class-aware eviction (INFO evicted first, ALERT never until resolved), keyed dedup, episode rows (START bumps, END closes), cap 40 -> 60 with sticky pin of OPEN ALERT episodes.

## 3. v2 event discipline (the design)

### 3.1 Classes and source-level rate control
Single choke point `EventBus.emit(type, payload, ts, key=None)` replacing `_emit_event/_feed_event/tape.event`. The per-type policy table lives in `schema.py` (`EVENT_POLICY`), enforced at the SOURCE: the tape row is written only if the policy admits it; suppressed repeats are counted and attached to the next admitted row (`suppressed: n`). Policy fields: `class`, `min_interval_s` (per key), `episodic` (bool), `hold_on_s`, `hold_off_s`, `cockpit` (bool), `sound`.

| type (kind) | class | key | min-interval / hysteresis at source | episodic |
|---|---|---|---|---|
| STARTUP_CONFIG, EXPIRY_RESOLVED, HOLIDAY_DECISION, THRESHOLDS_PROVISIONAL, HOLIDAYS_UNVERIFIED | INFO | - | once per run_id | no |
| STARTUP_SUMMARY (new, replaces the 24+13 warm-up rows) | INFO | - | once, at warm-up end | no |
| ANOMALY STEP_RESOLVED_LATE / STRIKE_STEP_CORRECTED / UNMAPPED_WS_NAME / MISSING_WEIGHT | INFO | instrument | once per key per run | no |
| ANOMALY EXECUTION_CAUTION_WINDOW | INFO | - | once/day | no |
| ANOMALY POST_STOP_COOLDOWN | INFO | - | once per SL_HIT | no |
| ANOMALY FALSE_BREAK (NIFTY) | INFO | level_id | episode: START on 1st, count `n_breaks`, END after 120 s with no new break at that level; emit at most 1 row / 60 s | yes |
| REVERSAL_WATCH | INFO (WARN only if `abs(bias)>=0.25` and NIFTY contribution != 0) | - | gated `abs(prev_bias) >= 0.5*bias_threshold`; START after condition holds 5 s; END after 30 s quiet; min 120 s between STARTs | yes |
| FEED_GAP basis (per instrument, NIFTY) | WARN; NIFTY basis or position-held quote => ALERT after 10 s | (inst, reason) | no row until gap persists `hold_on_s=3` (stocks) / 1 (NIFTY); END on `hold_off_s=3` clean rounds | yes |
| FEED_GAP all_instruments_gapped / round_missing_nifty | ALERT if > 10 s or position open, else WARN | - | as above, with `duration_s` on END | yes |
| FEED_GAP manage_quote, EXIT_REJECTED | ALERT | (strike,right) | START once; re-summarise every 30 s with `n`; END on recovery | yes |
| ENTRY_REJECTED | WARN | (reason, level_id) | once per key then every 60 s with `suppressed` | no |
| APPROACH_ALERT | INFO (fires the "anticipation" row); the sound is tied to ENTRY_FILLED instead | (level_id, dir) | once per (level, dir) per band_id episode | yes |
| SESSION_ERROR critical (connect failed, run_loop crash, no seed) | ALERT | stage | once per stage per 60 s | no |
| SHUTDOWN | INFO | - | once; with `reason` enum | no |
| SPOT_FROZEN, SPOT_IMPLAUSIBLE, REGIME, LEVELS_NARROWED, ENTRY_CUTOFF, RISK_STANDDOWN, SOUND, HEALTH | see 3.5 | | | |

### 3.2 `episode_id`
Every episodic event carries `episode_id = f"{type}:{key}:{start_ts_ms}"` (or `uuid4().hex[:8]`), `phase in {"START","UPDATE","END"}`, and END adds `duration_s`, `n` (pulses), `peak` (type-specific). UPDATE rows are rate-limited (<= 1 per 30 s). Open episodes are listed in `latest_state["open_episodes"]` so a mid-session dashboard attach sees them; `finalize()` closes any open ones with `phase=END, reason="shutdown"`. This replaces the per-pulse "edge latch" dicts (`_reversal_on`, `_false_break_on`, `_nifty_gap_on`, `_basis_gap`) with one `EpisodeTracker` (`open(key, ts)`, `touch(key, ts)`, `close_if_quiet(ts, hold_off_s)`), which also fixes D1/D3/D10. The tracker is a pure function of the (ts, snapshot) stream => replay-deterministic (no wall clock).

### 3.3 `alerts` channel (the only source of dashboard sounds)
- Engine keeps `alerts: deque(maxlen=200)` of rows `{seq, ts, class:"ALERT", code, text, episode_id, sound}`; only class==ALERT rows get a `seq`. `/api/alerts?since=<seq>` (and `latest_state["alert_seq"]`) is the contract. The dashboard stores `lastSeq` (in `sessionStorage`) and plays `row.sound` for each new seq exactly once; reload/second tab cannot replay old ones (first poll primes `lastSeq`, as `_lastExit` does today). Today's sounds are derived from front-end state diffs (`_lastSide`, `_breached`, `journal`), so they inherit every flicker of those views (dashboard.py:968-989).
- ALERT codes (the whole list): `ENTRY_FILLED`, `EXIT_PROFIT`, `EXIT_STOP`, `EXIT_REJECTED` (open), `FEED_CRITICAL` (NIFTY basis/quote gap > 10 s, or SPOT_FROZEN / SPOT_IMPLAUSIBLE while position open or levels armed), `ENGINE_DOWN` (browser-side stall already exists), `RISK_STANDDOWN`. Everything else is INFO/WARN: visible in the strip, never audible.
- Measured: today's `sister_breach` rule (rising edge of `breach_alerts` for ANY non-NIFTY instrument, dashboard.py:974-977) = **28 / 241 / 112 rings on 10-14/15/16** (decision_tape replay of the same condition; ITC 109 + BHARTIARTL 104 on 10-15, BANKBARODA 67 on 10-16). That is the "ting ting ting". Under v2, sister breaches are INFO only (a tick in the strip), never in `alerts`.

### 3.4 `HEALTH` once per minute (round-ts driven, so replays produce it too)
```
HEALTH payload: {"window_s":60, "rounds":N, "rounds_per_sec":float, "round_gap_max_ms":int,
  "instruments_live":int, "instruments_stale":int, "stale_list":[...<=8],
  "nifty_spot":float, "nifty_spot_age_s":float,        # secs since NIFTY spot LAST CHANGED
  "spot_changed_pct":float,                             # share of last-60s rounds where any cash index moved
  "future_age_s":float, "basis":float,
  "levels_age_s":int, "levels_stale":bool, "band_half_width":float,
  "events_written":{"INFO":n,"WARN":n,"ALERT":n}, "events_suppressed":n,
  "position_open":bool, "open_episodes":[episode_id...], "lag_ms_p99": int}
```
Emitted in `process_round` when `round_ts // 60000` advances (including rounds where NIFTY is absent — emit from a path before the early return at orchestrator.py:431, otherwise the proof of life vanishes exactly when needed). Class INFO; `rounds_per_sec == 0` or `nifty_spot_age_s > 20` upgrades it to a WARN. Consequence for the tape: a missing minute is itself evidence of a dead engine; the 15:15-15:20 freeze on all three days would have appeared as `nifty_spot_age_s` climbing 0 -> 290 while `future_age_s` stayed ~0.

### 3.5 `schema.EVENT_TYPES` additions for v2 (payload contracts; all rows also carry `run_id, seq, class, episode_id?, phase?`)

| type | class | payload contract (required keys) | emitted by |
|---|---|---|---|
| SPOT_FROZEN | ALERT (when position open or levels armed and in-session) else WARN | `instrument, last_change_ts, frozen_s, frozen_rounds, future_moved_pts, future_age_s, threshold_s` ; END: `duration_s, resume_jump_pts` | feed-health monitor. Rule: cash spot unchanged for `>= max(8 s, 20 x median round gap)` while implied-future/option ticks keep updating (>= 3 distinct ltp changes) => START; END on 3 distinct spot changes. NIFTY frozen => entries blocked (`ENTRY_BLOCKED reason=SPOT_FROZEN`). Observed: 290 s on all 3 days |
| SPOT_IMPLAUSIBLE | ALERT | `instrument, spot, future, implied_basis, basis_p99_abs, jump_pts, threshold_pts, action` ; one row per episode | rule: `abs(spot - implied_future) > max(40, 6 x rolling p99 abs(basis))` OR single-round `abs(Δspot) > 60` while `abs(Δfuture) < 10`. Observed: +185/+240/-134 jumps with basis -190/-247/-64. Action = `{"block_entries","suppress_levels_recompute"}` until spot re-converges for 30 s |
| REGIME | INFO | `regime in {"EXPIRY","NEAR","FAR"}, tdays_to_expiry, is_expiry_day, prev_regime, params:{proximity_pts, itm_depth, band_narrow_factor, size_mult}` ; fire on session start and on change only | orchestrator at start of day |
| LEVELS_NARROWED | INFO | `band_id, trigger, raw_upper, raw_lower, raw_half_width, narrowed_upper, narrowed_lower, narrowed_half_width, rule, spot, atm, vwap_ce, vwap_pe, intrinsic_ce, intrinsic_pe` | once per band recompute where narrowing changed the level by > 1 step (the recompute is already one row per 20 min on level_tape; this is the audit of *why*) |
| ENTRY_CUTOFF | INFO | `cutoff_hhmm, now_hhmm, reason in {"time","feed_unhealthy","spot_frozen","implausible"}, positions_open` ; once per day at cutoff, or per episode of an unhealthy-feed block | orchestrator |
| RISK_STANDDOWN | ALERT | `reason in {"daily_loss_limit","consecutive_stops","drawdown_pct","feed_unhealthy"}, metric, value, limit, until_ts, realized_pnl, trades_today` ; episodic (START/END) | executor/orchestrator |
| SOUND | INFO | `code, file, volume, source_alert_seq, played_by:"client"|"server"` ; written when a client acknowledges (`POST /api/sound_ack`) or, for headless runs, when the server would have rung | dashboard → engine ack; gives an audit trail of what was audible and a hard test that no non-ALERT code ever has a sound |
| HEALTH | INFO/WARN | as 3.4 | orchestrator |
| STARTUP_SUMMARY | INFO | as D8 | feed |
| ENTRY_BLOCKED (recommended extra) | INFO | `reason, level_id, side, dist_pts` ; episodic per (reason) | replaces silent `HOLD` with typed block reason during cooldown/gate |

Also add `post_cooldown` to `schema.RECALC_TRIGGERS` (known), and add `EVENT_CLASSES = ("INFO","WARN","ALERT")`, `EVENT_PHASES = ("START","UPDATE","END")`, `EVENT_POLICY`. `verify_vajra.py` should assert: every row has `class`; every `phase=START` has an eventual END or run SHUTDOWN; per-type rows/min <= policy cap.

### 3.6 Strip (cockpit) v2
Rows keyed by (type, key); class-aware eviction (drop oldest INFO, then WARN; never an OPEN ALERT episode); `first_ts/last_ts/count`; show OPEN episodes pinned on top with a live duration; startup bursts folded into one STARTUP_SUMMARY row. Capacity 60. The dedup is display-only, as today, but because the SOURCE now limits the tape the two cannot diverge by 100x.

## 4. Other defects found while reading (event-adjacent)
- [MEDIUM] `process_round` NIFTY-absent early return (orchestrator.py:431-441) skips every other check, so a long NIFTY-less period produces exactly one FEED_GAP row (latched) and nothing else; no duration, no END. Episode model fixes it.
- [MEDIUM] `_feed_event` timestamps startup rows with `time_ns()` wall clock while the round loop uses the same wall clock; on replay the sink is `None`, so replays lose all feed events and `latest_state` differs from live (feed events never appear in a replayed event_tape). Acceptable, but verification of "replay == live event tape" is impossible; document or emit feed-health from engine-side rounds (the SPOT_FROZEN/HEALTH design above is engine-side and replayable).
- [LOW] dashboard `eicon/ecls` map only FEED_GAP/EXIT_REJECTED/ENTRY_REJECTED/ANOMALY (dashboard.py:826-827); REVERSAL_WATCH/APPROACH_ALERT/SHUTDOWN render as `mut •`. v2 class drives the style.
- [LOW] `SESSION_ERROR` at app.py:177/190 and the `breeze_connect_failed` x3 on 10-16 are not visible on the cockpit (the dashboard starts after connect), so the operator only sees them in the terminal.
- [LOW] `ts_ms=None` on SESSION_ERROR (orch:775/781) mixes "ts of event" with "ts of round" — fine live, wrong in replay.

## 5. Recommendations for the two operator asks (event/alert view)

### A. Non-expiry productivity
- Emit `REGIME` at 09:20 and `LEVELS_NARROWED` per recompute, so every non-expiry band change is auditable in the tape (raw vs narrowed half-width, intrinsic vs extrinsic components). This is the only way the lead's replay harness can attribute P&L to the narrowing rule.
- Make ENTRY_REJECTED rate-limited (D2) *before* enabling more entries: wider productivity means more PREMIUM_TOO_THIN rejects (min premium 45 vs 30).
- `ENTRY_CUTOFF` / `RISK_STANDDOWN` rows must be episodic START/END so a standdown that blocks 50 approaches shows as one row with `blocked_approaches=50`, not as silence in `decision_tape` plus 50 `NO_APPROACH` reasons.

### B. Sounds
- Source of truth = `alerts` channel (3.3), ALERT class only, **five codes**: `ENTRY_FILLED` (existing trade_taken.mp3), `EXIT_PROFIT` (profit.mp3), `EXIT_STOP` (stoploss.mp3), `FEED_CRITICAL` + `RISK_STANDDOWN` + `EXIT_REJECTED` share ONE distinct urgent tone (new CC0 file `urgent.mp3`; may repeat twice, 5 s apart, until the episode END), nothing else.
- Retire `sister_breach` (28/241/112 rings per day) and make `approach_upper/lower` optional: if kept, tie them to NIFTY entering the 7-pt zone of a band that is within the v2 narrowed width AND no cooldown active AND at most once per (level, band_id) (today `_lastSide` clears whenever the state leaves ABOUT_TO_BREACH, so an oscillating spot re-rings each time: the 12 FALSE_BREAKs in 23 s on 10-15 imply the same pattern for the sound).
- Global cap in the engine, not the browser: `<= 1 sound / 10 s`, `<= 6 / 10 min` (priority: urgent > exit_stop > entry > exit_profit); every ring writes a SOUND event. Browser mute pill stays; it must not suppress the tape rows.

## 6. Things that are fine and should not change
- Binding event_tape being append-only jsonl with a typed enum is the right contract (tape.py:99-113) — just don't raise in live (D7).
- Edge latching for `round_missing_nifty` / `all_instruments_gapped` / `feed_recovered` (orch:436, feed.py:418-425) is correct in spirit; keep as the model for the EpisodeTracker.
- The run-length display dedup being strip-only and never touching the tape (orchestrator.py:237-247) — keep the principle "tape is truth", but add source limits (the two floods were *both* real sensor noise, not display problems).
- `EXECUTION_CAUTION_WINDOW`, `POST_STOP_COOLDOWN`, `STALE_PREV_CLOSE_SEED` (modulo ts), `UNMAPPED_WS_NAME` once-latches are correct.
- Operator rule "post-profit cooldown is SILENT" (orch:567-579): agree; in v2 it is still an `ENTRY_BLOCKED` INFO row on the tape only (never cockpit/sound), consistent with the rule.

## 7. Tests to add
1. `test_event_policy_coverage`: AST-scan `orchestrator.py`, `feed.py`, `app.py` for literals passed to `_emit_event/_feed_event/tape.event`; assert each is in `EVENT_TYPES` and has an `EVENT_POLICY` entry; no unclassified type.
2. `test_instrument_error_latched` (D1): force `continuation.update` to raise for 500 rounds -> exactly 1 START row, 1 END after clean rounds; the demo script is the fixture.
3. `test_entry_reject_ratelimit` (D2): stub `Executor.enter` to return `PREMIUM_TOO_THIN` for 1,000 rounds with spot in zone -> <= 1 + 1000/(4*60) ENTRY_REJECTED rows, 0-1 APPROACH_ALERT.
4. `test_reversal_watch_gate`: bias +0.03 and NIFTY contribution 0 with 100 abrupt-down pulses -> 0 events; bias 0.30 + NIFTY dir opposing -> exactly 1 START; replay golden: 10-15 first 30 min yields <= 3 rows (measured stand-in: counts from `rw_hyst.py`).
5. `test_false_break_episode`: spot oscillating 12 times around one level inside 23 s -> 1 START, `n_breaks == 12`, END ~120 s later.
6. `test_strip_abab`: alternate A/B 1,000 times -> strip rows <= 2 and OPEN ALERT row never evicted by INFO flood; eviction order INFO < WARN < ALERT.
7. `test_health_cadence`: replay 10 minutes -> exactly 10 HEALTH rows, also through a NIFTY-less stretch; `nifty_spot_age_s` rises when spot frozen but option ticks continue (feed the 15:15-15:20 slice of 10-14: expect SPOT_FROZEN START at ~15:15:08 and END ~15:20:03, SPOT_IMPLAUSIBLE at the resume tick).
8. `test_event_ts_monotonic`: run STALE seed path; assert tape ts non-decreasing (D5).
9. `test_undeclared_event_never_raises_live` (D7) and strict mode raises.
10. `test_alert_seq_contract`: no ALERT row duplicates a `seq`; `/api/alerts?since=n` returns only newer; no INFO/WARN row has `sound`; sound caps hold on a synthetic burst.
11. `verify_vajra.py`: rows/min per type <= policy cap on each recorded tape; every START has END or SHUTDOWN; monotone `seq` per `run_id`.
