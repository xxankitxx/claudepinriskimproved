"""VAJRA application bootstrap.

Resolves the session expiry deterministically, opens the tape, builds the feed
source (live / replay / synthetic), wires the orchestrator, optionally starts
the dashboard, and runs.  Mirrors the spec's operator-run model; the shell
orchestrator (run_vajra.ps1 / orchestrator.sh) calls in here.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import date, datetime

from .config import load_config
from .expiry import ExpiryCalendar, parse_holidays, ExpiryResolution
from .tape import TapeWriter
from .orchestrator import Orchestrator
from .timeutil import IST


def build_calendar(cfg) -> ExpiryCalendar:
    holidays = set()
    for key, val in (cfg.get("expiry", default={}) or {}).items():
        # only DATE LISTS (holidays_2026: [...]) — never scalar flags like holidays_verified,
        # which also match the prefix (a latent crash while the flag was false: `False or []`
        # iterated cleanly; `True or []` blew up in parse_holidays).
        if key.startswith("holidays_") and isinstance(val, (list, tuple)):
            holidays |= parse_holidays(val)
    return ExpiryCalendar(
        holidays=holidays,
        weekly_weekday=cfg.get("expiry", "weekly_weekday", default="TUE"),
    )


def choose_synthetic_session(cal: ExpiryCalendar, base: date) -> date:
    """Pick a non-monthly weekly-expiry trading day (so is_expiry_day + IMPLIED)."""
    expiry, _ = cal.active_weekly_expiry(base)
    for _ in range(8):
        res = cal.resolve(expiry)
        if not res.is_monthly:
            return expiry
        expiry, _ = cal.active_weekly_expiry(expiry.fromordinal(expiry.toordinal() + 1))
    return expiry


def write_startup_events(tape: TapeWriter, cfg, res: ExpiryResolution, ts_ms: int):
    # Config snapshot WITHOUT secrets (§9.7).
    snap = dict(cfg.raw)
    snap.pop("breeze", None)
    tape.event("STARTUP_CONFIG", {"config": snap, "schema_version": cfg.schema_version}, ts_ms=ts_ms)
    tape.event("EXPIRY_RESOLVED", {
        "expiry_date": res.expiry_date.isoformat(), "basis_mode": res.basis_mode,
        "is_expiry_day": res.is_expiry_day, "is_monthly": res.is_monthly,
        "session_date": res.session_date.isoformat(),
        "near_month_expiry": res.near_month_expiry.isoformat() if res.near_month_expiry else None,
    }, ts_ms=ts_ms)
    if res.shifted_from_holiday:
        tape.event("HOLIDAY_DECISION", {
            "nominal_weekday": res.nominal_weekday,
            "resolved_expiry": res.expiry_date.isoformat(),
            "reason": "nominal expiry weekday was a holiday/weekend; shifted to prev trading day",
        }, ts_ms=ts_ms)
    if not cfg.thresholds_calibrated:
        tape.event("THRESHOLDS_PROVISIONAL", {
            "bias_threshold": cfg.get("decision", "bias_threshold"),
            "bias_threshold_lead": cfg.get("decision", "bias_threshold_lead"),
            "thresholds_calibrated": False,
            "note": "PROVISIONAL placeholders — calibrate on recorded tape via replay.py (§12)",
        }, ts_ms=ts_ms)
    if not bool(cfg.get("expiry", "holidays_verified", default=False)):
        tape.event("HOLIDAYS_UNVERIFIED", {
            "holidays_verified": False,
            "note": "NSE 2026 holiday calendar contains UNVERIFIED placeholders — a wrong date "
                    "silently mis-resolves expiry/basis-mode/chain. Confirm against the official NSE "
                    "circular and set expiry.holidays_verified: true.",
        }, ts_ms=ts_ms)
        print("[VAJRA WARNING] NSE 2026 holidays are UNVERIFIED — confirm against the official NSE "
              "circular and set expiry.holidays_verified: true (wrong holiday -> wrong expiry).",
              file=sys.stderr, flush=True)


def _say(msg: str) -> None:
    """2.0: print that never crashes on a non-UTF-8 Windows console / redirected stdout (1.x died on
    the ⚡ banner with UnicodeEncodeError AFTER opening the tape and the broker socket)."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def _now_ms_ist() -> int:
    return int(datetime.now(tz=IST).timestamp() * 1000)


