# 25 — Windows compatibility: teardown
Verdict: Ready for Monday on Windows **if** the operator starts it with `.\run_vajra.ps1 -Token <key>` from Windows Terminal and stops it only with a single Ctrl+C (or lets it stop itself at 15:30). Better than 1.0 overall: the 1.0 launcher does not even parse under PowerShell 5.1, and 1.0's banner crashes on redirected output. Neither version survives a closed console window, a double Ctrl+C or a QuickEdit freeze.
Score (your area): 1.0 = 4.5/10, 2.0 = 6.5/10

Environment used: Python 3.13.16 on Linux; PowerShell 7.4.6 (portable tarball in `scratch/25/pwsh`). I tested PowerShell 5.1 behaviour by decoding the file as cp1252, which is how 5.1 reads a script that has no BOM, and parsing the result with the 7.4 parser. I checked wheel availability with `pip download --platform win_amd64 --python-version 3.13`.

## Findings (most severe first)

### [HIGH] Closing the console window, End Task or a crash leaves every parquet tape unreadable, and 2.0's ReplaySource no longer falls back to jsonl (vajra/tape.py:103, vajra/feed.py:85-90) — reproduced
- Background: on Windows, clicking the console's X, logging off or shutting down sends CTRL_CLOSE/LOGOFF/SHUTDOWN_EVENT. The CPython runtime only maps Ctrl+C to SIGINT and Ctrl+Break to SIGBREAK; every other event goes to the default handler, which calls `ExitProcess`. So no `finally` block runs and no ParquetWriter footer gets written. Task Manager "End task" and `Stop-Process` behave the same.
- Evidence: I started a replay and killed it with SIGKILL, which is equivalent to ExitProcess. Scratch commands are in `scratch/25`.
  ```
  tapes_replay/2026-10-15/basis_tape.parquet UNREADABLE: Parquet magic bytes not found in footer...
  tapes_replay/2026-10-15/level_tape.parquet UNREADABLE: ...
  tapes_replay/2026-10-15/tick_tape.parquet  UNREADABLE: ...
  ReplaySource FAILS: ArrowInvalid Parquet magic bytes not found in footer.
  ```
- This is a 2.0 regression for crashed days. 1.0's `_read_rows` preferred `tick_tape.jsonl` (1.0 feed.py:80-93). 2.0's `_iter_rows` prefers the parquet whenever the file exists, does not catch errors, and never tries the jsonl, even though the jsonl is intact. Separately, the operator manual tells the operator to delete old `tick_tape.jsonl` files. Combine that with a footerless parquet and the day is gone.
- No force-flatten/SHUTDOWN row gets written either. Monday's prev-close seed (feed.py:191-196) silently falls through to the jsonl tail, if that file still exists.
- Fix:
  1. In `app.run`, add a Windows console control handler:
  ```python
  if sys.platform == "win32":
      import ctypes
      from ctypes import wintypes
      _done = threading.Event()
      @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
      def _ctrl(ev):                      # 2=CLOSE 5=LOGOFF 6=SHUTDOWN (Windows allows ~5 s)
          if ev in (2, 5, 6):
              orch.stop(); _done.wait(4.5); return True
          return False                    # let Ctrl+C / Ctrl+Break use the SIGINT path below
      ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl, True)
      app_ctrl_ref = _ctrl                # keep a reference or it gets garbage-collected
  ```
  Then `_done.set()` after `tape.close()` in the `finally`.
  2. In `ReplaySource._iter_rows`, wrap `pq.ParquetFile(pqf)` in try/except `pa.ArrowInvalid` and fall back to the jsonl.
  3. Add a 20-line `tools/rebuild_parquet_from_jsonl.py`, and tell the operator not to delete a day's jsonl until that day's parquet opens.

### [HIGH] Windows console QuickEdit freezes the whole engine, including stops and the trail, while text is selected (orchestrator.py:943-950, breeze.py:288-292) — plausible (well-known conhost behaviour; same in 1.0)
- Scenario: the operator runs PowerShell 5.1 in the classic conhost window, which is the default on Windows 10. He clicks in the console to copy a strike number. conhost enters selection mode, and any `WriteFile` to the console blocks until he presses Esc or Enter.
- Two places write to the console:
  - The engine thread prints a heartbeat to stderr every 30 rounds, about every 7.5 s at 0.25 s cadence. That print blocks the engine thread, so no rounds run, no exits fire and the 15:25 flatten waits.
  - `_on_ticks` writes an UNMAPPED_WS_NAME warning to stderr **while holding `self._lock`** (breeze.py:249 → 288). That blocks the websocket thread and also blocks `snapshot_raw()` on the engine thread.
