# 09 — Startup, CLI, credentials/token, run scripts: teardown
Verdict: Startup is better than 1.0 and Monday-capable on the happy path. Wrong token, missing tapes, the weekend/--date guards, the encoding-safe banner and the PS 5.1 script all behave. The weak spots are a mid-day restart (state is lost, and the manual calls it "safe"), a fragile `.token` reader, silent stale-token precedence, and a dashboard port clash that goes unnoticed. No BLOCKER found.
Score (your area): 1.0 = 5/10, 2.0 = 7/10

## Findings (most severe first)

### [HIGH] Mid-day restart resets capital, position and cooldowns; the docs call restarts "safe" (execution.py:107-108, orchestrator.py:199, OPERATOR_MANUAL.md:105) — read in code
- `ExecutionEngine.capital = starting_capital` (Rs 1,00,000) on every construction. `_last_stop_ms = 0`, so there is no 11-min cooldown after a restart. Nothing reads today's order_tape or event_tape at startup (`grep order_tape|restart` finds nothing in the orchestrator or execution code).
- Scenario: the engine is at Rs 1.4L after two winning trades. It crashes or is restarted at 11:00, and the next entry is sized on Rs 1.0L. That breaks binding rule 1 (running P&L compounds through the day). If the restart follows a hard-stop loss, the engine can re-enter immediately, which breaks rule 5. An open position is orphaned. The manual admits the position is lost but not the capital or cooldown.
- 1.0 has the same limitation, so this is not a regression. It is still the most likely real-world incident, because the operator restarts when something breaks.
- Fix: on live start, replay today's `order_tape.jsonl` (ENTRY/EXIT rows) to restore `capital` and `_last_stop_ms`/`_last_profit_ms`. At minimum print the last known capital and cooldown from the jsonl, and fix the manual wording. A same-day restart also leaves only the post-restart session in `tick_tape.parquet`; the earlier part is in `*.bak`.

### [MEDIUM] `vajra/.token` is read with strict UTF-8 and no BOM handling (config.py:~125) — reproduced
- PowerShell 5.1 `"123" > vajra\.token` and `Out-File` write UTF-16LE with a BOM. Command run: `printf '12345678\r\n' | iconv -t UTF-16 > vajra/.token`, then a live start. Result: `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff` traceback after the tape was opened.
- A UTF-8-with-BOM file (some Notepad saves) returns `'﻿12345678'` (len 9). I ran `breeze_secret('session_token')` directly and it returned that string. `.strip()` does not remove the BOM, so the broker rejects it and the operator burns the 5-attempt, 21 s retry.
- Fix: read bytes and decode with `utf-8-sig` (fall back to `utf-16`). Keep only `[0-9A-Za-z]` characters, and accept a pasted `apisession=NNN` or a full URL by parsing out the `apisession` value. Wrap the read in try/except and print a clear message.

### [MEDIUM] Stale token wins silently; the token source is never printed (config.py token precedence) — reproduced
- Precedence is `--token` > env > `vajra/.token` > credentials.py. A leftover `.token`, or a persistent `VAJRA_SESSION_KEY` from a `setx`, overrides a token the operator just pasted into credentials.py, which the docs still say works. I confirmed that a `.token` holding 11111111 is used instead.
- The result is a 21 s retry followed by a traceback, with no hint about which source was used. A pasted `apisession=12345678` is accepted unvalidated (len 19).
- Fix: log "session token from <source>, ending ...<last2>" at start and in the tape. Warn if `.token` is older than today (check mtime) or if the value is not 6–10 digits.

### [MEDIUM] Dashboard bind failure is silent; no single-instance guard (app.py:_start_dashboard) — reproduced
- With :8766 occupied, the uvicorn thread dies after one `ERROR: ... address already in use` line. The engine keeps running and the banner still prints `http://127.0.0.1:8766`. The operator then has no dashboard, or is looking at the dead old engine's page. I reproduced this: the run took 20 s and produced a tape with no dashboard.
- This is a realistic mid-day-restart case: the old process hangs or its window is still open. Nothing stops two engines appending to the same `tapes/<date>/` jsonl files and racing the `.bak` renames.
- Fix: before opening the tape, try `socket.bind` on the port and abort with "VAJRA already running?". Or check `server.started` after about 2 s and print a loud warning.

### [LOW] Failed live connect leaves an orphan tape and a bare traceback
- A bad token gives 5 attempts with 2+4+6+8 = 20 s of backoff, then `BreezeConnectionError` and a traceback (exit 1). `SESSION_ERROR` is written to event_tape, which is good. The tape is not closed, and a futile retry is made even for an authentication error. Reproduced with a fake `breeze_connect` that rejects the session (real SDK not installed here).
- Fix: catch `BreezeConnectionError` in `main()` and print one line with the token hint. Do not retry when the error text says "authenticate".

### [LOW] credentials.py typo gives a repeated, slightly misleading error
- Reproduced with `SESSION_KEY = 01234567`, a leading-zero SyntaxError. The syntax error is printed 2-3 times (once per `breeze_secret` call), then `RuntimeError: ... fill in credentials.py`. It is loud and exits 1, which is much better than 1.0, but even a good `--token` cannot rescue it because API_KEY and API_SECRET also live in that file. That is correct behaviour, but the message should say so.
- Other exceptions in credentials.py (NameError, ImportError) hit `except Exception: pass` and fall through to the generic "missing credentials" message with no detail.

### [LOW] Banner hard-codes "(paper)"; nothing refuses LIVE_ORDERS=true (app.py:150)
- The banner prints `LIVE_ORDERS={cfg.live_orders} (paper)` even if the flag is true. The brief says paper only. Fix: refuse to start, or print a red warning, when `live_orders` is true.

