"""Dashboard — localhost monitoring + status API (port from config; NOT 8765).

A trader's cockpit onto the engine. 2.1: the page is a static app in ``vajra/static/``:
  * ``index.html`` / ``styles.css`` / ``ui.js`` — the presentation (see docs/DASHBOARD_STATE_CONTRACT.md)
  * ``data.js``  — the frozen transport: SSE push with coalesced repaint, polling fallback, feed watchdog,
                   lag pill, the 10-s client beacon
  * ``sound.js`` — the frozen "sound council" Web-Audio player for engine-issued cues

The server pushes every engine round over Server-Sent Events (``/api/stream``): a small "hot" update per
round, the full state once a second, a keep-alive every 2 s. 2.1: the JSON is serialised ONCE per round
and shared by every connected client (2.0 serialised per client, ~0.4 % of a core each).
"""

from __future__ import annotations

import os

try:                                   # module-level so FastAPI can resolve the (string) annotation
    from fastapi import Request
except Exception:                      # pragma: no cover — fastapi missing: dashboard unavailable anyway
    Request = None

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_PAGE_FILES = ("index.html", "styles.css", "sound.js", "data.js", "ui.js")


def _read_static(name: str) -> str:
    try:
        with open(os.path.join(STATIC_DIR, name), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def page_source() -> str:
    """Every page asset concatenated (the contract tests grep this)."""
    return "\n".join(_read_static(n) for n in _PAGE_FILES)


class _PageProxy(str):
    """``dashboard._PAGE`` stays a string for the 2.0 tests, re-read from disk on each access."""
    def __new__(cls):
        return str.__new__(cls, page_source())


def __getattr__(name):                 # PEP 562: dashboard._PAGE -> the live page source
    if name == "_PAGE":
        return page_source()
    raise AttributeError(name)


def build_app(orch):
    import asyncio
    import json
    import math
    import time
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="VAJRA 2.1", docs_url=None, redoc_url=None, openapi_url=None)

    # Serve the operator's downloaded alert sounds from vajra/sounds/ (drop your .mp3/.wav here).
    sounds_dir = os.path.join(os.path.dirname(__file__), "sounds")
    os.makedirs(sounds_dir, exist_ok=True)
    # 2.0.2 (sound council 02/09): the page must never play a stale cached copy of a cue. Sound files
    # are served no-cache, and the page re-fetches them whenever their fingerprint (sounds_rev) moves.
    # 2.1: the page assets are no-cache too (an operator refresh must always get the current UI).
    # 2.1.1: done with a 12-line pure-ASGI wrapper, NOT Starlette's BaseHTTPMiddleware: behind that
    # middleware an SSE generator never learns the client went away (request.is_disconnected() is always
    # False there), so every page reload left a zombie stream serialising state 4x/s for the whole day.
    class _NoCache:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            async def _send(msg):
                if msg.get("type") == "http.response.start":
                    hdrs = [(k, v) for k, v in msg.get("headers", []) if k.lower() != b"cache-control"]
                    hdrs.append((b"cache-control", b"no-cache, must-revalidate"))
                    msg = {**msg, "headers": hdrs}
                await send(msg)
            await self.inner(scope, receive, _send)

    app.mount("/sounds", _NoCache(StaticFiles(directory=sounds_dir)), name="sounds")
    os.makedirs(STATIC_DIR, exist_ok=True)
    app.mount("/static", _NoCache(StaticFiles(directory=STATIC_DIR)), name="static")
    _NOCACHE_HDR = {"Cache-Control": "no-cache, must-revalidate"}

    _rev = {"t": 0.0, "v": "0"}

    def sounds_rev() -> str:
        now = time.time()
        if now - _rev["t"] > 5.0:
            import hashlib
            h = hashlib.sha1()
            for fn in sorted(os.listdir(sounds_dir)):
                if fn.endswith(".mp3"):
                    st_ = os.stat(os.path.join(sounds_dir, fn))
                    h.update(f"{fn}:{st_.st_size}:{int(st_.st_mtime)};".encode())
            _rev["v"], _rev["t"] = h.hexdigest()[:10], now
        return _rev["v"]

    # 2.0.2: engine-side sound WATCH -- never plays anything; warns on the console when no page can play
    from .soundguard import SoundGuard, run_guard
    _sc = getattr(getattr(orch, "sounds", None), "cfg", {}) or {}
    guard = SoundGuard(sounds_dir,
                       enabled=str(_sc.get("sound_watch", "on")).lower() not in ("off", "0", "no", "false"),
                       stale_secs=float(_sc.get("sound_watch_secs", 30)))
    app.state.sound_guard = guard
    if guard.enabled and getattr(orch, "sounds", None) is not None:
        run_guard(orch, guard)

    def _clean(o):
        # 2.0: Starlette refuses NaN/inf (allow_nan=False) -> 1.x returned HTTP 500 and the cockpit
        # showed a false DATA FROZEN whenever e.g. a VIX tick arrived without `last`.
        if isinstance(o, float):
            return o if math.isfinite(o) else None
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_clean(v) for v in o]
        return o

    # 2.1.1: orjson (5-10x faster, NaN/inf -> null natively, so no pure-Python _clean pass) when present
    try:
        import orjson as _orjson

        def _dumps(body) -> bytes:
            return _orjson.dumps(body, default=str)
    except Exception:                                   # pragma: no cover - fallback without orjson
        _orjson = None

        def _dumps(body) -> bytes:
            return json.dumps(_clean(body), separators=(",", ":"), default=str).encode("utf-8")

    from fastapi.responses import Response

    @app.get("/api/state")
    def state():
        body = dict(orch.latest_state or {})
        body["server_ms"] = int(time.time() * 1000)
        body["sounds_rev"] = sounds_rev()
        body["snd_fallback"] = guard.played_seqs()
        return Response(_dumps(body), media_type="application/json")

    # ---- 2.0: PUSH, not poll -------------------------------------------------------------
    HEAVY = ("nifty_chain", "burst_board", "breach_alerts", "events", "level_history",
             "instruments", "journal")

    def _hot(st):
        return {k: v for k, v in st.items() if k not in HEAVY}

    # 2.1.1: one serialisation per (state_seq, kind) shared by every SSE client; the FULL frame is built
    # only on the round that sends it (3 of 4 were thrown away), frames are bytes, and the generator is
    # woken by the orchestrator (state_wake) instead of polling the attribute every 20 ms.
    _cache = {"seq": None, "hot": None, "full": None, "snd": None, "at": 0.0, "ser_ms": 0.0}
    _streams = {"active": 0, "opened": 0}
    _wake = {"loop": None, "ev": None}

    def _extra():
        return {"server_ms": int(time.time() * 1000), "sounds_rev": sounds_rev(), "snd_fallback": guard.played_seqs()}

    def _frames(st, seq, want_full):
        """(hot_bytes, full_bytes|None, snd_key) for this state, serialised at most once per kind."""
        if _cache["seq"] != seq:
            t0 = time.perf_counter()
            _se = st.get("sound_events") or []
            _cache.update(seq=seq, snd=(_se[-1].get("seq") if _se else None), at=time.time(),
                          hot=_dumps({**_hot(st), **_extra()}), full=None)
            _cache["ser_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        if want_full and _cache["full"] is None:
            _cache["full"] = _dumps({**st, **_extra()})
        return _cache["hot"], _cache["full"], _cache["snd"]

    def _install_wake():
        """Called from the running loop: the orchestrator's state setter sets an asyncio.Event."""
        if _wake["ev"] is not None:
            return _wake["ev"]
        loop = asyncio.get_running_loop()
        ev = asyncio.Event()
        _wake["loop"], _wake["ev"] = loop, ev

        def _poke():
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:                        # loop closed
                pass
        try:
            orch.state_wake = _poke
        except Exception:
            pass
        return ev

    @app.get("/api/stream")
    async def stream(request: Request):
        ev = _install_wake()

        async def gen():
            _streams["active"] += 1
            _streams["opened"] += 1
            last_seq = -1
            last_full = 0.0
            last_send = 0.0
            try:
                yield b"retry: 1000\n\n"
                while True:
                    seq = getattr(orch, "state_seq", 0)
                    st = orch.latest_state or {}
                    now = time.time()
                    if seq != last_seq:                 # every published state (pre-open ones too) goes out
                        last_seq = seq
                        full = (now - last_full) >= 1.0
                        if full:
                            last_full = now
                        hot, full_b, _snd = _frames(st, seq, full)
                        yield (b"event: " + (b"full" if full else b"hot") + b"\ndata: "
                               + (full_b if full else hot) + b"\n\n")
                        last_send = now
                    elif now - last_send >= 2.0:
                        yield ('event: beat\ndata: {"server_ms":%d,"sounds_rev":"%s","ser_ms":%s,"streams":%d}\n\n'
                               % (int(now * 1000), sounds_rev(), _cache["ser_ms"], _streams["active"])).encode()
                        last_send = now
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    ev.clear()
            finally:
                _streams["active"] -= 1
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/client")
    async def client_beacon(request: Request):
        """2.0.1: the page reports every 10 s what it painted and played (client_tape.jsonl), so a
        dropped round or a missed sound is provable from the BROWSER side at EOD. Log-only."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False}, status_code=400)
        if not isinstance(body, dict) or len(json.dumps(body)) > 20_000:
            return JSONResponse({"ok": False}, status_code=400)
        fn = getattr(orch.tape, "client", None)
        if callable(fn):                                 # 2.1.1: the jsonl write+flush leaves the event loop
            try:
                from starlette.concurrency import run_in_threadpool
                await run_in_threadpool(fn, body)
            except Exception:
                pass
        try:
            guard.beacon(body)
        except Exception:
            pass
        return {"ok": True}

    @app.get("/api/health")
    def health():
        st = orch.latest_state or {}
        return Response(_dumps({"ok": True, "engine": "VAJRA", "version": "2.2.2", "round_ts": st.get("round_ts"),
                                "state_seq": getattr(orch, "state_seq", None), "streams": dict(_streams),
                                "ser_ms": _cache["ser_ms"], "round": st.get("round"),
                                "feed_health": st.get("feed_health"), "ws": st.get("ws")}),
                        media_type="application/json")

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = _read_static("index.html")
        if not html:
            return HTMLResponse("<h1>VAJRA: vajra/static/index.html is missing</h1>", status_code=500)
        return HTMLResponse(html, headers=_NOCACHE_HDR)

    return app


__all__ = ["build_app", "page_source", "STATIC_DIR"]
