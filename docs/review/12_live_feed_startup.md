# 12 — R12 Live feed source & startup reviewer: findings

Scope: `vajra/feed.py` (LiveBreezeSource), `vajra/app.py`, `run_vajra.ps1`, `orchestrator.sh`, `realfeed.py`, startup parts of `orchestrator.py`/`tape.py`/`breeze.py`/`dashboard.py`.
Evidence: code reading, the three event tapes, level/basis tapes, `logs/websocketLogs.log` + `apiLogs.log` (only the 10-16 11:10 session survives). No secrets quoted. Analysis was inline Python (nothing under /home/claude/vajra2 modified).
Certainty tags: CR = confirmed by reading, CT = confirmed from tapes/logs, PL = plausible/unverified.

## 1. Correctness defects (most severe first)

### [CRITICAL] `--mode replay` writes into the SAME partition it reads, destroying the recording  (app.py:107-112,149; tape.py:53-77)  CR
- `run()` in replay mode sets `session_date` to `--date`, builds `TapeWriter(cfg.tape_root, session_date)` (= `tapes/<date>/`) AND `ReplaySource(tapes/<date>)`. `write_startup_events` appends STARTUP_CONFIG etc. to the live `event_tape.jsonl`; every `append` writes the jsonl sidecar in "a" mode (same `tick_tape.jsonl` being read, if present); the first parquet flush renames the recorded `tick_tape.parquet` to `.bak` and starts a new one.
- RUN_LIVE.md §6 and the manual §6 both advertise `python -m vajra.app --mode replay --date <date>` as THE review command. Binding tapes get duplicated/renamed by the documented procedure.
- Fix: replay mode must write to `tapes_replay/` (as `replay.py` does) or a null writer; refuse if `out partition == source partition`. Make tapes read-only after session close (chmod / `.sealed` marker; the review copy is already `r--`).

### [HIGH] The previous-close seed is dead weight in the happy path and a hazard in the failure path  (feed.py:176-236; orchestrator.py:691-718, 307-325, 429; dashboard.py:717-723)  CR+CT
- Happy path: rounds before 09:20 are skipped (`process_round` early return), and on the first processed round `freshness.update` sees `since_attempt = now - seed_ts` (hours to months) and recomputes immediately (trigger `interval`, not `open`, when a seed exists; confirmed: first live level row on all 3 days is `interval`). The seed therefore never influences a decision.
- It is also invisible: `latest_state` is only filled by `_update_view` (post-09:20), so pre-open the dashboard has no `level_history`; the banner text "levels seeded from prev close" (manual §2.3) cannot appear. No levels are displayed pre-open at all.
- Failure path: a failed re-anchor (ITM quote not yet ticked) is SILENT (no event; only `mark_attempt`, 30 s retry) and leaves `st.band` = the seed band. `_approach_entry` treats any spot at/beyond a level as a zone, so a stale band fires an immediate entry. Mid-day restart has `trade_start` already passed, so this is live exposure. On 10-14 the seed was a June band around 24,000 vs spot 22,584: any band failure in the first rounds would have produced "FIRE_DOWN, breached by ~1,400 pts" instantly. Window is real: options are subscribed in k=-6..+6 order, CE then PE, only after the first spot tick; ATM may be present before ATM+50 PE (basis ok, band not ok). Not observed on tape (no band-failure events exist because none are emitted) — PL for occurrence, CR for exposure.
- Data hygiene (CT): the seed is stamped with the OLD timestamp: 10-14 `level_tape` row ts = 2026-06-25 15:30:05 and the `STALE_PREV_CLOSE_SEED` event ts = 2026-06-25, inside the 2026-10-14 partition. 10-15's seed = last NIFTY row of 10-14 (15:29:12, i.e. taken from the post-freeze garbage window); 10-16's = 10-15 15:29:59 (garbage window too: basis -39).
- Manual-vs-code trap: manual §5 says delete old `tick_tape.jsonl` (keep parquet), but seed priority 1 REQUIRES `tick_tape.jsonl` (feed.py:188). Following the manual silently falls through to the stale June recording (which is what happened on 10-14; the review copy has no jsonl at all).
- Fix (v2, see §2.1): drop jsonl/realfeed seeding; write a tiny `state/last_levels.json` at each recompute and at 15:20; use for pre-open DISPLAY only, tagged `trusted=False`; entries require `band.source == 'live'`; reject any seed older than 5 trading days or with ts not on the previous trading date; emit FEED_GAP on a failed live re-anchor.