def run(config_path: str | None = None, mode: str | None = None,
        session_date: date | None = None, with_dashboard: bool | None = None,
        pace: float = 0.0, port: int | None = None) -> str:
    cfg = load_config(config_path)
    mode = mode or cfg.get("feed", "mode", default="synthetic")
    if port:
        cfg.raw.setdefault("server", {})["port"] = int(port)
    cal = build_calendar(cfg)

    # --- resolve session date + expiry ---
    real_src = None
    if mode == "real":
        from .realfeed import RealSessionSource
        real_src = RealSessionSource(cfg, session_date=session_date)   # recorded day (--date)
        session_date = real_src.session_date
    elif mode == "synthetic":
        session_date = session_date or choose_synthetic_session(cal, date(2026, 6, 1))
    elif mode == "live":
        if session_date is not None and session_date != datetime.now(tz=IST).date():
            raise SystemExit("VAJRA: --date is not allowed in live mode (it would subscribe the wrong "
                             "expiry and write into an old tape folder).")
        session_date = session_date or datetime.now(tz=IST).date()
        if not cal.is_trading_day(session_date):     # don't run a live session on a weekend/holiday
            raise SystemExit(f"VAJRA: {session_date} is NOT a trading day (weekend/holiday) — aborting live run.")
        _now = datetime.now(tz=IST)
        from .closing import resolve_closing
        _close = resolve_closing(cfg, session_date, cal.resolve(session_date).is_expiry_day).session_close
        if _now.strftime("%H:%M") >= _close:       # 1.x/2.0 exited silently when started after the close (T19)
            raise SystemExit(f"VAJRA: market is closed (now {_now:%H:%M} IST, session close {_close}) — "
                             f"nothing to run live. Use --mode replay for a recorded day.")
        _next = f"holidays_{session_date.year + 1}"
        if session_date.month == 12 and session_date.day >= 1 and not cfg.get("expiry", _next, default=None):
            print(f"  !! WARNING: expiry.{_next} is missing — the near-month (Jan) expiry may be "
                  f"resolved onto a holiday. Add next year's NSE holidays to vajra_config.yaml.", flush=True)
    else:  # replay
        session_date = session_date or datetime.now(tz=IST).date()
    res = cal.resolve(session_date)

    # 2.0: a replay NEVER writes into the recorded day (1.x renamed the recorded parquet to .bak and
    # appended to its jsonl/event tapes). Replays go to tapes_replay/<date>/.
    tape_root = cfg.tape_root if mode != "replay" else cfg.tape_root.parent / "tapes_replay"
    tape = TapeWriter(tape_root, session_date, schema_version=cfg.schema_version,
                      rotate_secs=float(cfg.get("tape", "rotate_secs", default=60)),
                      sidecar_jsonl=str(cfg.get("tape", "sidecar_jsonl", default="small")),
                      compression=str(cfg.get("tape", "compression", default="zstd")))
    tape.drop_verified_jsonl = str(cfg.get("tapes", "drop_verified_jsonl", default="on")).lower() not in ("off", "0", "no", "false")
    if getattr(tape, "merge_in_progress", False):
        _say("  vajra · the earlier session's tapes are still being merged by the background process -- "
             "left to it (logs/tape_merge.log)")
    if getattr(tape, "recovering", None):
        _say(f"  vajra · an earlier (killed) session today left tape parts: {', '.join(tape.recovering)} -- "
             f"merging in the background (the session starts now, not after the merge)")
    elif tape.recovered:
        _say(f"  vajra · recovered the tapes of an earlier (killed) session today: {', '.join(tape.recovered)}")
    if mode == "live":
        _repair_prior_days(cfg.tape_root, session_date)
        for line in abnormal_end_report(cfg.tape_root, session_date):
            _say(line)
    start_ms = _now_ms_ist()
    write_startup_events(tape, cfg, res, start_ms)

    breeze = None
    if mode == "live":
        breeze = _connect_breeze(cfg, tape, start_ms)

    orch = Orchestrator(cfg, tape, res, breeze=breeze)
    raw_rec = None
    if mode == "live" and breeze is not None:        # 2.0.1: record every broker tick (EOD forensics)
        try:
            from .rawtape import RawTickRecorder
            raw_rec = RawTickRecorder(tape.partition,
                                      enabled=str(cfg.get("tape", "raw_ticks", default="on")).lower()
                                      not in ("off", "false", "0", "no"))
            breeze.raw_recorder = raw_rec
        except Exception as _e:
            _say(f"  !! raw tick recorder not started ({_e!r}) — engine runs without it")
    if mode == "live":                           # same-day restart: keep compounding capital (T09/T27)
        try:
            info = orch.restore_from_order_tape(tape.partition)
            if info:
                _say(f"  vajra · RESTART: capital Rs {info['capital']:,.0f} (realized {info['realized_pnl']:+,.0f}), "
                     f"entries so far {info['entries_today']}, open position: {info['open_position'] or 'none'}")
        except Exception as _e:
            _say(f"  !! RESTART restore FAILED ({_e!r}) — capital starts from the configured value")
        try:
            n = orch.restore_basis_history(tape.partition)
            if n:
                _say(f"  vajra · RESTART: {n} pre-15:15 NIFTY basis rows restored (closing-session spot)")
        except Exception as _e:
            _say(f"  !! basis history restore failed ({_e!r}) — closing-session entries need a clean pre-freeze basis")

    # --- dashboard ---
    server = None
    want_dash = cfg.get("server", "enable", default=True) if with_dashboard is None else with_dashboard
    if want_dash:
        server = _start_dashboard(cfg, orch)

    # --- source ---
    source = real_src if mode == "real" else _build_source(
        cfg, mode, session_date, res, breeze, event_sink=orch._emit_event, pace=pace)

    # Startup banner to the terminal (the operator watches this for an attended live run).
    poll = float(cfg.get("feed", "poll_interval_secs", default=1))
    dash_url = f"http://{cfg.server_host}:{cfg.server_port}" if want_dash else "(dashboard off)"
    _say(f"⚡ VAJRA 2.0 {mode.upper()} | {session_date} | expiry {res.expiry_date} {res.basis_mode}"
         f" | regime {orch.regime.name} (T-{orch.regime.dte_trading}, {orch.regime.band_method} band)"
         f" | last entry {orch.entry_cutoff} | square-off {orch.flatten_at} | runs to {orch.closing.session_close}"
         f" | cadence {poll:g}s | LIVE_ORDERS={cfg.live_orders} ({'!! LIVE ORDERS !!' if cfg.live_orders else 'paper'}) | {dash_url}")
    if mode == "live":
        try:                                 # a full day writes ~8-9 GB of tape (teardown T17)
            import shutil
            free_gb = shutil.disk_usage(str(tape.partition)).free / 1e9
            if free_gb < 20:
                print(f"  !! WARNING: only {free_gb:.1f} GB free on the tape drive — a full day writes ~9 GB. "
                      f"Free space (old tick_tape.jsonl files are the big ones) before the session.", flush=True)
        except Exception:
            pass
    if mode == "live" and poll > 0.5:                    # boot-time guard: a stale config would run slow
        print(f"  !! WARNING: live round cadence is {poll:g}s — expected 0.25s. Check vajra_config.yaml"
              f" feed.poll_interval_secs and RESTART.", flush=True)

    if mode == "live":
        # 2.0: Python's cyclic garbage collector paused the engine thread 167-650 ms at a time on
        # live-sized buffers (team finding 26). Freeze the long-lived startup objects and make
        # collections rarer -> max pause measured ~8 ms.
        import gc
        gc.collect()
        gc.freeze()
        gc.set_threshold(50000, 50, 100)
        # 2.1.1: measure every collection (generation, ms) -> round.gc in the state / round tape, so a
        # 400-1500 ms round can be tied to the collector instead of guessed at. The orchestrator
        # re-freezes every ~10 min so the standing tape buffers leave the scanned set.
        _gc_t = {"t0": 0.0}
        orch.gc_info = {"gen": None, "ms": None, "count": 0, "max_ms": 0.0}

        def _gc_cb(phase, info):
            if phase == "start":
                _gc_t["t0"] = time.perf_counter()
            else:
                ms = (time.perf_counter() - _gc_t["t0"]) * 1000.0
                gi = orch.gc_info
                gi.update(gen=info.get("generation"), ms=round(ms, 1), count=gi["count"] + 1,
                          max_ms=round(max(gi["max_ms"], ms), 1))
        gc.callbacks.append(_gc_cb)
        _disable_quickedit()
        # 2.1 (2 Nov: the laptop switched off mid-trade): no idle sleep while live, loud on battery.
        from . import power as _power
        _say("  vajra · system sleep blocked for the session" if _power.keep_awake()
             else "  !! could not block system sleep -- set the Windows power plan to never sleep while plugged in")
        _prio = str(cfg.get("runtime", "process_priority", default="above_normal"))
        if _power.raise_priority(_prio):
            _say(f"  vajra · process priority {_prio} (batch jobs on this laptop now wait for the engine)")
        _ps = _power.status()
        if _ps.get("on_battery"):
            _say(f"  !! RUNNING ON BATTERY ({_ps.get('battery_pct')}%) -- plug the laptop in. A power-off "
                 f"mid-trade cannot be flattened by VAJRA.")
        orch.power_status_fn = _power.status
    _done = threading.Event()
    if mode == "live":
        _install_clean_shutdown(orch, _done)
    try:
        orch.run(source, heartbeat=(mode == "live"))
    finally:
        # 2.1: the close is sequenced so that NOTHING can skip a step: a second Ctrl+C is ignored
        # while the tapes close (it used to leave every parquet without a footer), each step is
        # isolated, and the broker socket is always torn down.
        # 2.1.1: the position is flattened and its EXIT row flushed by the time orch.run returns, so a
        # Windows console-close handler (which gives us ~4.5 s) may let the process die NOW without
        # losing anything that matters; the tape merge below is recoverable at the next start.
        _done.set()
        try:
            import signal as _sig
            _sig.signal(_sig.SIGINT, _sig.SIG_IGN)
            if hasattr(_sig, "SIGBREAK"):
                _sig.signal(_sig.SIGBREAK, _sig.SIG_IGN)
        except Exception:
            pass
        _t0 = time.perf_counter()
        # 2.2: the live engine only CLOSES its part files here; the minutes-long merge runs in a
        # detached process, so this window can be closed straight away (9 Nov: it was, mid-merge).
        _detach = (mode == "live" and str(cfg.get("tape", "merge_at_close", default="detached")).lower()
                   == "detached")
        try:                                       # the binding tapes first (a console close gives ~4.5 s)
            tape.close(merge=not _detach)
        except Exception as _e:
            _say(f"  !! tape close failed ({_e!r}) — parts are kept on disk and merge at the next start")
        finally:
            _done.set()
        if raw_rec is not None:
            try:
                raw_rec.close(merge=not _detach)
                _say(f"  vajra · raw ticks recorded: {raw_rec.rows:,} rows"
                     + (f" ({raw_rec.dropped:,} dropped — disk too slow)" if raw_rec.dropped else ""))
            except Exception:
                pass
        try:                                       # 2.2: only now is every part in the folder closed
            tape.release_live()
        except Exception:
            pass
        if breeze is not None:
            try:
                breeze.disconnect()
            except Exception:
                pass
        if server is not None:
            server.should_exit = True
        if mode == "live":
            try:
                from . import power as _power
                _power.release()
            except Exception:
                pass
        _say(f"  vajra · tapes closed in {time.perf_counter() - _t0:.1f} s")
        if _detach:
            from . import tape_merge
            if tape_merge.spawn([tape.partition]):
                _say("  vajra · the day's tapes are being merged by a background process "
                     "(logs/tape_merge.log) -- this window can be closed now")
            elif tape_merge.has_parts(tape.partition):
                _say("  !! could not start the tape merge -- the parts are intact and merge at the next start")

    return str(tape.partition)


