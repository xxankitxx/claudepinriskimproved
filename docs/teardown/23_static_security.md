# 23 — Static analysis & security: teardown
Verdict: Ready for Monday as configured (paper-only holds with the shipped `LIVE_ORDERS: false`, no secret leaks found, dashboard read-only on 127.0.0.1). But the paper-only promise is a single loosely-coerced boolean with no fail-safe: it is not *guaranteed*. Slightly better than 1.0 overall; the LIVE_ORDERS weakness is unchanged from 1.0, and the new `.token` path brings a new Windows hazard.
Score (your area): 1.0 = 6/10, 2.0 = 7/10

## Findings (most severe first)

### [HIGH] Paper-only is not guaranteed: `LIVE_ORDERS` uses `bool()` coercion, nothing else guards it, and the banner says "(paper)" no matter what (vajra/config.py:101, vajra/execution.py:105, vajra/app.py:150, verify_vajra.py:328-354) — reproduced
- The only thing standing between the engine and real ICICI orders is `bool(cfg["execution"]["LIVE_ORDERS"])`. In live mode `breeze` is always connected (it is the data feed), so `OrderWrapper.place` (execution.py:77) calls `place_option_order`, which calls `BreezeConnect.place_order`. That means **every entry and exit goes to the broker at full-capital size**.
- `bool()` of any non-empty string is True. Ran `scratch/23/cp` on a copy of the config with dummy creds:
  ```
  'false'  cfg.live_orders= True  Executor.live_orders= True
  'False'  cfg.live_orders= True  Executor.live_orders= True
  'no'     cfg.live_orders= True  ...
  '0'      cfg.live_orders= True  ...
  'off'    cfg.live_orders= True  ...
  False / 0 / off (unquoted) -> False
  ```
  So one stray pair of quotes (`LIVE_ORDERS: "false"`) during a config edit turns live routing ON.
- The completion gate does not catch it. Gate 6 only complains about values that `is False` and are not on the allow-list. `LIVE_ORDERS: true` and `"false"` both pass. No test anywhere asserts `live_orders is False` (`grep -rn -i live_orders tests/` returns nothing).
- The startup banner prints `LIVE_ORDERS={cfg.live_orders} (paper)`, with "(paper)" hard-coded, so a misconfigured run shows `LIVE_ORDERS=True (paper)`. README.md:48 also invites the operator to "flip `execution.LIVE_ORDERS: true`".
- No input path can flip it: there is no CLI flag, no env var and no dashboard endpoint for it, and `cfg.raw` is only mutated for `strike_step` (feed.py:292,337). Replay, real and synthetic modes never construct Breeze (app.py:128), so they are paper even if the flag is true. The risk is live mode plus a mistyped or edited yaml.
- Same code as 1.0 (config.py and execution.py are identical on this point). It is not a regression, but rule 2 says paper must be guaranteed and today it is not.
- Fix (about 10 lines):
  1. `live = cfg.get("execution","LIVE_ORDERS"); live_orders = live is True`, and raise if the value is not a bool.
  2. Additionally require an explicit `--i-understand-live-orders` CLI flag or `VAJRA_ALLOW_LIVE_ORDERS=YES`, or else force paper and emit a `LIVE_ORDERS_BLOCKED` event.
  3. Add a gate in verify_vajra.py that FAILS when `LIVE_ORDERS is not False`.
  4. Print `PAPER` or `!!! LIVE ORDERS !!!` based on the value, not a constant.
  5. Optionally, have `OrderWrapper` take the breeze handle only when live is on.

### [MEDIUM] `vajra\.token` written the Windows way breaks the live start (vajra/config.py:147-150) — reproduced
- `read_text(encoding="utf-8")` sits outside the try/except. Tested with dummy files:
  ```
  utf8:                          repr='11112222'       ok=True
  utf8-bom (Notepad/PS7 Out-File): repr='﻿11112222' ok=False   -> wrong token -> 5 connect retries (~20 s) then BreezeConnectionError
  utf16le-bom (PS5.1 `>` / Out-File default): RAISED UnicodeDecodeError -> uncaught traceback before connect
  ```
- The docs (RUN_LIVE.md:18, OPERATOR_MANUAL.md:69, CHANGELOG.md:113) tell the operator to "write a one-line `vajra\.token`". On Windows PowerShell 5.1 the natural `echo 12345678 > vajra\.token` produces UTF-16LE. On Monday that means a crash at 09:1x or a confusing auth failure.
- Fix: `txt = tok.read_bytes().decode("utf-8-sig", errors="ignore")`, try `utf-16` first if the file starts with BOM `FF FE`, strip `\x00` and whitespace, and validate `^\w{6,}$`. Log which *source* supplied the token (`--token` / env / .token / credentials.py), never its value.