### [HIGH] Feed liveness is not monitored: a frozen websocket looks like a healthy 4 Hz feed  (feed.py:381-429; breeze.py:247-261,308; dashboard.py:476-508)  CR
- `snapshot_raw()` returns last-known ticks; every round gets a fresh `ts_ms` and rebuilt snapshots whether or not any tick arrived. Ticks carry no receive timestamp; `ltt` is ignored. The dashboard "DATA FROZEN" watchdog only checks that `round_ts` advances (5 s), which it always does. Hence the A2 15:15-15:20 cash freeze (and any dead-socket case) is invisible, and `all_instruments_gapped` fires only before the first tick.
- Fix: in `_on_ticks` store `recv_ms` and exchange `ltt` per key; per round compute age per key. Rules: (a) no tick on ANY key for >3 s in session -> FEED_GAP `ws_silent` + reconnect/resubscribe; (b) NIFTY cash spot unchanged AND option keys ticking for >N s -> `SPOT_FROZEN`: mark NIFTY snapshot `stale`, block entries, do not re-anchor (calibrate N from healthy days: longest unchanged-spot run 09:20-15:15; I did not run this calibration, script sketch in §5); (c) spot-vs-implied-future gap |basis| > max(60, 3x trailing p95) -> `SPOT_IMPLAUSIBLE`, block entry. Carry-forward of falsy fields (`breeze.py:257-260`) must also stop for `ltp` (stale price retained as live).

### [HIGH] No single-instance guard; dashboard bind failure is silent  (app.py:195-204)  CR
- Two engines started for the same day append to the same jsonl files (interleaved rows) and fight over parquet rename. `_start_dashboard` runs uvicorn in a daemon thread; a bind failure (`sys.exit` inside a thread) ends the thread silently, the engine keeps recording, and the browser shows the OTHER (possibly dead/stale) instance on 8766. `demo_dashboard.py` has a port check; `app.py` does not.
- Fix: lockfile `tapes/<date>/.lock` (pid + start ts, `msvcrt.locking`/`fcntl.flock`), `socket.bind` probe before engine start, fail loudly.

### [HIGH] Connect failure policy: dies after ~20 s; records nothing useful; tape left unclosed  (app.py:114-116,171-192; breeze.py:129-153)  CT+CR
- 10-16: three consecutive starts (10:24, 10:28, 10:46) ended `breeze_connect_failed: Failed to connect to live stream` after 5 attempts (backoff 2/4/6/8 s). Session only began 11:10:43, i.e. ~46 min of the day lost; the mid-day start is a consequence. `generate_session` evidently succeeded (token ok); the failing step was `ws_connect`, which points to the broker stream (503s from the REST gateway at 11:10:58-11:11:02 support an upstream-congestion story) or a competing connection. Credentials are documented as copied from MYTHOS; if MYTHOS holds a websocket on the same session, a conflict is plausible (PL, unverifiable here).
- `SESSION_ERROR` is stamped with start time not failure time; `connect` failure leaves `TapeWriter` unclosed (before the `try/finally`); each failed start appends a full STARTUP_CONFIG block to the day's event tape.
- Fix: supervisor loop in app: retry connect with capped backoff until 15:00 (log each attempt with timestamp, exception text, phase `generate_session` vs `ws_connect`), keep dashboard up showing "CONNECTING (attempt n)". Distinguish token-invalid (stop, tell operator) from ws-down (keep trying). Wrap everything after `TapeWriter(...)` in the `try/finally`.