- Windows Terminal (the default on Windows 11 22H2+) does not block on selection.
- Fix:
  1. At startup on win32, turn QuickEdit off:
  ```python
  k=ctypes.windll.kernel32; h=k.GetStdHandle(-10); m=wintypes.DWORD(); k.GetConsoleMode(h,ctypes.byref(m))
  k.SetConsoleMode(h,(m.value & ~0x40) | 0x80)    # clear ENABLE_QUICK_EDIT_MODE, set EXTENDED_FLAGS
  ```
  2. Move the stderr write in `_normalize_tick` out of the lock: collect the name, and let the round thread print it, as it already does for the tape event.
  3. Ideally, send all console output through a `queue` + printer thread so the engine thread never touches the console.
  4. Have RUN_LIVE.md say: run it in Windows Terminal.

### [MEDIUM] Double Ctrl+C skips force-flatten and SHUTDOWN; a single Ctrl+C interrupts a round mid-way (app.py:163-170, orchestrator.py:953-966) — reproduced
- Single Ctrl+C: the KeyboardInterrupt landed inside `json.dumps` in `process_round` (traceback in int1.out). `finalize` then ran and the parquet was readable. Recovery works, but the interrupt can land between a fill and its order_tape row.
- Two Ctrl+C 50 ms apart: the second one landed in `finalize` (traceback orchestrator.py:961). The tail of event_tape was `['THRESHOLDS_PROVISIONAL','REGIME','FEED_GAP']`: **no SHUTDOWN, and no force-flatten EXIT for an open position.**
- Pressing Ctrl+C twice is reflexive on Windows.
- Fix: install a handler in `app.run` before `orch.run`. The loop already checks `_stop` every round, at most 0.25 s apart:
  ```python
  import signal
  def _graceful(sig, frm):
      orch.stop(); signal.signal(signal.SIGINT, signal.default_int_handler)  # 2nd press = hard
  signal.signal(signal.SIGINT, _graceful)
  if hasattr(signal, "SIGBREAK"): signal.signal(signal.SIGBREAK, _graceful)  # Ctrl+Break on Windows
  ```
  Make sure `BreezeConnect` is created before this, because python-socketio chains SIGINT handlers. Alternatively, have the handler ignore further presses until `finalize` is done.

### [MEDIUM] `vajra\.token` written by PowerShell 5.1 crashes startup or produces a wrong token (config.py:147-151) — reproduced
- RUN_LIVE.md §1 offers "a one-line `vajra\.token`" as a way to pass the token. In PS 5.1:
  - `"12345678" > vajra\.token` writes UTF-16LE with a BOM.
  - `Out-File -Encoding utf8` writes UTF-8 with a BOM.
- Evidence (`scratch/25/tok_test.py`):
  ```
  ps51_redirect_utf16le -> EXC UnicodeDecodeError 'utf-8' codec can't decode byte 0xff in position 0
  ps51_outfile_utf8bom  -> '\ufeff12345678'      (Breeze rejects the session)
  plain                 -> '12345678'
  ```
  The UnicodeDecodeError is not caught. It escapes `_connect_breeze` after the tape has been opened, and the credentials.py fallback is never tried.
- Fix:
  ```python
  b = tok.read_bytes()
  txt = b.decode("utf-16") if b[:2] in (b"\xff\xfe", b"\xfe\xff") else b.decode("utf-8-sig", "replace")
  txt = txt.strip().lstrip("\ufeff")
  ```
  Then wrap it in try/except. Using `-Token` (the documented primary path) avoids this problem.

### [MEDIUM] `run_vajra.ps1 -Mode Live` passes the PowerShell check and then fails in argparse after the ~20 s gate, and the launcher exits 0 when Python fails (run_vajra.ps1:13,49-53) — reproduced
- PowerShell's ValidateSet and `-eq` are case-insensitive. argparse `choices` are not.
  ```
  pwsh -File run_vajra.ps1 -token 1 -skipverify -mode LIVE  ->  PYARGS: -m vajra.app --mode LIVE --token 1   rc=0
  APP_RC=7 (python exit 7)                                    ->  rc=0
  ```
  With real Python, `--mode LIVE` fails with "invalid choice". The exit code is swallowed in both 1.0 and 2.0.
- Fix:
  - Add `$Mode = $Mode.ToLowerInvariant()` after `param()`.
  - End the script with `python @pyArgs; exit $LASTEXITCODE`.
  - Do the token check **before** `verify_vajra.py`, so a missing token doesn't cost 20 s on a restart.