### [MEDIUM] Token precedence can silently pick a stale key, and the failure doesn't say which source was used (vajra/config.py:143-156, run_vajra.ps1:41) — read in code
- The order is env `VAJRA_SESSION_KEY` > `vajra\.token` > `credentials.py`. If the operator ever ran `setx VAJRA_SESSION_KEY …` (the launcher suggests that env var), or left yesterday's `.token` in place, that stale value silently overrides a freshly edited `credentials.py`. The connect then fails after about 20 s with `Breeze connect failed after 5 attempt(s)`, with no hint which source was used.
- A blank env var (`"  "`) correctly falls through (tested).
- Fix: print `session token source: <name> (len N)` at startup. Warn when `.token` mtime is older than today. Have `--token` write nothing to disk (it already writes nothing).

### [LOW] `--token` on the command line goes into PowerShell history and the process list (app.py:236-241, run_vajra.ps1:51) — read in code
- PSReadLine keeps `.\run_vajra.ps1 -Token 12345678` in plaintext in `ConsoleHost_history.txt`, and the token is visible in the Windows process command line. The token expires daily, so the exposure is small on a single-user box.
- The token is not leaked to tapes, logs or banners. STARTUP_CONFIG pops the `breeze:` block (app.py:51). The banner and `latest_state` have no secret fields (listed every key). I checked whether the `credentials.py` values appear anywhere in 1.0's `logs/apiLogs.log`, `logs/websocketLogs.log` and every `tapes/*/*.jsonl`; the script printed booleans only: all False.
- A `credentials.py` SyntaxError message shows only the message, file and line, never the line text (tested with dummy creds).
- Fix (optional): accept `-Token` via `Read-Host -AsSecureString` when the value is omitted.

### [LOW] Dashboard has no Host check (DNS rebinding) and renders event text through innerHTML unescaped (dashboard.py:51-106, 959-962; orchestrator.py:309-311) — reproduced / read in code
- Probed a stub app on :8923:
  - `/docs` and `/openapi.json` return 404 (1.0 exposed `/docs`).
  - `POST /api/state` returns 405, and there are no state-changing routes at all (GET `/`, `/api/state`, `/api/stream`, `/api/health`, static `/sounds`).
  - No `Access-Control-*` headers, so cross-origin reads are blocked.
  - `/sounds/../dashboard.py` and its encoded variants return 404.
  - Bound to `127.0.0.1` from config.
- `Host: evil.example` is accepted (200), so a rebinding page could read trading state. That is read-only and not secret.
- Event `summary` includes `repr(e)` from broker or feed exceptions and is inserted with innerHTML. A broker HTML error page would break the events strip. There is no direct XSS source the operator doesn't control.
- Fix: add `TrustedHostMiddleware(allowed_hosts=["127.0.0.1","localhost"])` and escape `e.summary` / `e.type` in JS. If `server.host` is ever set to `0.0.0.0`, the dashboard has no auth, so keep the default.

### [LOW] Broken or dead code
- **`tools/latency_bench.py:27` is a SyntaxError** (reproduced with pyflakes, ruff and `compile`):
  ```
  pf = pq.ParquetFile("str(Path(__file__).resolve().parent.parent / "tapes" / "2026-10-15" / "tick_tape.parquet")")
  ```
  This is a botched path substitution. CHANGELOG.md:78 cites measurements "from `tools/latency_bench.py`", but the shipped tool cannot run, so that claim cannot be reproduced from this snapshot. Fix: remove the outer quotes.
- **Unused imports.** pyflakes found the same 6 in 1.0 and 2.0, plus 4 new ones in 2.0: `tests/test_v2.py:9 orch_mod`, `tools/make_real_fixture.py:5 json`, `tools/make_sounds.py:5 os`, and the syntax error above. The 1.0 leftovers are `orchestrator.py:19 field, _dc_replace`, `execution.py:22 MarketSnapshot` and `synthetic.py:25 csv`.
- **Unused parameters.** vulture: `orchestrator.py:868 _recompute_nifty_band(fallback_band)` and `execution.py:184 enter(channels)`. These are harmless leftovers from 1.0.
- **mypy:** 59 errors in 2.0 versus 39 in 1.0. All are Optional/None narrowing noise (e.g. `round(basis)` where `basis` is checked earlier and the function returns, `_band_kw` typed `dict[str,float]` holding an int). I found no real type bug.
- **Mutable defaults:** none (searched `def …=[]/{}/set()`).