def abnormal_end_report(root, today, max_days: int = 3) -> list[str]:
    """2.1: did the last session(s) end cleanly? A partition whose event tape has no SHUTDOWN after its
    last round was killed (power-off, task manager, crash). Report the last round time and whether a
    paper position was still open at that moment, so the operator reconciles ICICI before trading on."""
    import json
    lines: list[str] = []
    try:
        from .timeutil import ist_datetime
        days = sorted(p for p in root.glob("*") if p.is_dir() and len(p.name) == 10 and p.name[:4].isdigit()
                      and p.name <= today.isoformat())
        for p in days[-max_days:]:
            ev = p / "event_tape.jsonl"
            if not ev.exists():
                continue
            last_ev, started, shutdown = None, False, False
            with open(ev, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    last_ev = r
                    if r.get("event_type") == "STARTUP_CONFIG":
                        started, shutdown = True, False
                    elif r.get("event_type") == "SHUTDOWN":
                        shutdown = True
            if not started or shutdown:
                continue
            last_round = None
            rt = p / "round_tape.jsonl"
            if rt.exists():
                with open(rt, "rb") as fh:
                    fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 4096))
                    tail = fh.read().decode("utf-8", errors="replace").strip().splitlines()
                for line in reversed(tail):
                    try:
                        last_round = json.loads(line)
                        break
                    except ValueError:
                        continue
            open_pos = None
            ot = p / "order_tape.jsonl"
            if ot.exists():
                with open(ot, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("action") == "ENTRY":
                            open_pos = f"{r.get('right')} {r.get('strike')} x{r.get('qty')} @ {r.get('fill_price')}"
                        elif r.get("action") == "EXIT":
                            open_pos = None
            when = (ist_datetime(int(last_round["ts"])).strftime("%H:%M:%S") if last_round and last_round.get("ts")
                    else (ist_datetime(int(last_ev["ts"])).strftime("%H:%M:%S") if last_ev else "?"))
            lines.append(f"  !! {p.name}: the session ENDED ABNORMALLY (no SHUTDOWN; last round {when}) -- "
                         f"power-off / kill. " + (f"A paper position was OPEN: {open_pos}. Reconcile ICICI by hand."
                                                   if open_pos else "No position was open."))
    except Exception as e:
        lines.append(f"  (abnormal-end check skipped: {e!r})")
    return lines


def _repair_prior_days(root, today, days: int = 8) -> None:
    """Merge leftover parquet parts of recent sessions whose merge was interrupted, so EOD tooling and
    the prev-close seed find readable files. 2.2: done by a detached low-priority process
    (vajra.tape_merge) -- the in-process thread it replaces competed with the live feed for the
    interpreter for minutes at the open."""
    try:
        from . import tape_merge
        stale = tape_merge.stale_partitions(root, exclude=root / today.isoformat(), look_back=days)
        if stale and tape_merge.spawn(stale):
            _say(f"  vajra · unmerged tapes from {', '.join(d.name for d in stale)} -- merging in a "
                 f"background process (logs/tape_merge.log)")
    except Exception:
        pass


def _build_source(cfg, mode, session_date, res, breeze, event_sink=None, pace: float = 0.0):
    if mode == "replay":
        from .feed import ReplaySource
        src = ReplaySource(cfg.tape_root / session_date.isoformat())
        if pace and pace > 0:
            return _Paced(src, pace)
        return src
    if mode == "live":
        from .feed import LiveBreezeSource
        nm = (res.near_month_expiry or res.expiry_date).isoformat()
        return LiveBreezeSource(breeze, cfg, res.expiry_date.isoformat(), res.basis_mode,
                                future_expiry_iso=nm, event_sink=event_sink)
    # synthetic (dev-only)
    from .synthetic import generate_rounds
    rounds = generate_rounds(cfg, session_date)

    class _Static:
        def rounds(self_inner):
            yield from rounds
    return _Static()


class _Paced:
    """2.1: replay at (tape time) / pace -- pace 1.0 = real time, 4.0 = four times faster. Lets the
    dashboard be watched / developed on a recorded day exactly as it would look live."""

    def __init__(self, src, pace: float):
        self.src, self.pace = src, float(pace)

    def prev_close_snapshot(self):
        fn = getattr(self.src, "prev_close_snapshot", None)
        return fn() if callable(fn) else None

    def rounds(self):
        prev_ts = None
        t_wall = time.perf_counter()
        for ts, bucket in self.src.rounds():
            if prev_ts is not None:
                t_wall += max(0.0, (ts - prev_ts) / 1000.0) / self.pace
                delay = t_wall - time.perf_counter()
                if delay > 0:
                    time.sleep(min(delay, 5.0))
                else:
                    t_wall = time.perf_counter()
            prev_ts = ts
            yield ts, bucket


def _connect_breeze(cfg, tape, ts_ms):
    from .breeze import BreezeWrapper, BreezeCredentials, BreezeConnectionError
    api_key = cfg.breeze_secret("api_key")
    api_secret = cfg.breeze_secret("api_secret")
    token = cfg.breeze_secret("session_token")
    if not (api_key and api_secret and token):
        tape.event("SESSION_ERROR", {"reason": "missing Breeze credentials in vajra/credentials.py"}, ts_ms=ts_ms)
        raise RuntimeError(
            "Live mode needs API_KEY / API_SECRET / SESSION_KEY filled in vajra/credentials.py "
            "(refresh SESSION_KEY daily — see the docstring there).")
    _src = getattr(cfg, "session_token_source", "?")
    print(f"  vajra · session token from {_src} (length {len(token)}; value not shown)", flush=True)
    creds = BreezeCredentials(api_key, api_secret, token)
    bw = BreezeWrapper(
        creds, vix_fix=bool(cfg.get("feed", "vix_decimal_shift_fix", default=True)),
        reconnect_max_retries=int(cfg.get("breeze", "reconnect_max_retries", default=5)),
        reconnect_backoff_secs=float(cfg.get("breeze", "reconnect_backoff_secs", default=2)),
        sdk_reconnect_grace_secs=float(cfg.get("feed", "sdk_reconnect_grace_secs", default=10)),
        keepalive_secs=float(cfg.get("feed", "ws_keepalive_secs", default=10)),
        buffered_reader=str(cfg.get("feed", "ws_buffered_reader", default="on")).lower() not in ("off", "false", "0"),
        switch_interval_ms=float(cfg.get("feed", "switch_interval_ms", default=0.5)),
    )
    try:
        bw.connect()
    except BreezeConnectionError as e:                  # auditable connect failure, not a silent crash
        tape.event("SESSION_ERROR", {"reason": "breeze_connect_failed", "detail": str(e)}, ts_ms=ts_ms)
        raise
    return bw


_CTRL_HANDLER = None   # keep a reference: ctypes callbacks must not be garbage-collected


def _install_clean_shutdown(orch, done):
    """2.0.1 (teardown T25/T14): Ctrl+C, Ctrl+Break, or closing the console window used to kill the
    process mid-write -> parquet tapes without a footer (unreadable) and no SHUTDOWN/flatten. Now the
    first Ctrl+C asks the engine to stop at the end of the round (flatten + close tapes); closing the
    window does the same and waits up to ~4.5 s (Windows' limit) for the tapes to close."""
    import signal
    hits = {"n": 0}

    def _on_sigint(signum, frame):
        hits["n"] += 1
        if hits["n"] >= 3:
            raise KeyboardInterrupt
        print("\n  vajra · stopping cleanly (flatten + close tapes) — press Ctrl+C twice more to force",
              flush=True)
        orch.stop()
    try:
        signal.signal(signal.SIGINT, _on_sigint)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _on_sigint)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def _on_console(evt):
                if evt in (2, 5, 6):            # CLOSE / LOGOFF / SHUTDOWN
                    orch.stop()
                    done.wait(4.5)
                    return True
                return False                     # Ctrl+C / Ctrl+Break -> the signal handlers above
            global _CTRL_HANDLER
            _CTRL_HANDLER = HANDLER(_on_console)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True)
        except Exception:
            pass