### [MEDIUM] No single-instance guard: a second launch silently runs without a dashboard, and both instances write the same tapes (app.py:218-227, tape.py:93-103) — reproduced (port part, on Linux)
- Scenario: the old window is hung or minimised and the operator relaunches. uvicorn's bind fails (WinError 10048) inside the daemon thread, which exits. Evidence: `first alive True second alive False -> main thread continues headless`.
- The new engine trades with no dashboard. The browser keeps showing the **old** instance's cockpit, so the operator mirrors the wrong engine.
- In `tapes/<today>`, `path.rename(bak)` fails with PermissionError [WinError 32] on Windows while the first process holds the parquet open. The worker catches it, logs it on every flush, and the second session's parquet is never written. Both processes interleave into the same jsonl files.
- Fix:
  - Take an exclusive lock at startup: `fd = os.open(partition/".vajra.lock", os.O_CREAT|os.O_RDWR)` plus `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`. On Windows the lock is released automatically when the process dies. If it fails, abort with "another VAJRA is running".
  - Also probe-bind the dashboard port in the main thread before starting the engine, and abort on failure.

### [LOW] Launcher depends on a bare `python` and on execution policy (run_vajra.ps1:25,34,53) — plausible
- If Python 3.13 was installed without "Add to PATH", `python` resolves to the Microsoft Store stub (exit 9009). The script then prints "Completion gate FAILED", which is misleading.
- Files copied or downloaded fresh carry Mark-of-the-Web. Under RemoteSigned, `.\run_vajra.ps1` is then refused as "not digitally signed".
- Fix:
  - Run `Get-ChildItem -Recurse D:\vajra2 | Unblock-File` once, or launch with `powershell -ExecutionPolicy Bypass -File .\run_vajra.ps1`.
  - In the script, pick `.venv\Scripts\python.exe`, then `py -3.13`, then `python`, and print `python --version` in the banner.

### [LOW] Token typed on the command line is saved in PSReadLine history (run_vajra.ps1:15) — read in code
- `-Token 12345678` ends up in `%APPDATA%\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt`. The token only lasts a day, so this is low risk. You can offer `Read-Host -AsSecureString` when `-Token` is empty.

### [LOW] Logging and console text encoding — read in code / reproduced OK
- `verify_vajra.py` with `PYTHONIOENCODING=cp1252` and redirected output: rc=0, all 7 gates PASS, no UnicodeEncodeError.
- An AST/regex scan found exactly one character outside cp1252 in a print, the `⚡` at app.py:148. It goes through `_say()`, which has an ASCII fallback.
- The ps1 sets `PYTHONIOENCODING=utf-8`, and a real console uses WindowsConsoleIO anyway. Every `open()` in the runtime passes `encoding="utf-8"`. `json.dumps` uses ensure_ascii, so the tapes are pure ASCII. Fine.
- Remaining nit: breeze_connect opens `logs\apiLogs.log` / `websocketLogs.log` with a `FileHandler` using the locale encoding and no rotation, held open all day (breeze_connect.py:46,54). That's harmless.

### [LOW] Clock source is the Windows wall clock (feed.py:424-426) — plausible, same in 1.0
- Round `ts`, the 09:21 entry gate, the 15:25 flatten and the 15:30 stop all come from `time.time_ns()`. A standalone Windows PC re-syncs w32time only about weekly and can drift by seconds.
- Fix: add `w32tm /resync` to the checklist (needs admin), or log the offset between the local clock and Breeze tick timestamps at startup.

### [LOW] Dev tools are Linux-only or broken — read in code
- `tools/latency_bench.py:27` is a **SyntaxError** (nested quotes left over from a path rewrite): `pq.ParquetFile("str(Path(__file__)... / "tapes" / ...")")`.
- `tools/make_real_fixture.py:10-11` hard-codes `/home/claude/...`.
- `tools/make_sounds.py` needs ffmpeg and scipy.
- None of these are on the live path.

### [LOW] Mixed line endings — read in code
- 1.0 sources are CRLF, which shows the operator edits on Windows. 2.0 sources are LF, except `tests/test_execution_risk.py` and `vajra/data/real_nifty_day.csv`, which are CRLF.
- Python, PowerShell and the `newline=""` csv reader all accept either, so this is functionally fine. Expect full-file diffs in any git or diff tool. Normalise with `.gitattributes` (`* text=auto eol=crlf`, `*.parquet binary`).
- On Windows the jsonl tapes come out CRLF (text mode), as the recorded 10-16 event_tape shows. All readers use `strip()`/`splitlines()`, and the determinism gate only compares Windows output with Windows output. OK.

## Checked and fine (brief)
- **run_vajra.ps1 syntax:** 0 parse errors under PowerShell 7.4.6. The file is pure ASCII (`file`: "ASCII text"), so PS 5.1 parses it identically.
  - Functional run with a stub `python`:
    - no token → refuses (rc 1)
    - `-Token` → `--token` is passed
    - `-Mode live -Date` → refused
    - gate failure → refuses (rc 1)
    - `-Verify` and `-SkipVerify` behave as documented
