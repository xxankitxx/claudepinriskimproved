# 22 — Static analysis & code-quality reviewer: findings

Scope: every `.py` under `/home/claude/vajra2` read (vajra/*, engine/*, replay*.py, verify_vajra.py, demo_dashboard.py, dashboard `_PAGE` skimmed
for structure), plus ruff / pyflakes / vulture / bandit / mypy. Raw tool output: `/home/claude/vajra2_review/scratch/22/`
(`ruff_all.txt`, `ruff_sel.txt`, `pyflakes.txt`, `vulture.txt`, `bandit.txt` [secret values redacted], `mypy.txt`, `prof.py`, `ruff.toml`).
Nothing under the repo was modified. (Side effect to disclose: importing `vajra` from my snippets may have refreshed some `__pycache__/*.pyc`; no source touched.)
Secrets: values in `vajra/credentials.py` are never quoted here.

Tool signal summary: pyflakes 5 real hits, vulture 6, ruff(E,F,B,PL,C901,S) ~48 on the selected set, bandit 0 medium/high (all LOW; the `B105` hits are false
positives on variable *names*, except credentials.py which is a true positive). mypy: 51 messages, almost all `Optional` narrowing (benign) but see
HIGH-3/MED-5. 77 tests pass; the test suite does not exercise the dashboard HTTP layer at all (no `TestClient`), which is why HIGH-3 survived.

---------------------------------------------------------------------------------------------------------------------------------

## 1. Correctness / robustness defects (most severe first)

### [HIGH] Startup banner `⚡` raises `UnicodeEncodeError` when stdout is not UTF-8 (Windows cp1252)  (vajra/app.py:133)  certainty: confirmed-by-running
- Repro: `PYTHONIOENCODING=cp1252 python -c 'print("⚡")'` -> `UnicodeEncodeError: 'charmap' codec can't encode character '⚡'`. It is the **only**
  non-cp1252 character in any console string in the engine (I scanned all print/stderr literals; `·`, `—` are cp1252-safe; every `open()` already passes `encoding=`).
- Scenario: Python 3.13 on a real Windows console uses UTF-8 console I/O (works). But `.\run_vajra.ps1 | Tee-Object`, `> run.log`, Task Scheduler, or any
  piped launch makes stdout a pipe -> ANSI code page cp1252 -> crash at line 133. That line runs **after** `TapeWriter`, `_connect_breeze()` (live websocket open) and
  `_start_dashboard()` and **before** the `try/finally` at :140, so the tape is never closed (parquet gets no footer), the websocket is never disconnected, the
  process dies with a traceback at 09:1x. Looks exactly like "restarted mid-day" (10-16 had 4 STARTUP_CONFIGs).
- Fix: (1) in `main()` first line: `for s in (sys.stdout, sys.stderr): s.reconfigure(encoding="utf-8", errors="replace")`; (2) `$env:PYTHONUTF8=1` in `run_vajra.ps1`;
  (3) drop the emoji from the banner (keep it in the dashboard only); (4) move the banner **inside** the `try`.

### [HIGH] No feed-liveness primitive anywhere: ticks carry no receive time, `latest` never ages, no ws disconnect handler  (vajra/breeze.py:122,143-146,155-157,247-261)  certainty: confirmed-by-reading
- `_on_ticks` stores `norm` with no `time.time()`; `snapshot_raw()` returns the dict regardless of age; `_connected` is set True once in `connect()` and never flips;
  no `on_disconnect`/`on_error` is registered on the SDK. The A2 frozen-cash-feed (15:15-15:20) is therefore **invisible by construction**: spot "unchanged" is a valid
  value to this code. `feed.py:_build_snapshot` (344-379) has no per-tick age either.
- Compounding: `breeze.py:268` `ltp = _f(t.get("last") or t.get("ltp") or t.get("close"))` falls back to **previous close** when `last` is absent -> a stale/previous-close
  price is silently injected as a live LTP; `:301` `volume ... or ltq` replaces cumulative volume by *last traded qty* (volume goes backwards; `VWAPTracker`/`_vol_ratio` clamp
  with `max(0, ..)` so it is silent). `:257-260` carry-forward treats `0` (legit "no bid") as missing and resurrects an old bid/ask -> fantasy fills in `OrderWrapper.place`.
- Fix (-> new `vajra/feed_health.py`, section 2): stamp `rx_ms` per key in `_on_ticks`; expose `age_ms(key)`; register `on_disconnect` -> `_connected=False` + event; remove the
  `or close` / `or ltq` fallbacks (map explicit field names per product_type); carry-forward only for fields **absent** from the tick (`k not in t`), never for zero.

### [HIGH] `/api/state` returns HTTP 500 on any NaN/inf in the state -> cockpit looks frozen  (vajra/dashboard.py:33; source vajra/breeze.py:106-107, feed.py:354)  certainty: 500 confirmed-by-running; NaN source plausible
- Starlette `JSONResponse` serialises with `allow_nan=False`. Test: `latest_state={"vix": nan}` -> `GET /api/state` = 500. Path to NaN: a VIX tick lacking `last` -> `_f(None)` = None ->
  `normalize_vix(None)` **returns `float("nan")`** (breeze.py:106-107) -> stored in `latest` (NaN is truthy, so carry-forward does not replace it) -> `feed.py:354` accepts it (`is not None`)
  -> `snap.vix` -> `instruments_view["vix"]` / `_nifty_head["vix"]` -> every poll 500s until a good VIX tick arrives. The JS catches the failed fetch and shows the watchdog "stale" banner
  while the engine is healthy. (The NaN also lands in jsonl as bare `NaN` — invalid JSON for any non-Python reader.)
- Fix: `normalize_vix(None)` -> return `None`; `_f()` rejects non-finite (`math.isfinite`); serialise with `ORJSONResponse` (NaN->null) or a one-line `_json_safe()` in `_update_view`;
  add a dashboard `TestClient` test that injects NaN.

### [MEDIUM] `Orchestrator.run()` turns any non-per-instrument exception into a silent normal shutdown  (vajra/orchestrator.py:801-803)  certainty: confirmed-by-reading
- Only `_process_instrument` is isolated (:467). `decision.evaluate`, `executor.enter/manage`, `_emit_orders`, `_compute_alarm`, `_update_view`, `tape.append` (raises `ValueError`/`KeyError`
  on schema drift) all run unprotected; one raise ends the loop, logs one `SESSION_ERROR`, `finalize()` flattens, emits `SHUTDOWN reason=session_end` (the same reason as a normal close) and
  `app.run()` returns normally -> exit code 0, no terminal message, rest of the day untraded. Ironically the flatten is the right fail-safe for an open position; the problem is the silence and no recovery.
- Fix: per-round `try/except` with a consecutive-failure budget (e.g. 5), `ANOMALY ROUND_ERROR` with `traceback.format_exc(limit=4)`, continue; if budget exhausted -> flatten, `SHUTDOWN reason="fatal:<type>"`, print to stderr, `sys.exit(2)`.
  Never `continue` past an exception thrown by `executor.manage` while a position is open — flatten instead.

### [MEDIUM] Decision/continuation FIRE path is dead; its outputs are logged as if they mattered  (vajra/orchestrator.py:538-577, decision_maker.py:93-228)  certainty: confirmed-by-reading + grep
- `fire` (:577) depends only on `_approach_entry()` + `expiry_gate_ok` + cooldowns. `dr.decision/reason_code` are never read for gating (grep: only recorded at :853-854 and :1159-1160, and
  *overridden* by `decision_word/reason`). Therefore **dead in practice**: the CONFIRM branch (decision_maker.py:149-173), the LEAD branch (:175-191, even `routing_mode: LEAD` fires nothing), `bias_threshold`,
  `bias_threshold_lead`, `divergence_penalty`, `reversal_freeze` (the freeze computed at :119-122 never blocks `fire`), `Reason.FIRE_CONFIRM/FIRE_LEAD/DIVERGENCE_DAMPED/BIAS_*`.
  `decision_tape.reversal_freeze_flag` / `divergence_flag` columns therefore describe a mechanism with **zero effect** — and `REVERSAL_WATCH` (1,351 events on 10-15) is advisory-only noise.
- Still live and useful: `bias_ewma` (feeds `_confluence().confidence`), `applied_weight`, `ContinuationValidator` states (feed `signals` -> confluence + heat + cockpit).
- Fix: either (a) delete CONFIRM/LEAD/threshold code (~130 lines) and rename the module `confluence.py`, or (b) make the confluence a *real* size/entry modifier (v2: `regime.py`). Do not keep a gate that
  only writes columns. Also fix the tape: drop or rename `reversal_freeze_flag` until it gates something.

### [MEDIUM] Entry is not gated on staleness/feed health  (vajra/orchestrator.py:577-579, 552-553)  certainty: confirmed-by-reading
- `fire` ignores `self._levels_stale` (set when a due re-anchor failed, :390) and has no feed-health input. A band >20 min old (stale) can trigger entries. Also `entry_cutoff` is wired to
  `execution.flatten_before` (:93), so "last entry" == 15:25 == flatten time; the A2 15:20 entry had only 5 min to live. Add a separate `execution.last_entry` (suggest 14:45–15:00) and
  `and not self._levels_stale and feed_ok` in `fire`.

### [MEDIUM] `pad_floor < 0.10` is a silent no-op  (vajra/engine/trigger_engine.py:62; docstring :1-3)  certainty: confirmed-by-reading
- `pad = max(0.10, pad_floor)`. Any attempt to *narrow* the band through `trigger.pad_floor` (the obvious knob for complaint A; `replay.py --set trigger.pad_floor=0.05`) is clamped to 0.10 with no warning,
  so the experiment measures nothing. Remove the hard floor (validate `0 <= pad <= 0.5` in config load) or log when clamped.

### [MEDIUM] Config defaults in code disagree with `vajra_config.yaml` (silent-fallback hazard)  (orchestrator.py:91-97 etc.; app.py:91,131)  certainty: confirmed-by-running (script diffing 39+ `cfg.get(..., default=)` sites against the YAML)
| key | yaml | code default |
|---|---|---|
| expiry.trade_start | 09:21 | 09:30 |
| execution.post_stop_cooldown_secs | 660 | 180 |
| execution.post_profit_cooldown_secs | 15 | 120 |
| feed.poll_interval_secs | 0.25 | 1 (FreshnessEngine own default hold also 1800 vs yaml 1200) |
| feed.mode | live | synthetic |
| expiry.holidays_verified | true | false |
- A typo'd/missing YAML key does not fail; it quietly runs a *different* system (e.g. 1 Hz instead of 4 Hz — app.py:136 only warns for live). ~160 `cfg.get(...)` calls total (75 in orchestrator+app+feed+execution alone).
  Comments are also stale: "11-minute"/"660s" vs `post_stop` default 180 (orchestrator.py:~560), "post-2PM" vs `caution_after 14:30`, `freshness_engine.py` docstring "30 min" vs 20, "needs BREEZE_* env vars" in `run_vajra.ps1`.
- Fix: typed config (`@dataclass(frozen=True)` per section or pydantic), YAML is the single source of defaults, **unknown/missing key = startup error**, and the effective config (not the YAML text) goes in STARTUP_CONFIG.

### [MEDIUM] Blocking broker REST calls on the 4 Hz round thread  (vajra/feed.py:299-316, 254-272)  certainty: confirmed-by-reading
- `_step_maintenance` runs a `ThreadPoolExecutor` and **waits** (`ex.map`) for up to 20 `get_option_chain_quotes` calls inside `rounds()`. Pre-market these always fail and retry every 90 s, each retry stalling
  the generator for the slowest REST call (seconds). Result: periodic multi-second holes in the tape that look like feed freezes, and a stalled dashboard clock. Move to a daemon thread that publishes results via a queue
  drained non-blockingly at the top of each round.

### [MEDIUM] Tape durability / divergence  (vajra/tape.py:80-97, 115-129, 131-143)  certainty: confirmed-by-reading (+ profile)
- `flush()` `finally: self._buffers[name] = []` (:128-129) discards the buffered rows **even when the write failed** -> parquet permanently loses them while jsonl keeps them (only a logger line with no handler configured
  -> default lastResort stderr). Keep the buffer on failure, retry once, count failures, emit `ANOMALY PARQUET_FLUSH_FAILED`.
- Parquet footer exists only after `close()`; a hard kill/power loss leaves an unreadable parquet (jsonl is then the only truth, and `ReplaySource` correctly prefers it). Document, and have `verify` check for `.jsonl` presence.
- Per-row `fh.flush()` (:93, :113): `cProfile` over a 150-round synthetic day: `tape.append` = **55 % of `process_round` CPU** (10,964 appends, 1.6 s of 3.0 s; `json.dumps` 0.59 s). ~25 ms/round under the profiler
  on this box; a Windows laptop with Defender scanning the open file will be worse. Add `TapeWriter.commit_round()` that flushes each handle once per round.
- `path.rename(bak)` (:75) on Windows raises `PermissionError` if the old parquet is open in another program; it is swallowed by `flush()` -> that tape's parquet is silently dropped for the session.

### [MEDIUM] `replay.py` holiday fallback crashes with the shipped YAML  (replay.py:56-58 vs app.py:25-30)  certainty: confirmed-by-running
- `app.build_calendar` was hardened (`isinstance(val,(list,tuple))`, comment says exactly why); the **duplicate** in `replay.py` was not. `holidays_verified: true` + `k.startswith("holidays_")` ->
  `parse_holidays(True)` -> `TypeError: 'bool' object is not iterable` (reproduced). Hit whenever a tape dir has no `EXPIRY_RESOLVED` event. Fix: import `build_calendar` (duplication removal).

### [LOW] `ReplaySource` materialises the whole tape  (vajra/feed.py:80-93)  certainty: confirmed-by-reading
- `_read_rows` builds `list[dict]` of every row (240–285 MB parquet -> many GB of nested Python dicts; `.to_pylist()` on the parquet branch). `replay_browser.py` already streams via `iter_batches` — the two sources
  are not equivalent (replay_browser: no sort, swallows malformed rows at :54 `except Exception: pass`, drops that instrument for the round; ReplaySource: sorts, raises). Unify (section 2).

### [LOW] `prev_close_snapshot` needs `tick_tape.jsonl`; swallows the reason on failure  (vajra/feed.py:187-188, 214, 235)  certainty: confirmed-by-reading
- The seed looks only for `tick_tape.jsonl`; the review copy of the three days has parquet only (no jsonl), so the same code run against this copy would seed from the June file. On the operator machine 10-15/10-16 did **not**
  emit `STALE_PREV_CLOSE_SEED` (I grepped the event tapes: only 10-14), so the jsonl existed there — i.e. the A4 symptom is "first day", not a bug. Still: add a parquet tail fallback (`ParquetFile.read_row_group(last)`)
  and put the swallowed exception text into the `STALE`/`NO SEED` event so the cause is on tape.

### [LOW] Process left running past midnight / started after close  (vajra/feed.py:388, timeutil.py:60)  certainty: confirmed-by-reading
- `hhmm_reached(ts,"15:30")` uses seconds-into-day only. After 00:00 it is False again, so a live source that survived overnight keeps polling with yesterday's `session_date`/expiry/tape partition. Compare `ist_datetime(ts).date()` to `session_date` and stop.

### [LOW] Abrupt-basis detector is count-windowed, not time-windowed  (vajra/engine/basis_monitor.py:37,47-52)  certainty: confirmed-by-reading
- `std_window=30` *rounds* = 7.5 s at 4 Hz but 30 s at 1 Hz; with the 1-pt noise floor zeros dominate the window so `std` is tiny and any 1-pt tick of an option-parity basis (CE-PE quantised at 0.05 each)
  clears `k*std` -> the REVERSAL_WATCH flicker. Use a time-based window (e.g. 120 s) and require `std >= noise_floor/2`. (Moot while REVERSAL_WATCH is advisory, but it also drives `reversal_freeze_flag` on tape.)

---------------------------------------------------------------------------------------------------------------------------------

## 1b. Swallowed exceptions — each with its risk

| where | what is swallowed | risk | fix |
|---|---|---|---|
| config.py:143 `except Exception: pass` | **SyntaxError/ImportError in credentials.py** (a file hand-edited every morning) | typo in `SESSION_KEY` line -> falls to env -> "missing credentials" message, real cause hidden | catch `ImportError` only; let `SyntaxError` propagate with the line number |
| feed.py:156 `_feed_event` | any error from `tape.event` incl. `ValueError undeclared event_type`, closed tape | feed-side events vanish silently | log once + counter |
| feed.py:206 (`json.loads` skip) | torn line in jsonl tail | benign (tail read) | count |
| feed.py:214 / 235 `prev_close_snapshot` | *why* seed failed (PermissionError, MemoryError, parse) | falls to STALE June seed / NO SEED with no cause | include `repr(e)` in the ANOMALY/SESSION_ERROR |
| feed.py:260,306 + breeze.py:210 `option_step` | auth, network, rate-limit errors -> `None` | all report as `no_chain`; 20 stocks stay on 5.0 step | return `(None, reason)` |
| tape.py:125-129 | parquet write errors, **buffer discarded** | silent parquet/jsonl divergence | see MEDIUM above |
| tape.py:141 | close/flush of jsonl | acceptable | — |
| orchestrator.py:467 | per-instrument exception | good isolation, but only `type`+`str[:160]`, no traceback, only first per episode | `traceback.format_exc(limit=4)` in payload |
| orchestrator.py:801 | everything else (see MEDIUM) | silent session end, exit 0 | per-round budget |
| breeze.py:338 `disconnect` | ws close error | acceptable | — |
| breeze.py `_on_ticks` (no try at all) | **opposite problem**: an exception in `_normalize_tick` propagates into the SDK's socketio thread, which may kill the receive loop | whole feed dies on one weird tick | wrap per-tick, count + sample-log |
| replay_browser.py:54 | malformed row -> instrument missing from round | replay != engine replay | raise or count |

No bare `except:` exists; all are `except Exception`. No mutable default arguments (ruff B006: 0). No shadowed builtins (`dir`/`type` appear only as dataclass field names — `ContinuationSignal.dir`, `InstrumentWeight.type`; harmless but `dir` shadows a builtin inside those classes' scope: rename `direction`).

## 1c. Dead code / unused / duplicated (pyflakes + vulture + manual)
- Unused imports: `orchestrator.py:19` (`field`, `replace as _dc_replace`), `execution.py:22` (`MarketSnapshot`), `synthetic.py:25` (`csv`), `demo_dashboard.py:7` (`date`). Unused variables/params: `orchestrator.py:720` `fallback_band`
  (and all 3 callers pass it), `execution.py:180` `conviction`, `threshold`, `channels` (enter() signature carries three dead parameters), `execution.py:66` `action`, `freshness_engine.py:55` `spot`, `app.py:166` & `verify_vajra.py:242` `self_inner`.
- Schema enums defined but enforced nowhere: `RECALC_TRIGGERS`, `DEC_STATES`, `DECISIONS`, `ORDER_ACTIONS`, `ROUTING_MODES` (grep: zero uses outside schema.py). That is why `post_cooldown` "missing" from `RECALC_TRIGGERS`
  (operator note A3) never failed anything. Either enforce in `TapeWriter.append` (cheap set lookup) or delete. Unused `Reason.SESSION_ERROR/MISSING_TICK/STALE_CHAIN`.
- Event-name audit (AST/regex): every literal passed to `_emit_event/.event/_feed_event` is in `schema.EVENT_TYPES` and vice versa (no latent crash today) — but nothing enforces it, and an undeclared name raises inside `process_round` (see run() MEDIUM). Add the test (section 5).
- `import sys` repeated inside `run()` at orchestrator.py:765, 771, 777, 795 although `sys` is imported at module top (:16); `import logging` inside `flush()` (tape.py:116) and `resolve_codes` (breeze.py:166);
  `from collections import Counter` / `ThreadPoolExecutor` / `INDIA_VIX_CODE` imported per call. 32 PLC0415 hits; none are circular-import workarounds except the lazy `breeze_connect`/`uvicorn`/`fastapi` ones (keep those).
- Private-member coupling: `feed.py` calls `breeze._key()` (x4) and reads `breeze._unmapped_warned`; `orchestrator.py` reads `decision._bias_ewma.value`; `_equity_high` is created lazily via `getattr(self,"_equity_high",0.0)` (orchestrator.py ~1075) instead of `__init__`;
  `LiveBreezeSource._seed_source` is set in `prev_close_snapshot()` not `__init__`.
- Duplicated logic: (a) `compute_band(...)` with the identical 3 `cfg.get("trigger", …)` reads at orchestrator.py:311, :703, :732 -> build `self._band_kw` once; (b) the up/down-zone + nearest-level logic is written twice,
  `_approach_entry` (:911-923) and `_compute_alarm` (:980-990), and they already differ (`dist_up <= dist_dn` vs `abs(du) <= abs(dl)`) — the cockpit can in principle name a different side than the entry when both zones are live (narrow band — relevant once complaint A narrows it);
  (c) row->snapshot rebuild exists as `feed._snapshot_from_row` **and** `LiveBreezeSource._snapshot_from_tick_row` with different defaults (`float(s["ltp"])` vs `float(s.get("vwap") or s["ltp"])`);
  (d) replay sources: `ReplaySource`, `replay_browser.StreamReplay`, `realfeed.RealSessionSource`, `app._Static`, `demo_dashboard` loop; (e) calendar building in `app.build_calendar` vs `replay._read_expiry_from_events` (the crash above);
  (f) two Black-Scholes implementations (`synthetic.bs_price` r=0, `realfeed.bs_price` r=6.5%) — `realfeed` BS is now unreachable for sisters (comment says N/A).
- Long functions (AST): `process_round` 250 lines / 118 statements / cyclomatic 31 (orchestrator.py:423-672); `_process_instrument` 164 lines (:258); `DecisionMaker.evaluate` 133 (cyc 20); `_update_view` 107 (:1092);
  `Orchestrator.__init__` 97 lines, ~45 attributes; `_compute_alarm` 79; `ContinuationValidator.update` 85. JS: `async function tick()` is **~355 lines** (dashboard.py:638-993) and `_PAGE` is 955 lines (CSS 369, JS 586).
- Perf, honest numbers: `round_to` (Decimal) = **6.5 µs/call vs 0.38 µs** builtin. Hot-path call sites: `basis_engine:38`, `feed:360`, `pin_locator:29` = 3 per instrument per round = ~72/round = ~290/s at 4 Hz -> **~1.9 ms/s (0.2 % of a core)**. Not worth touching; keep Decimal (HALF_UP golden test needs it). If ever wanted:
  `math.floor(x/step+0.5)*step` is identical except for float-repr edge cases — do not. Larger real costs: `tape.append` (55 %), `snap.inferred_step()` sorts the key set for 24 instruments every round (feed.py `strike_step` + orchestrator:~270), `strikes_for()` re-sorts twice per instrument (`pin_locator`),
  and `RollingStats.push` uses `list.pop(0)` (use `deque(maxlen)`).
- Float equality: none problematic (grep). Epsilon via `_sign`. `compute_lots` uses `capital // (prem*lot)` — floor on a float product; at near-exact multiples float error can cost one lot (theoretical, not reproduced); use `math.floor(capital/(prem*lot)+1e-9)`.
- Type hints: mypy 51 msgs. Real ones: `Executor._close` dereferences `self.position` (Optional) unguarded (execution.py:264-275) — fine today because callers check, but make it a parameter; orchestrator `_nifty_band_inputs: None` assigned a tuple (:418) -> annotate;
  `normalize_vix(float)` called with `float|None` (breeze.py:298; same NaN root cause as HIGH-3). Public API is mostly un-annotated (213 ANN001): untyped `_process_instrument(self, inst, snap, st, ...)`, `Orchestrator.__init__(cfg, tape, ...)`.
  Introduce `typing.Protocol`s for `Tape`, `Source`, `Breeze` — it would also replace the `NullWriter.__getattr__` duck-type hacks in tests/replay_browser.

## 1d. Windows / timezone / locale
- Fine and should stay: all `open()` carry `encoding="utf-8"`; pathlib throughout; `ws_expiry` uses an explicit English month list (locale-safe); `strptime("%Y-%m-%d")` is locale-neutral; IST is a fixed-offset tz (no `zoneinfo`/tzdata dependency on Windows),
  all session logic is `ts_ms`-derived, `datetime.now(tz=IST)` used consistently, no `utcnow`/`date.today`. `TimeEWMA` clamps negative dt; `FreshnessEngine` uses `max(0,..)` (clock steps are survivable).
- `run_vajra.ps1`: sets `$ErrorActionPreference="Stop"` globally — on Windows PowerShell 5.1 native-command stderr (the engine writes `[VAJRA WARNING]`, heartbeat every 30 rounds, to stderr) can be promoted to a terminating `NativeCommandError` when stderr is redirected/merged.
  Set `Continue` around the `python` call. Also assigns to the automatic variable `$args` (line `$args = @(...)`; legal but fragile) — rename `$pyArgs`. Does not activate a venv or set `PYTHONUTF8`.
- `verify_vajra.py` prints raw ANSI colour escapes; legacy conhost shows `←[32m`. Strip colours when `not sys.stdout.isatty()` or `os.name=="nt"` without VT. It also runs on **every** start (16.5 s measured here) including mid-session restarts (10-16 had 4) —
  add `-SkipVerify` or cache a green result keyed on the source hash.
- `time.sleep` granularity on Windows is ~15.6 ms; at 0.25 s cadence fine. `Path.rename` onto/over open files fails (see tape.py). Default ws log files (`logs/apiLogs.log`) are written by the SDK relative to CWD — `run_vajra.ps1` does `Set-Location $PSScriptRoot` (good); `python -m vajra.app` from elsewhere scatters `logs/`.
- Not a git repo in this copy; `.gitignore` covers `vajra/credentials.py`, `.env`, `tapes/`, `logs/` (good) but not `vajra_results*.xlsx`, `*.pdf`, `.ruff_cache/`, `.mypy_cache/`.
- Time zone footgun in the cockpit: `T-{days} to expiry` (dashboard JS ~line 658) is calendar days, not trading days (Wed 14 shows T-5, engine says 3 trading days).

## 1e. Security
- **[HIGH-sev hygiene, certainty confirmed] Real broker secrets in plaintext** `vajra/credentials.py:14-16` (API_KEY, API_SECRET, SESSION_KEY; bandit B105 true positive). Mitigations present: file is in `.gitignore`; I searched every non-tape file (incl. `logs/`, event tapes, STARTUP_CONFIG snapshot)
  for the three values — **no leak** (`app.write_startup_events` correctly pops `breeze`). Risks: file mode is world-readable (`-r--r--r--`) in this copy; docstring says values were "copied from MYTHOS credentials" (two plaintext copies of a long-lived `api_secret`); this entire directory (credentials included) was handed to a 30-person review team and copied into review workspaces.
  Actions: (1) **rotate `api_secret`** (SESSION_KEY already expires daily) now that the folder has been shared; (2) `git log --all -- vajra/credentials.py` on the operator repo to prove it was never committed; (3) move to env vars/`keyring` (Windows Credential Manager); keep `credentials.py` only as a fallback,
  and `chmod 600`/`icacls` it; (4) startup check refusing to run live if the file is tracked by git or world-readable; (5) config.py:143 swallow (above).
- **[MEDIUM] Dashboard has no auth, `/docs` enabled** (dashboard.py:24). Verified with `TestClient`: `/docs`, `/redoc`, `/openapi.json` all 200; no CORS headers (good, so a hostile page cannot *read* responses cross-origin), no Host check -> DNS-rebinding can read `/api/state`
  (capital, open position, levels). Default bind is `127.0.0.1` (config `server.host`), but `app.py:199` will bind **whatever the YAML says** (0.0.0.0 would expose the whole cockpit to the LAN/VPN unauthenticated, with no warning). All endpoints are GET/read-only, so impact is information disclosure, not control.
  Fix: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`; `TrustedHostMiddleware(allowed_hosts=["127.0.0.1","localhost"])`; refuse non-loopback unless `server.token_env` is set and enforced as a bearer/cookie; `Cache-Control: no-store`; CSP `default-src 'self'`.
- Dashboard JS builds HTML with `innerHTML` in 28 places from engine strings (event `detail`, `exit_reason`, instrument names). Data is self-generated, so XSS is theoretical; when moving to `static/` add an `esc()` helper for the free-text fields (`detail`, `summary`, `reason`).
- `/sounds` StaticFiles: path traversal tested (`..%2f`, `../`) -> 404. OK.
- Live-order path: `LIVE_ORDERS` default false in code and yaml (good); `place_option_order` places a MARKET order with no price band — if ever enabled, add a limit-with-collar and a max-qty guard. `OrderWrapper.place` returns `fill=q.ask` even when ask is 0 -> falls back to LTP (fine) but spread is never checked (illiquid strike -> large paper slippage not penalised).

---------------------------------------------------------------------------------------------------------------------------------

## 2. Improvements / design proposals (ranked)

1. **Feed health first** (-> `vajra/feed_health.py`): per-key `rx_ms`; per-instrument `FeedHealth{spot_age_ms, opt_age_ms, frozen_s, spot_vs_future_gap_pts, ok, reason}`; rules: spot unchanged ≥ N s while ≥ M option LTPs changed -> `SPOT_FROZEN`;
   `|spot - implied_future| > max(5×median_basis_noise, 40 pts)` or implied-basis jump > X in one round -> `SPOT_IMPLAUSIBLE`; recover only after K clean consecutive rounds. Output is a pure function of tape fields (`ts`, spot, strikes) so it **replays deterministically**; the live-only part is `rx_ms`, so also record `spot_age_ms` into `tick_tape` (new nullable column) so replays see the same health. Gate: `fire` requires `health.ok`, `manage()` keeps trailing on option LTP only.
2. **Typed, strict config** (`vajra/config.py` -> dataclasses; unknown key = error; one place for defaults; effective config snapshot on tape and **replay loads the config recorded in the tape's STARTUP_CONFIG** so a historical replay is not silently run against today's YAML).
3. **Split `process_round`** into pure stages returning small dataclasses: `ingest -> per_instrument -> regime -> entry_decision -> execute -> publish`. `Orchestrator` becomes ~250 lines; `_update_view/_build_chain/_capital_block` move to `view.py`. Make the dashboard payload a typed dict built once per round and **pre-serialised to bytes** in the engine thread (`self.state_json`) so `/api/state` is a memcpy and cannot 500 inside the request thread.
4. **Dashboard to static files** (below), `/api/state` + optional SSE `/api/stream` (push on new `round_ts`; removes the 250 ms poll with no in-flight guard — `setInterval(tick,250)` at dashboard.py:995 issues a new `fetch` every 250 ms even if the previous one has not returned, so a slow engine thread gets 4 piled-up requests/s).
5. **Batch tape I/O**: `commit_round()`, `deque` stats, cache `inferred_step`, `orjson` for jsonl (≈3–5× faster `dumps`), parquet footer-safe writer (write a new row-group file per N minutes `tick_tape.0001.parquet`, concatenate lazily) so a crash never loses the day.
6. **One replay source** (`vajra/sources.py`): `ReplaySource(partition, stream=True)` using `iter_batches`, shared by `replay.py`, `replay_browser.py`, `demo_dashboard.py`, tests; delete `realfeed.py` BS code and `_snapshot_from_tick_row`.
7. **Alerts as a first-class layer** (`vajra/alerts.py`): engine emits `Alert(kind, level, key, ts, text)` with built-in **de-dup/hysteresis (per-key min-interval + require-clear)**; the tape event stream gets one row per *episode* (fixes the 1,351 `REVERSAL_WATCH` / 12× `FALSE_BREAK` floods at the source), the dashboard gets a policy table mapping `kind -> sound|none`.
8. **Static-quality gate in CI/verify**: replace verify gate 1 ("no TODO/pass-only", easily gamed) with `ruff check` + `mypy --strict` on `vajra/engine` + the event-name/reason test (section 5). Add `pytest` and `httpx` to a `requirements-dev.txt`; pin runtime deps (`requirements.lock`); drop unused `numpy`, `python-dateutil` (grep: no imports).

### Proposed v2 layout
```
vajra/
  config.py            # typed dataclasses, strict, effective-config export
  clock.py             # (timeutil) session calendar + IST helpers, session-date guard
  feed/
    breeze.py          # transport only: connect, subscribe, rx_ms stamping, reconnect/on_disconnect
    live.py            # LiveBreezeSource (round assembly; step resolution in background thread)
    sources.py         # ReplaySource (streaming), SyntheticSource
  feed_health.py       # NEW: frozen-spot, spot-vs-implied-future plausibility, per-instrument ok flag
  engine/
    basis.py pin.py band.py freshness.py continuation.py basis_monitor.py
    confluence.py      # (decision_maker minus dead FIRE paths): weights, bias_ewma, same/opp pull
  regime.py            # NEW: expiry vs non-expiry regime -> band params (half-width cap, pad, proximity, size multiplier); single place that answers complaint A
  alerts.py            # NEW: Alert dataclass, hysteresis/de-dup, policy (alert kind -> event tape / sound / cockpit)
  execution.py         # Executor + OrderWrapper (unchanged logic; drop 3 dead params; guard Optional position)
  orchestrator.py      # thin: stages + loop with failure budget
  view.py              # latest_state builders (+ NaN-safe serialisation, pre-encoded bytes)
  tape.py  schema.py   # batched commit, enforced enums
  server.py            # FastAPI: /api/state, /api/stream, static mount, no docs, TrustedHost
  static/
    index.html  app.css
    js/ state.js  render_nifty.js  render_board.js  render_journal.js  render_chain.js
        alerts.js (sound policy, edge detection)  watchdog.js  util.js
    sounds/  (the mp3s; move out of vajra/sounds)
tests/ …
```
`regime.py` should expose a pure function `band_params(regime, spot, extrinsic, minutes_to_close) -> BandParams` so replays can sweep it (details in section 3).

### Splitting the dashboard into static files (concrete)
- `server.py`: `app.mount("/static", StaticFiles(directory=STATIC), name="static")`; `@app.get("/") -> FileResponse(STATIC/"index.html", headers={"Cache-Control":"no-cache"})`. `_PAGE` is a **raw string with no `%`/`{}` templating** (`r"""`), so extraction is mechanical: lines 46-~415 (CSS) -> `app.css`; HTML body -> `index.html`; the 586-line `<script>` -> ES modules (`<script type="module">`).
- Split `tick()` (355 lines) into `fetchState() -> render(state)` where `render` calls 8 small `renderX(state)` functions; keep a single `store = {prev, cur}` so edge-detectors (sound, cooldown, breach) compare `prev` vs `cur` instead of module-level `_lastSide/_lastEntry/_breached/_lastExit` globals (dashboard.py:~458 and 968-989).
- Benefits: browser caching (68 KB page re-sent on every reload today), diffable JS, lintable with `eslint`, testable with a headless fixture of `latest_state` JSON (record one from a replay), no Python string escaping hazards, CSP possible (`script-src 'self'`; today it needs `'unsafe-inline'`).
- Add a `FastAPI` test that `GET /` references only `/static/*` assets that exist, and that `/api/state` survives NaN.

### Proposed `ruff.toml` (tested: loads, reports ~200 findings on today's tree, ~40 auto-fixable)
```toml
target-version = "py313"
line-length = 120
extend-exclude = ["tapes", "tapes_replay", "logs", "vajra/credentials.py"]

[lint]
select = ["E","W","F","I","UP","B","BLE","S","C4","SIM","PERF","PLW","PLC0415",
          "C901","PLR0912","PLR0915","ARG","RUF","DTZ","PTH","T20","TRY400","A"]
ignore = ["E501","S101","S311","TRY003","RUF001","RUF002","RUF003"]

[lint.mccabe]
max-complexity = 12
[lint.pylint]
max-statements = 60
max-branches = 15

[lint.per-file-ignores]
"tests/*" = ["S","ARG","PLR","DTZ","T20"]
"replay*.py" = ["T20"]
"verify_vajra.py" = ["T20","C901","PLR0912","PLR0915"]
"vajra/app.py" = ["T20"]

[format]
quote-style = "double"
```
(`/home/claude/vajra2_review/scratch/22/ruff.toml`). Ratchet: land with `--add-noqa` for the legacy 4 complexity hits, then forbid new ones. Add `mypy --strict vajra/engine vajra/mathx.py vajra/marketdata.py` (these are already near-clean).

---------------------------------------------------------------------------------------------------------------------------------

## 3. Recommendations specific to the two operator asks

**A. Non-expiry productivity — code-level points that affect any solution**
- `trigger_engine.py:62` hard floor `pad >= 0.10` and the three duplicated `compute_band` call sites mean a regime-aware band must be implemented once: `regime.band_params()` consumed by a single `Orchestrator._band_for(snap, atm, basis, mode, step)` (replace :311/:703/:732). Today there is nowhere to hook "cap half-width at X".
- Suggested mechanics for `regime.py` (values to be measured on the three tapes by the lead; my defaults): on non-expiry days compute `raw_half = (upper-lower)/2`; clamp each level to `spot ± min(raw_half, H)` with `H = clamp(k·ATR_30m, 25, 45)` pts (start `H=35`), **keep the ITM-VWAP level when it is already inside H** (so expiry days are untouched), and re-anchor on *spot excursion* (`|spot - anchor| > 0.5·H`) in addition to the 20-min timer so a clamped band follows price instead of being left behind. Size multiplier `0.5` off-expiry via `Executor.compute_lots(…, mult)` so `trade_small` finally means small (today it only opens the gate, `_expiry_gate_ok` orchestrator.py:~172).
- `proximity_pts: 7` is wrong to leave fixed once bands narrow: express as `max(5, 0.2·H)`.
- All of this must be a **pure function of tape fields** (spot, chain VWAPs, ts) to preserve replay determinism; no wall-clock.
- Do not tune until A2 is fixed: the 15:20 fiction trades are in every day's P&L and would pollute any sweep. Exclude `[feed_health.not_ok]` rounds from the measured P&L.

**B. Sounds — code-level points**
- Sounds are decided in browser JS from state diffs (dashboard.py:454-474, 968-989), i.e. policy is mixed with rendering and keyed to globals. Move the policy to the engine (`alerts.py`: `Alert.sound ∈ {None,"entry","exit_win","exit_loss","risk"}`) and have the page only play what the alert says; then the policy is testable and replayable, and reload does not re-trigger (the JS currently primes `_lastExit` on first sight — keep that idea server-side via alert ids).
- Today's six sounds map to 6 edge-detectors; my recommended minimum is **3 audible classes**: (1) *entry taken* (the only "act now" sound), (2) *exit* — one tone, pitch/duration differs win vs stop (or two files), (3) *system-risk* (feed frozen / levels stale / SHUTDOWN) — distinct, rare, repeats every 60 s until cleared. Approach/sister-breach become **visual only** (banner colour + title-bar blink); they are the "ting ting ting" because `side!==_lastSide` re-arms every time `a.lean` flickers between `ABOUT_TO_BREACH` and `WATCHING` at a 7-pt boundary. If an approach cue is kept, give it **hysteresis** (arm at ≤7 pts, re-arm only after ≥15 pts away *and* ≥120 s) — the same hysteresis class as the FALSE_BREAK/REVERSAL_WATCH floods.
- `a.state` flicker is also why `playSnd` re-fires: `currentTime=0; play()` restarts a still-playing clip, so jitter = machine-gun. Add `if(!a.paused) return`.
- `localStorage('vajra_sound')` persistence is fine (browser-side convenience); autoplay-unlock logic at :~470 is correct.

---------------------------------------------------------------------------------------------------------------------------------

## 4. Things that are fine and should NOT be changed
- `mathx.round_to` Decimal HALF_UP (6.5 µs; ≈0.2 % CPU at 4 Hz). Golden tests depend on it. Leave.
- Determinism discipline: all engine time from `ts_ms`; sorted instrument iteration (orchestrator.py ~line 452); `TimeEWMA` on tape time. Replay equality is a genuine asset.
- Per-instrument exception isolation (:467) and the NaN spot guard (:264); `manage()` refusing to fabricate a premium (execution.py:221-224); `force_flatten` not booking a 0.0 exit.
- `tape.event` rejecting undeclared event types (self-enforcing contract) — keep, but add the static test so it cannot fire at runtime.
- Locale-safe `ws_expiry`; explicit UTF-8 on every `open()`; fixed-offset IST; `.bak` preservation on same-day restart (just fix the Windows rename edge).
- `LIVE_ORDERS: false` default in both code and YAML; secrets popped from STARTUP_CONFIG; localhost default bind.
- Phantom-fill guards (`OrderPlacementError` -> `order_id None`); the one-position rule; post-stop cooldown concept (operator-locked).
- Lazy imports of `breeze_connect`, `uvicorn`, `fastapi` (keeps tests/engine importable without the SDK).

## 5. Tests I would add
1. `test_event_contract`: AST-scan all `_emit_event/.event/_feed_event("X", …)` literals ⊆ `schema.EVENT_TYPES`; all `Reason.X` references exist; every `recalc_trigger` string written ⊆ `RECALC_TRIGGERS` (and make the enum enforced in `append`).
2. `test_dashboard_http`: `TestClient` — `/api/state` with NaN/inf -> 200 + nulls; `/docs`, `/openapi.json` -> 404; `Host: evil.example` -> 400; `GET /` assets all resolve (after the static split).
3. `test_breeze_normalize`: VIX tick without `last` -> `None` not NaN; tick without `last` but with `close` -> ltp **None**; zero bid is not resurrected; per-key `rx_ms` stamped; `on_disconnect` flips `connected`.
4. `test_feed_health` (golden): replay the three real days' 15:10–15:25 window -> `SPOT_FROZEN` raised within ≤10 s of 15:15:00, `SPOT_IMPLAUSIBLE` on the +185/+240/−134 jump; no false positives over 09:20–15:10 on all three days; `fire` blocked while not ok.
5. `test_run_budget`: monkeypatch `decision.evaluate` to raise once -> session continues, `ANOMALY ROUND_ERROR` on tape; raise 6× -> flatten + `SHUTDOWN fatal:*` + non-zero exit; raise in `executor.manage` with open position -> immediate flatten.
6. `test_config_strict`: YAML with a typo'd key fails startup; every `cfg.get` default equals the YAML value (the script I used, as a test); `pad_floor=0.05` is honoured (or rejected loudly).
7. `test_replay_calendar_fallback`: tape dir without `EXPIRY_RESOLVED` + `holidays_verified: true` replays (regression for replay.py:58).
8. `test_tape_flush_failure`: force `write_table` to raise -> buffer retained, `PARQUET_FLUSH_FAILED` event, jsonl intact; Windows-style `PermissionError` on `.bak` rename handled.
9. `test_banner_cp1252`: run `app.main` banner with `PYTHONIOENCODING=cp1252` -> no crash.
10. `test_zone_consistency`: property test that `_approach_entry` and `_compute_alarm` choose the same side for random (spot, upper, lower); they can disagree when both zones are live (signed `dist_up<=dist_dn` vs `abs(du)<=abs(dl)`) — not reproduced, found by reading.
11. Alert hysteresis tests (`alerts.py`): 1,000 jittering rounds around a 7-pt boundary produce ≤ 1 alert per episode; recovery re-arms only after the clear distance/time.