### [MEDIUM] Mid-day (re)start: first valid round anchors immediately on a cold engine  (orchestrator.py:306-325; feed.py VWAPTracker)  CR+CT
- Behaviour on tapes: first level row 13:26:06 (10-14), 09:41:55 (10-15), 11:11:03 (10-16), 20-34 s after process start. Anchoring immediately is acceptable in principle: broker `avgPrice` is the exchange session VWAP, so restart does not distort it. Problems: (1) re-anchor grid phase becomes restart-time + 20 min (13:26, 13:46...) instead of 09:20-grid; (2) BasisMonitor std window, bias EWMA, ContinuationValidator, `_vol_ratio` are cold: first real d_basis against std~0 flags ABRUPT readily (relevant to the REVERSAL_WATCH flood); (3) if a tick lacks `vwap` the fallback `VWAPTracker` returns `ltp` on first sight (`computed`) and thereafter a since-restart VWAP, so a band can be built from LTP rather than session VWAP. I did not measure the share of `vwap_source=computed` in the tapes — measure before deciding.
- Fix: warm-up gate: no entries until (a) full ±n chain ticked for NIFTY (ATM, K_CE, K_PE all present and VWAP source `feed`), (b) >= 60 s of basis history, (c) spot-liveness green. Anchor immediately for display, then snap the next re-anchor to the 09:20+20k grid (13:40, not 13:46). Treat `computed` VWAP as untrusted for band input (refuse, retry).

### [MEDIUM] Startup latency 10-18 s inside `_ensure_base_subscriptions` before NIFTY is even subscribed  (feed.py:238-282)  CT
- `rounds()` start -> first yield: 18.2 s (10-14), 15.5 s (10-15), ~10 s (10-16). Log shows 21 sequential `get_names` (~0.16 s each, 3.3 s) then 8-thread chain REST for steps (pre-market ALWAYS fails; 9 of ~31 chain calls returned 503 on 10-16 and were retried only after 90 s), then 49 subscribes. NIFTY/BANKNIFTY/FINNIFTY codes and NIFTY step (50) need none of it.
- Fix: subscribe NIFTY spot+future+VIX and its chain first (milliseconds), start rounds, resolve stock codes/steps in a background thread, subscribe stocks as they resolve. Time to first NIFTY band drops from 20-34 s to ~5 s.

### [MEDIUM] Strike-step self-healing mutates global config, is not on the tape, and diverges live vs replay  (feed.py:252,284-332; orchestrator.py:272)  CR+CT
- NIFTY is protected: the config has `NIFTY: 50`, `_step_retry` only holds stocks, and part (2) skips `cfg.indices`. Good. But stocks: `cfg.raw["strike_step"]` is mutated from the feed thread; live `_process_instrument` then uses it, replay (`strike_step(inst, snap.inferred_step())`) uses the min gap of recorded strikes. Initial resolved steps are never written to the tape (only failures/corrections). Steps flip between days (BAJFINANCE 5->10 on 10-14, 10->20 on 10-16; BANKBARODA 2.5->5, 2.5->10; AUBANK 10->40): `inferred_step()` uses the smallest gap among STREAMED strikes, and strikes that never traded do not stream, so it over-coarsens, and the subscription grid is itself derived from the step (circular). Impact is on the 20 stocks' bloc voice, not NIFTY execution.
- Fix: v2 loads stock strike grids from the broker security master/option-chain strike list once per day (full strike list, not modal diff), persists to `state/steps_<date>.json`, writes a `STEP_TABLE` event to the tape, and replay reads it.