def _disable_quickedit():
    """Windows console QuickEdit: selecting text in the console window PAUSES every write to it, and
    the engine thread prints (heartbeat) -> rounds freeze, no stops, no 15:25 flatten (teardown T14).
    Turn QuickEdit off for this console. No-op elsewhere / on any failure."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)                       # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            k32.SetConsoleMode(h, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
            print("  vajra · console QuickEdit disabled (selecting text can no longer freeze the engine)",
                  flush=True)
    except Exception:
        pass


def _start_dashboard(cfg, orch):
    import uvicorn
    from .dashboard import build_app
    app = build_app(orch)
    import socket
    _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:                                   # a taken port used to kill the dashboard thread SILENTLY (T09)
        _probe.bind((cfg.server_host, int(cfg.server_port)))
    except OSError as e:
        print(f"  !! DASHBOARD NOT STARTED: port {cfg.server_port} is in use ({e}). Is another VAJRA "
              f"already running? Close it (or change server.port) and restart.", flush=True)
        return None
    finally:
        _probe.close()
    config = uvicorn.Config(app, host=cfg.server_host, port=cfg.server_port,
                            log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server


def smoke_test(config_path: str | None, secs: int) -> int:
    """Live-connectivity preflight (no engine, no tapes): credentials -> session -> websocket ->
    get_names for all 21 stocks -> strike-step resolution -> every subscription the real session
    makes -> listen `secs` seconds -> summary. Exit 0 = ready; 1 = something is wrong."""
    from datetime import timedelta
    cfg = load_config(config_path or "vajra_config.yaml")
    cal = build_calendar(cfg)
    today = datetime.now(tz=IST).date()
    d = today
    for _ in range(10):
        if cal.is_trading_day(d):
            break
        d += timedelta(days=1)
    res = cal.resolve(d)
    from .closing import resolve_closing
    _ct = resolve_closing(cfg, d, res.is_expiry_day)
    closed = (d != today) or datetime.now(tz=IST).strftime("%H:%M") >= _ct.session_close \
        or datetime.now(tz=IST).strftime("%H:%M") < "09:15"
    _say(f"VAJRA 2.0 SMOKE | now {datetime.now(tz=IST):%Y-%m-%d %H:%M} IST | next session {d} | expiry {res.expiry_date} "
         f"{res.basis_mode} | {'EXPIRY DAY' if res.is_expiry_day else 'non-expiry'} | market {'CLOSED' if closed else 'OPEN'} "
         f"| LIVE_ORDERS={cfg.live_orders} ({'!! LIVE !!' if cfg.live_orders else 'paper'})")
    ok = True

    class _T:                       # stand-in tape: events go to the terminal
        def event(self, t, p, ts_ms=None):
            _say(f"  smoke · {t}: {p}")
    t0 = time.perf_counter()
    try:
        bw = _connect_breeze(cfg, _T(), int(time.time() * 1000))
    except Exception as e:
        _say(f"  FAIL · Breeze session/websocket: {e}")
        _say("        -> wrong or expired session token? (it changes daily), wrong api key/secret, or no internet")
        return 1
    _say(f"  OK   · Breeze session generated + websocket connected ({time.perf_counter() - t0:.1f} s)")
    from .feed import LiveBreezeSource
    nm = (res.near_month_expiry or res.expiry_date).isoformat()
    src = LiveBreezeSource(bw, cfg, res.expiry_date.isoformat(), res.basis_mode, future_expiry_iso=nm,
                           event_sink=lambda t, p, ts_ms=None: _say(f"  smoke · {t}: {p}"))
    t1 = time.perf_counter()
    try:
        src._ensure_base_subscriptions()
    except Exception as e:
        _say(f"  FAIL · subscriptions: {e!r}")
        return 1
    _say(f"  OK   · base subscriptions done ({time.perf_counter() - t1:.1f} s): {len(bw._subs)} subscribe calls "
         f"(cash/futures for {len(cfg.universe)} instruments + VIX)")
    codes = {}
    for inst in cfg.stocks:
        try:
            codes[inst] = bw.code_for(inst)
        except Exception:
            codes[inst] = None
    missing = [k for k, v in codes.items() if not v]
    if missing:
        ok = False
        _say(f"  FAIL · broker codes not resolved for: {missing}")
    else:
        _say(f"  OK   · broker codes resolved for all {len(codes)} stocks")
    steps = cfg.raw.get("strike_step", {}) or {}
    unl = [s for s in cfg.stocks if s not in steps]
    _say(f"  OK   · strike steps: {len(cfg.stocks) - len(unl)} listed in the yaml" + (f", runtime-resolved: {unl}" if unl else ""))
    _say(f"  ...  · listening {secs} s for ticks")
    last = 0
    for i in range(secs):
        time.sleep(1)
        n = bw.ticks_received
        if i % 10 == 9 or i == secs - 1:
            _say(f"        {i + 1:3d} s: {n:,} ticks ({n - last:,} in the last {10 if i % 10 == 9 else (i % 10) + 1} s)")
            last = n
    raw = bw.snapshot_raw()
    kinds = {}
    for k in raw:
        kinds[k.split("|")[1]] = kinds.get(k.split("|")[1], 0) + 1
    spot = raw.get(bw._key(bw.code_for("NIFTY"), "cash", None, None))
    _say(f"  ...  · symbols with a tick: {kinds} · NIFTY spot {spot.get('ltp') if spot else None}")
    if bw.ticks_received == 0:
        if closed:
            _say("  OK   · 0 ticks is EXPECTED on a closed market. Session, websocket, codes and subscriptions all work.")
        else:
            ok = False
            _say("  FAIL · market is open but no tick arrived: check the token (expired?) or the Breeze feed.")
    else:
        if not kinds.get("option"):
            _say("  WARN · ticks arrived but no OPTION tick yet (chain subscriptions only start at the first spot; fine if < 10 s)")
        _say(f"  OK   · live ticks flowing: {bw.ticks_received:,}")
    try:
        bw.disconnect()
    except Exception:
        pass
    _say("SMOKE " + ("PASSED — ready for the live session (use TODAY's token on the day)." if ok else "FAILED — see above."))
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="VAJRA pin-risk / gamma-explosion engine")
    p.add_argument("--config", default=None)
    p.add_argument("--mode", default=None, choices=[None, "live", "replay", "synthetic", "real"])
    p.add_argument("--date", default=None, help="session date YYYY-MM-DD (replay/synthetic)")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--token", default=None,
                   help="today's Breeze SESSION_KEY (2.0: no need to edit credentials.py daily)")
    p.add_argument("--smoke", type=int, default=0, metavar="SECS",
                   help="2.0.1 preflight: connect to Breeze with the real credentials, resolve codes and "
                        "strike steps, subscribe everything, listen SECS seconds, report, exit. Works on a "
                        "closed market (expects 0 ticks then). Writes no tapes.")
    p.add_argument("--pace", type=float, default=0.0,
                   help="replay only: play the tape at tape-time / PACE (1 = real time, 10 = 10x). "
                        "0 (default) = as fast as possible.")
    p.add_argument("--port", type=int, default=None, help="dashboard port override (default: server.port)")
    args = p.parse_args(argv)
    if args.token:
        import os
        os.environ["VAJRA_SESSION_KEY"] = args.token.strip()
    if args.smoke:
        raise SystemExit(smoke_test(args.config, int(args.smoke)))
    session_date = date.fromisoformat(args.date) if args.date else None
    out = run(config_path=args.config, mode=args.mode, session_date=session_date,
              with_dashboard=(False if args.no_dashboard else None), pace=args.pace, port=args.port)
    _say(f"VAJRA session tape written to: {out}")


if __name__ == "__main__":
    main()