### [LOW] Broad excepts: none hide a trading-critical error — read in code
- 0 bare `except:` in both versions. 2.0 has 25 `except Exception` (1.0 has 21).
- Every new one either logs or emits an event:
  - FEED_GAP with `repr(e)`: feed.py:286, 317, 321, 446, 455.
  - ANOMALY INSTRUMENT_ERROR, once per instrument: orchestrator.py:572.
  - SESSION_ERROR: orchestrator.py:925 and 950, after which flatten still runs in `finally`.
  - Logged parquet failure, with jsonl still written: tape.py:73.
- The silent ones are cosmetic:
  - orchestrator.py:112: a calendar failure falls back to calendar-day DTE, which is display-only. regime.py hard-codes `capital_fraction=1.0, max_trades=0, loss_stop=0` either way.
  - config.py:160: a `credentials.py` ImportError or NameError falls through to the "missing credentials" RuntimeError, which is loud enough.
  - breeze.py:338: disconnect.
- One small thing: `breeze.py:331` should `raise … from None` (ruff B904).

### [LOW] Launcher dropped `$ErrorActionPreference = "Stop"` (run_vajra.ps1, diff vs 1.0) — read in code
- Python exit codes are still checked for verify. Otherwise this is harmless. Mentioned only for completeness.

## Python 3.13 / Windows compatibility (fine)
- No deprecated or removed APIs: no `utcnow`, `get_event_loop`, `imp`, `distutils`, `cgi`, `telnetlib` or `pkg_resources`.
- All files compile under `-W error`, except latency_bench.
- Importing app, dashboard, orchestrator, feed and breeze with `-W error::DeprecationWarning` succeeds.
- IST is a fixed `timezone(timedelta(5h30m))`, so it needs no tzdata on Windows.
- Every text `open`/`read_text` passes `encoding=`.
- The only non-cp1252 console output is the "⚡" banner, which `_say()` guards. The launcher also sets `PYTHONIOENCODING=utf-8`.

## What 2.0 does better than 1.0 (evidence)
- Dashboard: `docs_url/openapi_url=None` (1.0 had `/docs`). NaN-safe JSON (`_clean`) instead of HTTP 500. SSE is GET-only and has no write endpoints.
- `vajra/.token` was added to `.gitignore`.
- Replays write to `tapes_replay/` instead of mutating recorded tapes (app.py:122).
- `--date` is refused in live mode, and so is a non-trading day.
- A `credentials.py` SyntaxError is now reported loudly, without leaking the line.
- Console banner can't die on a cp1252 console.
- Instrument error latch prevents 4/s event spam.

## What 2.0 does worse than 1.0 (evidence)
- The new `.token` path breaks on Windows-encoded files (UTF-16 crash or BOM mismatch, reproduced). The 3-source precedence can silently prefer a stale token.
- `tools/latency_bench.py` ships with a SyntaxError while CHANGELOG cites its numbers.
- There are more lint findings (4 new unused imports, mypy 39 → 59), all noise.
- LIVE_ORDERS handling is **unchanged** (same weakness as 1.0).

## Tests / checks you ran
- `python3 -B -m pyflakes` on both trees, excluding credentials.py.
- `ruff check --select F,B,E9,PLE,PLW,S,ASYNC,BLE`; `vulture --min-confidence 60/80`.
- `python3 -B -m mypy --ignore-missing-imports vajra` on both trees (2.0: 59 errors / 6 files; 1.0: 39 / 3).
- `compile()` of all files with `-W error`; import smoke with `-W error::DeprecationWarning`; grep for 3.13-removed APIs, non-encoding opens and non-cp1252 prints.
- `scratch/23/cp/` (a copy with DUMMY credentials):
  - the LIVE_ORDERS coercion matrix;
  - the `.token` utf8 / utf8-bom / utf16 matrix;
  - blank-env fallthrough;
  - a SyntaxError message leak check.
- `scratch/23/leakcheck.py`: searched 1.0 logs and all jsonl tapes for the credentials.py values, printing presence booleans only (all False). No values were printed.
- `scratch/23/dash.py`: the real `build_app` on 127.0.0.1:8923 with a stub orchestrator, probing routes, POST, CORS, Host header and `/sounds` traversal.
- `python3 -B -m pytest -p no:cacheprovider -q -x` on 2.0: **101 passed** (267 s). None of those tests cover the LIVE_ORDERS gate.