### [MEDIUM] Subscriptions only grow, never unsubscribe  (feed.py:334-342)  CT
- 49 base + 892 option subscribes by 15:21 on 10-16 (941 total; NIFTY 2+38). Step corrections and the 15:20 garbage-spot jump (80 subscribe lines in 15:20-15:21) add whole new grids. Stale grids keep streaming. Headroom exists today, but a broker token cap exists (value not verified here) and a violent day or frozen-then-jump feed multiplies it. The "double subscribe per instrument" in the ws log is NOT a bug: the SDK prints only the stock code; 49 lines = 24 instruments x (cash + future) + INDVIX.
- Fix: cap the subscribed window to ATM±(n+2) using hysteresis, unsubscribe strikes outside it, never re-centre the grid on a spot that fails the plausibility check.

### [MEDIUM] `run_vajra.ps1` / `orchestrator.sh` do not match the manual and add risk  CR
- Manual §2.2/RUN_LIVE: `python -m vajra.app --mode live`. Scripts instead run `verify_vajra.py` (synthetic full session + two replays) before EVERY start, including a mid-day restart where 20-60+ s of delay matters (duration not measured). They accept `-Date`/`DATE_ARG` and pass `--date` in LIVE mode: live then runs with a non-today session_date, wrong expiry resolution and wrong tape partition (app.py:103 only checks it is a trading day). `-Mode` ValidateSet omits `real`; `.ps1` assigns the automatic `$args`; header says "needs BREEZE_* env vars" while credentials.py is the primary source. Neither checks SESSION_KEY non-empty/fresh.
- Fix: scripts call one `python -m vajra.app preflight && python -m vajra.app --mode live`; `--date` forbidden with `--mode live`; gate (`verify`) only on code change (hash stamp), not at every start.

### [LOW] `feed.mode` default mismatch  (config yaml:201 vs app.py:91)  CR
- yaml `live`, code fallback `synthetic`. Removing the key silently runs synthetic (non-binding) data that still writes tapes into `tapes/<date>/`. `--mode` choices include `None`.
- Fix: no default; require explicit mode; synthetic/replay always write to a different root with a visible banner.

### [LOW] Timing/pacing/close  (feed.py:381-429)  CR+CT
- Adaptive pacing (`poll - dt`) is correct, drift-free; at 0.25 s the build cost (24 snapshots x 13 strikes) fits. Session stop at 15:30 uses wall clock vs IST seconds (tz-robust, not skew-robust; no clock sanity check). SHUTDOWN is stamped with the LAST ROUND ts: 10-14 15:29:12 and 10-16 15:25:52 are operator Ctrl+C exits (not 15:30), 10-15 15:29:59 was graceful. `KeyboardInterrupt` is not caught by `except Exception` but `finally: finalize` runs, so flatten/flush still work; ok.
- All-gapped latch is correct (edge-fired, `feed_recovered` once); it only protects startup because of the liveness gap above. Tape close order (finalize/flush -> `tape.close` -> `breeze.disconnect` -> `server.should_exit`) is fine.

### [LOW] Dashboard clock logic uses browser-local time  (dashboard.py:478-480,491,662)  CR
- `marketPhase`, end-of-day detection and countdown use `getHours()`; correct only if the browser is on IST.

## 2. Improvements / design proposals (ranked)

### 2.1 Seed: what v2 should do (answer to the question)
Reject all three options as framed; combine: (1) NEVER read the multi-GB jsonl; (2) persist `state/last_session.json` (date, last trusted NIFTY spot, levels, basis) from the engine itself at 15:20 and at each recompute; (3) on start, load only if `date == previous trading day` (calendar-based) else drop with an event; (4) seeded levels are display-only (`trusted=False`), the first LIVE round after warm-up replaces them (no seed needed for decisions); (5) if the operator wants pre-open numbers, compute from the first live round at 09:15 (chains answer after open). Stale seed older than 5 trading days: refuse, log SESSION_ERROR severity LOW, continue unseeded. Prior-day parquet is acceptable as a fallback source (column-projected read of NIFTY rows, last row with spot/future plausibility check), but a 2 KB JSON is simpler and cannot be garbage-tainted by the 15:15+ window if written at 15:20.