### [LOW] Replay oddities
- `--mode replay` with no `--date` and no tape for today "succeeds" with an empty `tapes_replay/<today>` dir. It should error if `<tapes>/<date>` is missing. The date typo `--date 2026-13-45` gives a raw ValueError traceback.
- A second replay of the same date appends to the first one's jsonl files. After two runs, `order_tape.jsonl` had 16 lines against 8 parquet rows. The parquet files are `.bak`'d correctly. Fix: write to a fresh `tapes_replay/<date>/run-<ts>/` or truncate the jsonl.
- Replay starts the dashboard on :8766 by default, which collides with a live engine.
- The replay root is `tape_root.parent / "tapes_replay"`, so a custom tape root such as `tests/fixtures` writes to `tests/tapes_replay`. The default layout is fine.

### [LOW] orchestrator.sh is stale and not useful on Windows
- Its header says "needs BREEZE_* env vars". It has no `--token` or skip-verify option and checks only that credentials.py exists. It uses `python`, not `python3`. Syntax is OK (`bash -n`). Irrelevant to the Windows operator.

### [LOW] Cosmetic or environment
- verify_vajra.py prints ANSI colour codes, which may show as raw `←[32m` in the legacy PS 5.1 console (plausible, not tested). No tests cover the CLI, token precedence or the banner (grep of tests/ finds only `build_calendar` imports).
- No execution-policy note anywhere in the docs. A default Restricted client policy blocks `.\run_vajra.ps1`, so the operator needs `powershell -ExecutionPolicy Bypass -File .\run_vajra.ps1` (same as 1.0).
- requirements.txt is byte-identical to 1.0. numpy and python-dateutil are listed but not imported anywhere. `breeze-connect>=1.0.59` is unpinned and was not installable or testable here.

## What 2.0 does better than 1.0 (evidence)
- `--token` / `VAJRA_SESSION_KEY` / `.token` remove the daily edit of credentials.py. Verified by a fake-SDK run: the token reached `generate_session` as a `str` of 8 characters.
- `_say()` survives non-UTF-8 consoles. Run with `PYTHONIOENCODING=cp1252:strict` it printed `? VAJRA 2.0 SYNTHETIC ...` instead of crashing as 1.0's `print` does. The ps1 also sets `PYTHONIOENCODING=utf-8`.
- The `--date` live guard works. `--mode live` today (Sat 17 Oct) aborts with "NOT a trading day" before any tape is created (exit 1). `--mode live --date 2026-10-16` is refused.
- A SyntaxError in credentials.py is now loud, where 1.0 swallowed it.
- A same-day restart renames the parquet to `.bak` and never truncates.
- A missing tapes folder is fine. The synthetic run created `tapes/2026-06-02` with `mkdir(parents=True)`. In live mode the empty tapes dir gives a CRITICAL "NO prev-close seed ... first calc at 09:20" on stderr and an event, then continues. Entries start at 09:21, so there is no loss.
- Seeding from real Friday data works: `live_tape_parquet:2026-10-16` in 7.5 s under load 24, reading only the tail row-groups.
- Monday 19 Oct resolves correctly: trading day, expiry 19 Oct, IMPLIED, `shifted_from_holiday=True`. Tue 20 Oct is not a trading day.
- The completion gate passes (7/7, 6.7 s here), and `-SkipVerify` is available for restarts.
- run_vajra.ps1 reads correctly for PS 5.1:
  - It is ASCII-only with no CR characters and balanced `{}`, `()` and `[]`.
  - `$args` is no longer reassigned (it is `$pyArgs`).
  - `$ErrorActionPreference="Stop"` is removed, so stderr heartbeat lines from python cannot abort the script.
  - `python @pyArgs` splatting is valid.
  - The `-not (Test-Path ...)` chain parses.
  - `-Token` is a `[string]`, so tokens with leading zeros survive.
- pwsh is not installed, so this is reasoned from syntax and not executed.
- The banner now shows regime, DTE, band method, last-entry time and cadence. It warns when the live cadence is above 0.5 s.
- GC freeze plus raised thresholds are applied only in live mode.
- A replay never writes into the recorded day.

## What 2.0 does worse than 1.0 (evidence)
- It adds the `.token` file, a new fragile input (BOM/UTF-16 crash, silent stale precedence) that 1.0 did not have.
- The ps1 "no token" guard is effectively dead. `Test-Path vajra/credentials.py` is always true, so `-Token` omitted is never refused, and a stale credentials.py token is tried first.
- The restart weakness is unchanged; the "restarts are safe" wording stays misleading.

## Tests / checks you ran
- `python3 -B verify_vajra.py` gave ALL GATES PASS in 6.7 s.
- `--mode synthetic --no-dashboard` in a fresh copy with no tapes dir passed.
- Live-mode guards (weekend, `--date`) and the bad `--date` / missing-tape replay cases are described above.
- Fake `breeze_connect` live runs covered the wrong token, a credentials.py SyntaxError (with and without `--token`), `.token` as UTF-16 / UTF-8 BOM / blank first line, a stale `.token`, and a pasted `apisession=` value.
- Port-in-use test on :8766.
- Replay of the 2026-10-15 fixture, run twice, to check the output dir and the jsonl duplication.
- Seed timing on the real 14–16 Oct tapes and on an empty tapes dir.
- cp1252 strict-encoding banner run.
- ps1 static syntax check, `bash -n orchestrator.sh`, and an import scan against requirements.txt.
- Side effect to disclose: one mistaken run (a backgrounded `cd`) wrote a synthetic tape into `/home/claude/vajra2/tapes/2026-06-02` through the symlink. I deleted that directory and the three real days are intact.
- Scratch is under `/home/claude/teardown/scratch/09/`.