- **1.0's launcher is broken on PS 5.1.** It is UTF-8 with no BOM. Its em-dash bytes `E2 80 94` decode in cp1252 as `â€”`, and 0x94 is `”`, which PowerShell treats as a closing quote. Result: `v1-as-cp1252 errors: 2 — 32: Unexpected token '}'`. 1.0 also assigned to the automatic variable `$args`. 2.0 fixes both.
- **Timezones:** IST is a fixed `timezone(timedelta(hours=5, minutes=30))` (timeutil.py:11). There is no zoneinfo or pytz anywhere, so tzdata is not needed. Every `fromtimestamp`/`now` is tz-aware.
- **Expiry dates:** expiry strings use an explicit English month table (breeze.py:84-89), not locale-dependent `%b`.
- **Paths:** pathlib is used throughout the runtime. The only `os.path.join` is the sounds directory. `tape_root` is resolved against REPO_ROOT. In replay.py:77, the guard against writing into the recorded day uses `resolve()` and WindowsPath's case-insensitive equality.
- **asyncio / threads:**
  - No event-loop policy is set. uvicorn runs on Windows' default Proactor loop in a daemon thread and only installs signal handlers on the main thread (`capture_signals` checked).
  - breeze_connect uses the synchronous `socketio.Client`, whose engineio background threads are `daemon=True`.
- **Wheels:** all binary dependencies have cp313 win_amd64 wheels: pyarrow 26.0.0, numpy 2.5.4, pandas 3.0.6, pyyaml 6.0.3, pydantic_core 2.50.1, charset_normalizer, httptools 0.9.0, watchfiles 1.3.0 (abi3), websockets 17.2.
  - `breeze-connect` 1.0.69 is **sdist only**. It is pure Python and builds with setuptools, but `pip download --only-binary` reports "no matching distribution" for it.
  - The `uvicorn[standard]` "ResolutionImpossible" I hit is a pip `--platform` artifact: it evaluated the `uvloop; sys_platform != 'win32'` marker against the Linux host. It is not a Windows problem.
  - Install risk worth flagging: requirements.txt is entirely `>=`. A Sunday `pip install -U` would pull pandas 3.x, starlette 1.x and fastapi 0.143 under breeze_connect. Freeze the operator's working environment (`pip freeze > requirements.lock`) and install from that.
- **Timers:** Python 3.13 on Windows has high-resolution `time.sleep` (waitable timer), and `time()`/`monotonic()` use the precise clock APIs. The 0.25 s cadence and the 20 ms SSE loop are not quantised to 15.6 ms.

## What 2.0 does better than 1.0 (evidence)
- The launcher is ASCII-only, so it parses under PS 5.1; 1.0 fails to parse there (reproduced above). It also stops using `$args`, and adds `PYTHONIOENCODING=utf-8`, `-Token` and `-SkipVerify`.
- The banner goes through `_say()`, so a redirected or cp1252 stdout no longer crashes after the tape and broker socket are open (app.py:84-90).
- The tape writes the `.bak` with a collision-safe name, so a Windows `rename` onto an existing file can't drop a session (tape.py:93-102).
- Replays write to `tapes_replay/`, never into `tapes/<date>`, so there are no Windows rename or lock fights with recorded data.
- `verify_vajra.py` calls `os.system("")` on win32 to enable ANSI colours.

## What 2.0 does worse than 1.0 (evidence)
- `ReplaySource` prefers the parquet and has no fallback (feed.py:85-90). A day that ended with a console close or crash is unreplayable, even with an intact jsonl; 1.0 read the jsonl first. The parquet-first choice fixes 1.0's out-of-memory problem, but it needs a try/except fallback.
- `run_vajra.ps1` dropped `$ErrorActionPreference = "Stop"`. That's neutral in practice, because it never affected native exit codes, and the exit code is still swallowed.

## Tests / checks you ran
- `pwsh 7.4.6 Parser::ParseFile` on 2.0's ps1 and 1.0's ps1, plus on 1.0 decoded as cp1252 (the PS 5.1 simulation). Functional runs of the ps1 with a stub `python` covering 9 argument combinations, including case and exit-code checks.
- `PYTHONIOENCODING=cp1252 python3 -B verify_vajra.py > file` on a scratch copy: ALL GATES PASS, rc 0.
- Replay of 2026-10-15 (partial runs, about 30 s CPU each) stopped three ways:
  - single SIGINT: SHUTDOWN written, parquet OK
  - double SIGINT: no SHUTDOWN or flatten
  - SIGKILL: all parquets footerless, ReplaySource fails
- `.token` decoding for UTF-16LE, UTF-8-BOM and plain files.
- Port conflict with two uvicorn servers in threads.
- `pip download --platform win_amd64 --python-version 3.13 --only-binary=:all:` for the full dependency tree, plus a breeze-connect sdist inspection.
- Greps for `open(` without `encoding=`, strings outside cp1252, naive datetimes, zoneinfo, signal/asyncio usage and hard-coded paths; CRLF census for 1.0 and 2.0.