### 2.2 `real` mode / realfeed.py: retire
Only users: `--mode real`, `demo_dashboard.py`, seed fallback #2. `vajra/data/` here contains no `real_session_*.jsonl`, so all three already fail/return None in this copy. The Black-Scholes path is dead (NIFTY always has `real_opts`; non-NIFTY not yielded), and the module violates the operator's own no-fabrication rule. Delete `realfeed.py`, `synthetic.py` data CSV coupling stays only for the dev smoke; point `demo_dashboard.py` at a parquet tape via `replay_browser.py`'s streaming reader.

### 2.3 v2 startup checklist code (pre-flight)
```python
# vajra/preflight.py  (v2) -- returns list[Check]; any FAIL aborts, WARN prints
import os, shutil, socket, time, importlib
from datetime import datetime
from pathlib import Path
from .timeutil import IST

def run_preflight(cfg, cal, mode, dry=False):
    out = []; add = lambda n, s, d="": out.append((n, s, d))
    now = datetime.now(IST); today = now.date()
    # 1 trading day / mode sanity
    if mode == "live":
        add("trading_day", "OK" if cal.is_trading_day(today) else "FAIL", str(today))
        add("session_window", "OK" if "08:30" <= now.strftime("%H:%M") < "15:25" else "FAIL", now.strftime("%H:%M"))
    # 2 credentials present, file touched today after 06:00 IST (daily token heuristic)
    ok = all(cfg.breeze_secret(k) for k in ("api_key", "api_secret", "session_token"))
    add("credentials_present", "OK" if ok else "FAIL")
    cp = Path(importlib.import_module("vajra.credentials").__file__)
    mt = datetime.fromtimestamp(cp.stat().st_mtime, IST)
    add("token_fresh_heuristic", "OK" if mt.date() == today or (mt.date() < today and mt.hour >= 6 and False) else "WARN",
        f"credentials.py mtime {mt:%Y-%m-%d %H:%M}")
    # 3 token validity: real authenticated call (generate_session only; NO websocket) + latency
    if not dry or mode == "live":
        t0 = time.time()
        try: bw = connect_rest_only(cfg); add("token_valid", "OK", f"{time.time()-t0:.1f}s")
        except Exception as e: add("token_valid", "FAIL", repr(e)[:120])
    # 4 clock sanity: local clock vs HTTP Date header of the broker API (<=2 s) and tz is IST-aware
    skew = http_date_skew("https://api.icicidirect.com")        # abs seconds, None if unreachable
    add("clock_skew", "OK" if skew is not None and skew < 2 else "WARN" if skew is None else "FAIL", f"{skew}")
    # 5 disk: >= 12 GB free on tape volume (jsonl ~6.5-8 GB/day), today's partition writable
    free = shutil.disk_usage(cfg.tape_root.anchor or cfg.tape_root).free / 1e9
    add("disk_space", "OK" if free >= 12 else "FAIL", f"{free:.1f} GB free")
    # 6 prior tape presence (parquet or state file) -- informational, never blocks
    add("prior_state", "OK" if Path("state/last_session.json").exists() else "WARN", "no seed; will anchor live")
    # 7 single instance + port
    add("single_instance", "OK" if acquire_lock(cfg.tape_root / today.isoformat() / ".lock") else "FAIL")
    s = socket.socket(); busy = s.connect_ex((cfg.server_host, cfg.server_port)) == 0; s.close()
    add("dashboard_port", "FAIL" if busy else "OK", str(cfg.server_port))
    # 8 config sanity: mode explicit, LIVE_ORDERS false, weights cover universe, holidays_verified
    add("weights_cover_universe", "OK" if not missing_weights(cfg) else "WARN", ",".join(missing_weights(cfg)))
    add("live_orders_off", "OK" if not cfg.live_orders else "FAIL")
    return out
```
Print a table; exit non-zero on any FAIL. Run automatically as first step of `--mode live`; `python -m vajra.app preflight` runs it alone.

### 2.4 `--dry` self-test (no tape writes, no orders, <30 s)
`python -m vajra.app --mode live --dry`: (1) preflight; (2) connect REST+ws; (3) subscribe NIFTY spot/future/VIX + ATM±2 only; (4) wait up to 15 s for ticks; assert: spot tick age < 3 s, ATM CE/PE and K_CE/K_PE present with `vwap` from feed, implied basis within ±60 pts of spot, VIX plausible (8-90), `ltt` within 5 s of local clock (outside market hours: report last-trade age only); (5) run `compute_basis`/`compute_band` once and print the band, half-width and distance to spot; (6) feed 200 synthetic rounds through an in-memory orchestrator (NullWriter) to prove the engine path; (7) unsubscribe, disconnect, print PASS/FAIL per item. Uses NullWriter (as in replay_browser.py) so partitions stay untouched.

### 2.5 Other
- Supervisor with auto-reconnect: on `ws_silent` > 3 s during session, reconnect + resubscribe current windows (keep the `latest` dict, mark ages), backoff 1/2/4/8 s, event per attempt.
- Emit FEED_GAP `band_failed` when a re-anchor fails; never keep an unreferenced stale band: `band.source` field (`seed|live`), `band.age`.
- Run `verify_vajra.py` on code change only; write `state/gate_ok.<git/hash>`.
- Record `recv_ms`, `ltt` and resolved steps in the tick tape (schema bump) so replay can reproduce feed health decisions.

## 3. Recommendations specific to operator asks
A (non-expiry levels): startup items here are neutral, but two matter. (i) The band half-width diagnostics from the first live round should be printed by `--dry` and in the first-round event so the operator sees "band ±132 pts vs entry 7" at once. (ii) The first-round anchoring after a restart must go through the warm-up gate in §1, otherwise a narrowed (smaller-padding) band on non-expiry days will be more sensitive to a partial chain/`computed` VWAP at restart. Any tighter-band parameter must be validated on tapes that start at 09:15, not on these mid-day-started ones.
B (sounds): startup-related only: add one distinct, rare "ENGINE DEGRADED" sound (feed silent / connect failed) driven by the new liveness events; do not sound on FEED_GAP step/ATM gaps at startup (they fire ~35 times in the first 30 s on every start).

## 4. Fine, do NOT change
- Adaptive 0.25 s pacing, edge-latched all-gapped alert, per-instrument try/except around snapshot build, 15:30 self-stop and finalize in `finally`, `.bak` rename of same-day parquet (never truncate), jsonl flush per row, explicit-English-month expiry formatter, monthly-only expiry for non-NIFTY instruments, NIFTY step fixed in config (exempt from self-healing), non-trading-day guard, paper-always.
- Double subscribe is cash + future, correct.

## 5. Tests I would add
1. `test_replay_never_writes_source_partition` (CRITICAL fix): run app replay on a tmp tape; assert source files byte-identical.
2. Seed: stale (>5 trading days / wrong date) seed rejected; seed band never produces FIRE; failed first re-anchor emits `band_failed` and blocks entries.
3. Liveness: fake breeze with frozen cash tick + ticking options 10 s -> `SPOT_FROZEN`, entries blocked; implied-vs-spot gap 185 pts -> `SPOT_IMPLAUSIBLE`.
4. Partial chain at first round (ATM present, ATM+50 PE absent) with a stale seed present -> no entry.
5. Single-instance: second start on same date exits non-zero; port busy -> fails loudly.
6. Connect supervisor: ws_connect fails 7 times then succeeds -> session proceeds, attempts logged with timestamps.
7. CLI: `--mode live --date X` rejected; missing `feed.mode` rejected.
8. Preflight table test with injected failures (disk, clock skew, token).
9. Calibration script (not unit test): longest run of identical NIFTY spot per day, 09:20-15:15 from tick_tape (column-projected, NIFTY rows) to set `SPOT_FROZEN` seconds; also `vwap_source` share by day and minutes since start.
