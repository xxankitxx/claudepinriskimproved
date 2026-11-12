"""Self-contained ICICI Direct BreezeConnect wrapper.

Re-implements the MYTHOS integration *patterns* independently (spec §0.7 vs the
operator's independence requirement): session handling, the websocket subscribe
layer, the India-VIX decimal-shift fix, runtime instrument-code resolution, and
option order placement.  NOTHING is imported from the MYTHOS folder.

`breeze_connect` is imported lazily inside ``connect()`` so the rest of VAJRA
(engine, tape, verify, replay) imports cleanly without the SDK or credentials.

Instrument codes (carried over from MYTHOS's verified live integration):
  * India VIX  -> "INDVIX"  (NOT a guessed code).
  * NIFTY      -> "NIFTY";  BANKNIFTY -> "CNXBAN";  FINNIFTY -> "NIFFIN".
  * Heavyweight stocks -> resolved AT RUNTIME via breeze.get_names() to the
    isec_stock_code; a guessed broker code can subscribe to the WRONG stock, so
    we never hard-code them (operator override map available as a fallback).
  * Websocket expiry format -> "DD-Mon-YYYY" (e.g. 16-Jun-2026), built from an
    explicit English month list — strftime('%b') is locale-dependent and on a
    non-English Windows locale yields e.g. 'juin', silently failing every option
    subscription (a real MYTHOS audit finding).

2.1 — the live-feed hardening (from the 21 Oct / 2 Nov 2026 tapes):
  * The socket is SUPERVISED on its own thread. A silent / dropped websocket is torn down and
    re-opened there, never on the engine thread (2.0 blocked the round loop 3-5.5 s per reconnect
    = no stop management while it waited).
  * Every subscription is remembered as its broker TOKEN; after any reconnect the whole set is
    re-joined in ONE packet (the SDK's own 'rewatch' path), so nothing is silently lost.
  * The SDK latches `authentication=False` on the first connect_error and then refuses EVERY later
    subscribe for the rest of the day ("Could not authenticate credentials": 6,655 refusals on
    2 Nov, every new strike after 09:50 on 21 Oct). The latch is cleared on every (re)connect and
    before every subscribe; a genuinely dead token shows up as a socket that never reconnects.
  * Old client objects are shut down (their reconnect threads aborted) instead of being orphaned
    while a second client logs in with the same session -- the orphan's reconnects were the
    connect_errors that tripped the latch.
  * A 0 bid/ask/volume is a real value, not a missing one (2.0 carried the previous value forward).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

# Fixed index + VIX Breeze codes (verified live in MYTHOS).
INDEX_CODE = {"NIFTY": "NIFTY", "BANKNIFTY": "CNXBAN", "FINNIFTY": "NIFFIN"}
# Breeze WEBSOCKET ticks carry NO stock_code — only `stock_name` (e.g. "NIFTY 50") + a token
# `symbol` (e.g. "4.1!79805"). Map the stock_name back to OUR instrument so the snapshot key matches
# what _build_snapshot looks up (code_for(instrument)). Indices confirmed live 2026-06-29.
WS_NAME_TO_INSTRUMENT = {
    # indices (real ws stock_name, captured live 2026-06-29)
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FINANCIAL SERVICES INDEX": "FINNIFTY",
    # heavyweight stocks (real ws stock_name -> our universe symbol; code_for() resolves to the ISEC code)
    "HDFC BANK LIMITED": "HDFCBANK",
    "ICICI BANK LIMITED": "ICICIBANK",
    "RELIANCE INDUSTRIES": "RELIANCE",
    "BHARTI AIRTEL LTD": "BHARTIARTL",
    "LARSEN AND TOUBRO LIMITED": "LT",
    "INFOSYS LTD": "INFY",
    "TATA CONSULTANCY SERVICES LTD": "TCS",
    "STATE BANK OF INDIA": "SBIN",
    "AXIS BANK LIMITED": "AXISBANK",
    "KOTAK MAHINDRA BANK LTD": "KOTAKBANK",
    "ITC LIMITED": "ITC",
    "FEDERAL BANK LTD": "FEDERALBNK",
    "INDUSIND BANK LIMITED": "INDUSINDBK",
    "AU SMALL FINANCE BANK LTD": "AUBANK",
    "BANK OF BARODA": "BANKBARODA",
    "IDFC FIRST BANK LIMITED": "IDFCFIRSTB",
    "BSE LIMITED": "BSE",
    "BAJAJ FINANCE LIMITED": "BAJFINANCE",
    "SHRIRAM FINANCE LIMITED": "SHRIRAMFIN",
    "BAJAJ FINSERV LIMITED": "BAJAJFINSV",
    "SBI LIFE INSURANCE COMPANY LTD": "SBILIFE",
}
INDIA_VIX_CODE = "INDVIX"

RIGHT_TO_BREEZE = {"CE": "call", "PE": "put"}


class OrderPlacementError(RuntimeError):
    """Raised when a live Breeze order did NOT come back with a real order_id (so it must
    never be treated as a fill)."""


class BreezeConnectionError(RuntimeError):
    """Raised when the Breeze session/websocket could not be established after the configured
    retries — so the caller can log SESSION_ERROR and degrade instead of dying uncaught."""


_MONTHS_EN = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def ws_expiry(iso_date: str) -> str:
    """Breeze websocket/REST expiry format: 16-Jun-2026.

    Built from an explicit English month list — strftime('%b') is LOCALE-
    dependent and on a non-English Windows locale would yield e.g. 'juin',
    making every option subscription silently fail to resolve a token.
    """
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.day:02d}-{_MONTHS_EN[dt.month]}-{dt.year}"


@dataclass
class BreezeCredentials:
    api_key: str
    api_secret: str
    session_token: str


def normalize_vix(raw: float, apply_fix: bool = True) -> float:
    """Carry-over of the MYTHOS India-VIX decimal-shift fix.

    Breeze streams India VIX shifted by /100 (e.g. 0.25 for a true 25.0).
    India VIX realistically lives in ~8..90, so a value below 5 is the shifted
    form and is multiplied by 100; an already-correct value is passed through.
    """
    if raw is None:
        return float("nan")
    if apply_fix and 0 < raw < 5.0:
        return raw * 100.0
    return float(raw)


_UNKNOWN_CONTRACT = ""          # _token_for: the SDK's script list has no such contract (never join it)


def _bogus_token(tok) -> bool:
    """The SDK builds a stream token as '<exch>1!<value>' and spells a failed lookup as the literal
    'False' -> '4.1!False'. Joining it is noise (74 times on the 3 Nov tape)."""
    return (not isinstance(tok, str)) or tok == "" or tok.endswith("!False") or tok.endswith("!None")


try:                                    # 2.3: orjson decodes a tick ~5x faster than json
    import orjson as _orjson
    _loads = _orjson.loads
except Exception:                       # pragma: no cover - plain json keeps everything working
    import json as _json
    _loads = _json.loads
_STOCK_PREFIX = '2["stock",'         # socket.io EVENT as engine.io hands it over (its own '4' already stripped)


class _BufferedSock:
    """Read-side buffer over the websocket's socket (see BreezeWrapper._buffer_socket). Everything but
    ``recv`` is the real socket's; ``recv`` only touches the network when the buffer is empty."""

    __slots__ = ("_s", "_b")
    CHUNK = 65536

    def __init__(self, sock):
        self._s = sock
        self._b = bytearray()

    def recv(self, n, *a):
        b = self._b
        if not b:
            d = self._s.recv(self.CHUNK if n < self.CHUNK else n)
            if not d or len(d) <= n:
                return d
            b += d
        out = bytes(b[:n])
        del b[:n]
        return out

    def __getattr__(self, k):
        return getattr(self._s, k)


class BreezeWrapper:
    INBOX_MAX = 60_000              # raw socket.io packets waiting for the consumer (oldest evicted)

    def __init__(self, creds: BreezeCredentials, vix_fix: bool = True,
                 reconnect_max_retries: int = 5, reconnect_backoff_secs: float = 2.0,
                 sdk_reconnect_grace_secs: float = 10.0, keepalive_secs: float = 10.0,
                 buffered_reader: bool = True, switch_interval_ms: float = 0.0):
        self.creds = creds
        self.buffered_reader = bool(buffered_reader)    # 2.2: one big socket read per wake-up (see _buffer_socket)
        self.switch_interval_ms = float(switch_interval_ms)
        self.sockets_buffered = 0
        self.vix_fix = vix_fix
        self.reconnect_max_retries = reconnect_max_retries
        self.reconnect_backoff_secs = reconnect_backoff_secs
        self.sdk_reconnect_grace_secs = float(sdk_reconnect_grace_secs)
        # 2.1.1 (3 Nov tape): python-engineio's write loop ABORTS the socket when nothing has been
        # queued for max(pingInterval, pingTimeout)+5 s (= 30 s on Breeze) -- the server's PINGs are
        # the only regular traffic, and when the read loop lags behind a tick flood they arrive late
        # -> "transport error" every ~30 s all morning. A tiny idempotent re-join every keepalive_secs
        # keeps the write loop fed; 0 disables.
        self.keepalive_secs = float(keepalive_secs)
        self.keepalives = 0
        self._last_keepalive = time.monotonic()
        # 2.1.1 tick intake (3 Nov tape: NIFTY spot 15-25 s behind the exchange all day, socket kicked
        # every ~2 min). python-engineio starts a NEW OS THREAD for every socket.io message; a burst
        # of ~850 re-join snapshots means hundreds of threads fighting for the GIL and for our lock,
        # out-of-order application into `latest`, and a read loop so starved the PONG is late enough
        # for the server to drop us. Now: the engine.io 'message' event is intercepted with an O(1)
        # deque append and ONE consumer thread dispatches in receive order.
        self._inbox: deque = deque(maxlen=self.INBOX_MAX)      # (recv_s, sio, raw socket.io packet)
        self._inbox_ev = threading.Event()
        self._consumer: threading.Thread | None = None
        self.inbox_dropped = 0                          # packets evicted from a full inbox
        self.inbox_max_seen = 0
        self.consumed = 0
        self.fast_parsed = 0                            # ticks parsed by our fast path (no SDK dicts)
        self.tick_lag_s: float | None = None            # NIFTY cash: receive time - exchange time (EMA)
        self._tok_meta: dict[str, dict] = {}            # token -> contract meta for the fast parser
        self._sub_err_seen: dict[str, tuple] = {}       # message -> (last logged mono, count since)
        self._static: dict[str, tuple] = {}             # token -> (code, kind, strike, right, key, is_vix)
        self._ltt_cache: tuple = (None, None)           # (epoch_s, 'YYYY-MM-DD HH:MM:SS')
        self._breeze = None
        self._lock = threading.Lock()
        self.latest: dict[str, dict] = {}              # normalized ticks by routing key
        self._stock_codes: dict[str, str] = {}         # symbol -> resolved isec_stock_code
        self._code_source: dict[str, str] = {}         # symbol -> get_names | override | symbol_fallback
        self._unmapped_warned: set = set()             # ws names already warned-about (one warning each)
        self._subs: deque = deque(maxlen=5000)         # recent subscribes (kwargs; smoke test / audit)
        self._tokens: set[str] = set()                 # every broker stream token ever subscribed
        self._token_kw: dict[str, dict] = {}           # token -> the kwargs that produced it
        self.raw_recorder = None                       # RawTickRecorder (set by app in live mode)
        self.ticks_received = 0
        self.bad_ticks = 0
        self.last_tick_mono = time.monotonic()         # wall time of the last websocket tick
        self._connected = False
        self._session_ok = False
        # ---- 2.1 supervision / health -------------------------------------------------------
        self.ws_connects = 0
        self.ws_drops = 0
        self.ws_connect_errors = 0
        self.reconnects_ok = 0
        self.reconnects_failed = 0
        self.sub_errors = 0
        self.last_sub_error: str | None = None
        self.last_connect_error: str | None = None
        self.last_connect_mono: float | None = None
        self.last_drop_mono: float | None = None
        self._events: deque = deque(maxlen=200)        # (mono, stage, payload) for the feed to tape
        self._ev_lock = threading.Lock()
        self._reconnect_req = threading.Event()
        self._reconnect_reason = ""
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._busy = False
        self._busy_since: float | None = None
        # ---- 2.2.2 stream watch (12 Nov tape) -------------------------------------------------------
        # After 3 of the day's reconnects the server ignored the re-join: 0-6 streams ticked in the next
        # 20 s (a good reconnect: 300-400 within 5 s) and the NIFTY spot stayed dead 14, 27 and 10 min,
        # because a few near-ATM options came back through the stale-quote re-subscribe and so the
        # socket never looked silent. Every connect is now VERIFIED and the NIFTY spot is WATCHED.
        self.stream_verify_secs = 10.0                  # look this long after a connect (12 Nov: healthy
                                                        # reconnects >= 317 streams by 10 s, failed ones <= 6 at 20 s)
        self.stream_min_frac = 0.10                     # ...for at least this share of the tokens
        self.spot_silent_secs = 15.0                    # NIFTY spot silent this long while others tick
        self.stream_watch_hours = ("09:15:30", "15:29:00")   # verify connects only in this IST window
        self.spot_watch_hours = ("09:15:30", "15:14:00")     # the index stops ticking at the freeze
        self.spot_key: str | None = None                # set by set_spot_watch() (the NIFTY cash key)
        self._connect_ms: int | None = None             # wall ms of the last connect (any path)
        self._verify_at: float | None = None            # monotonic time of the pending verification
        self._verify_attempt = 0
        self._spot_action_at = 0.0                      # last spot-watch action (rate limit)
        self._spot_attempt = 0
        self._spot_rebuilds: deque = deque(maxlen=8)    # monotonic times of spot-driven socket rebuilds
        self._spot_backoff_until = 0.0
        self.stream_rejoins = 0                         # re-joins the watch had to make
        self.stream_rebuilds = 0                        # sockets it had to rebuild

    # ---- session ------------------------------------------------------------
    def connect(self):
        """Generate the Breeze session ONCE and open the websocket, retrying the socket with
        backoff. Raises BreezeConnectionError once the configured retries are exhausted so the
        caller can log SESSION_ERROR and degrade instead of dying on an uncaught exception."""
        last_err = None
        attempts = max(1, self.reconnect_max_retries)
        for attempt in range(1, attempts + 1):
            try:
                if not self._session_ok:
                    from .sdk_boot import import_sdk       # lazy — only needed live; cached security master
                    BreezeConnect = import_sdk().BreezeConnect
                    self._breeze = BreezeConnect(api_key=self.creds.api_key)
                    self._breeze.generate_session(
                        api_secret=self.creds.api_secret,
                        session_token=self.creds.session_token,
                    )
                    self._breeze.on_ticks = self._on_ticks
                    # 2.1.1 (3 Nov tape): the SDK's get_stock_token_value reads self.interval, which the
                    # SDK only assigns inside subscribe_feeds -> the FIRST lookup (the NIFTY spot) raised
                    # AttributeError, the token was never recorded, and the first rebuilt socket lost the
                    # spot for the rest of the day (frozen 36 min). Give the attribute its idle value now.
                    if not hasattr(self._breeze, "interval"):
                        self._breeze.interval = ""
                    self._session_ok = True            # a session is generated once; only the socket retries
                # 2.2 lowered the interpreter switch interval to 0.5 ms. 2.2.2 (12 Nov tape): on a busy
                # laptop every forced hand-over waits for Windows to schedule the other thread, and with
                # ticks flowing that is ~2000 times a second -- engine rounds ran 10x slower (median
                # 270 ms; 647 vs 136 ms in a loaded benchmark). The buffered reader alone keeps up at the
                # default 5 ms, so the default is kept unless feed.switch_interval_ms is set (> 0).
                if self.switch_interval_ms > 0:
                    try:
                        import sys as _sys
                        if _sys.getswitchinterval() > self.switch_interval_ms / 1000.0:
                            _sys.setswitchinterval(self.switch_interval_ms / 1000.0)
                    except Exception:
                        pass
                self._start_consumer()                 # must be alive before any hooked socket exists
                self._breeze.ws_connect()
                self._hook_handler()
                self._connected = True
                self.ws_connects += 1
                self.last_connect_mono = time.monotonic()
                self.last_tick_mono = time.monotonic()
                self._start_supervisor()
                return self
            except Exception as e:                      # broker / network / token failure
                last_err = e
                self._connected = False
                if attempt < attempts:                  # linear backoff between attempts
                    time.sleep(self.reconnect_backoff_secs * attempt)
        raise BreezeConnectionError(
            f"Breeze connect failed after {attempts} attempt(s): {last_err!r}")

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- socket.io handler hooks (2.1) --------------------------------------
    def _handler(self):
        return getattr(self._breeze, "sio_rate_refresh_handler", None) if self._breeze is not None else None

    def _log(self, stage: str, **payload) -> None:
        with self._ev_lock:
            self._events.append((time.time(), stage, payload))

    def drain_events(self) -> list[tuple[float, str, dict]]:
        """Supervision events since the last call (consumed by the feed on the ENGINE thread, which
        is the only thread that writes the tape)."""
        with self._ev_lock:
            out = list(self._events)
            self._events.clear()
        return out

    def _hook_handler(self) -> None:
        """Attach our own connect / disconnect / connect_error handlers to the SDK's socket.io
        client. The SDK registers 'connect' -> handler.rewatch inside every subscribe, so the hook
        is installed as the INSTANCE attribute `rewatch` (what the SDK registers) and calls the
        original: the token re-join keeps working and the auth latch is cleared on every connect."""
        h = self._handler()
        if h is None or getattr(h, "_vajra_hooked", False):
            return
        orig_rewatch = h.rewatch
        wrapper = self

        def _on_connect(*_a):
            h.authentication = True                     # clear the SDK's permanent refusal latch
            wrapper.ws_connects += 1
            wrapper.last_connect_mono = time.monotonic()
            wrapper.last_tick_mono = time.monotonic()   # a fresh socket gets a fresh silence window
            wrapper._log("ws_connected", tokens=len(wrapper._tokens))
            wrapper._relax_eio_timeouts(h)
            wrapper._clean_sdk_tokens(h)                # 2.2.2: never re-join a garbage token
            try:
                orig_rewatch()                          # the SDK re-joins its tokenlist in one packet
                wrapper._last_keepalive = time.monotonic()
            except Exception as e:
                wrapper._log("ws_rejoin_failed", reason=repr(e))
            wrapper._schedule_verify()                  # 2.2.2: and make sure the streams came back

        def _on_disconnect(*a):
            wrapper.ws_drops += 1
            wrapper.last_drop_mono = time.monotonic()
            wrapper._log("ws_dropped", reason=(str(a[0]) if a else ""))

        def _on_connect_error(*a):
            wrapper.ws_connect_errors += 1
            wrapper.last_connect_error = str(a[0]) if a else "connect_error"
            wrapper._log("ws_connect_error", reason=wrapper.last_connect_error)
            # deliberately NOT h.authentication = False (the SDK default): one transient rejection
            # must not refuse every subscription for the rest of the day.

        h.rewatch = _on_connect
        h.my_connect_error = _on_connect_error
        # 2.1.1: the SDK registers `self.on_message` for 'stock' at watch()/rewatch() time by attribute
        # lookup, so an instance attribute replaces its parse_data + reverse-lookup path with ours.
        h.on_message = wrapper._fast_on_message
        try:
            h.sio.on("connect", _on_connect)
            h.sio.on("disconnect", _on_disconnect)
            h.sio.on("connect_error", _on_connect_error)
        except Exception:
            pass
        self._hook_eio(h)
        h.authentication = True
        h._vajra_hooked = True

    def _hook_eio(self, h) -> None:
        """Intercept engine.io's 'message' dispatch (which starts a thread per packet) and queue the raw
        socket.io packet for the single consumer thread instead. connect / disconnect events keep the
        library's own path. Falls back to the library if our consumer is not alive."""
        sio = getattr(h, "sio", None)
        eio = getattr(sio, "eio", None) if sio is not None else None
        if eio is None or getattr(eio, "_vajra_hooked", False):
            return
        orig = getattr(eio, "_trigger_event", None)
        if orig is None or not hasattr(sio, "_handle_eio_message"):
            return
        inbox, ev, wrapper = self._inbox, self._inbox_ev, self

        def _trigger(event, *args, **kwargs):
            # runs on the socket READER thread, between two reads: the one safe place to swap the socket
            ws = getattr(eio, "ws", None)
            if ws is not None and not getattr(ws, "_vajra_buf", False):
                wrapper._buffer_socket(ws)
            if event == "message" and args and wrapper._consumer_alive():
                if len(inbox) >= inbox.maxlen:
                    wrapper.inbox_dropped += 1
                inbox.append((time.time(), sio, args[0]))
                ev.set()
                return None
            return orig(event, *args, **kwargs)

        eio._trigger_event = _trigger
        eio._vajra_hooked = True
        try:                                            # make the three read-loop exits distinguishable
            import logging
            lg = logging.getLogger("engineio.client")
            if not any(getattr(hd, "_vajra", False) for hd in lg.handlers):
                from pathlib import Path
                Path("logs").mkdir(exist_ok=True)
                fh = logging.FileHandler("logs/engineio.log", encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
                fh._vajra = True
                lg.addHandler(fh)
                lg.setLevel(logging.WARNING)
        except Exception:
            pass

    def _buffer_socket(self, ws) -> None:
        """2.2 (9 Nov tape: NIFTY 25-50 s behind the exchange for two hours, 93 drops). websocket-client
        reads each frame with three small ``recv`` calls; every one releases the interpreter lock and,
        while another thread is busy, waits a full switch interval (5 ms) to get it back -- measured cap
        35 frames/s against the 300/s the feed needs. One 64 KB read now serves hundreds of frames, and
        the pure-Python UTF-8 check of every frame is skipped (the JSON parser validates anyway)."""
        try:
            ws._vajra_buf = True
            if not self.buffered_reader:
                return
            sock = getattr(ws, "sock", None)
            if sock is not None and not isinstance(sock, _BufferedSock):
                ws.sock = _BufferedSock(sock)
            for part in (getattr(ws, "frame_buffer", None), getattr(ws, "cont_frame", None)):
                if part is not None and hasattr(part, "skip_utf8_validation"):
                    part.skip_utf8_validation = True
            self.sockets_buffered += 1
        except Exception:
            pass

    # ---- the consumer thread (2.1.1) ------------------------------------------------------------
    def _consumer_alive(self) -> bool:
        t = self._consumer
        return t is not None and t.is_alive()

    def _start_consumer(self) -> None:
        if self._consumer_alive():
            return
        self._stop.clear()
        self._consumer = threading.Thread(target=self._consume, name="vajra-tick-consumer", daemon=True)
        self._consumer.start()

    def _consume(self) -> None:
        inbox, ev = self._inbox, self._inbox_ev
        while not self._stop.is_set():
            if not inbox:
                if ev.wait(0.25):
                    ev.clear()
                continue
            n = len(inbox)
            if n > self.inbox_max_seen:
                self.inbox_max_seen = n
            for _ in range(min(n, 4000)):
                try:
                    _recv_s, sio, data = inbox.popleft()
                except IndexError:
                    break
                try:
                    # 2.3: a quote tick arrives here as the text '2["stock",[...]]'. Decode it (orjson) and hand
                    # the list straight to our parser; python-socketio's packet / namespace / handler
                    # machinery cost more than the tick itself. Everything else keeps the library path.
                    if type(data) is str and data.startswith(_STOCK_PREFIX):
                        arr = _loads(data[1:])
                        if len(arr) == 2 and arr[0] == "stock":
                            self._fast_on_message(arr[1])
                        else:
                            sio._handle_eio_message(data)
                    else:
                        sio._handle_eio_message(data)   # CONNECT acks, other events
                except Exception:
                    self.bad_ticks += 1
                self.consumed += 1

    def _meta_for(self, sym: str) -> dict:
        """Contract meta for a stream token: OUR subscription kwargs first (exact key), the SDK's
        reverse lookup only for a token we never subscribed ourselves. Cached per token."""
        meta = self._tok_meta.get(sym)
        if meta is not None:
            return meta
        kw = self._token_kw.get(sym)
        if kw is not None:
            meta = {"stock_code": kw.get("stock_code"), "stock_name": "",
                    "product_type": kw.get("product_type", ""), "strike_price": kw.get("strike_price"),
                    "right": kw.get("right", "")}
        else:
            meta = {}
            fn = getattr(self._breeze, "get_data_from_stock_token_value", None)
            if fn is not None:
                try:
                    meta = dict(fn(sym) or {})
                except Exception:
                    meta = {}
            if not meta:
                meta = {"stock_code": sym, "stock_name": ""}
        self._tok_meta[sym] = meta
        return meta

    def _parse_fast(self, data) -> dict | None:
        """The broker's quote tick as the SDK-shaped dict `_normalize_tick` expects, without the SDK's
        per-tick dict building, string splitting and strftime('%c'). None -> let the SDK parse it."""
        if not (isinstance(data, list) and len(data) in (21, 23) and isinstance(data[0], str)):
            return None
        sym = data[0]
        bang = sym.find("!")
        if bang < 0 or sym[bang - 1:bang] != "1":       # only '<exch>.1!<token>' exchange quotes
            return None
        if len(data) == 23:                              # FONSE / CDNSE
            oi, ttq, ltt, close = data[12], data[14], data[21], data[22]
        else:                                            # NSE / BSE cash
            oi, ttq, ltt, close = None, data[12], data[19], data[20]
        t = {"symbol": sym, "last": data[2], "bPrice": data[6], "sPrice": data[8], "ltq": data[10],
             "avgPrice": data[11], "ttq": ttq, "close": close, "ltt": ltt}
        if oi is not None:
            t["OI"] = oi
        t.update(self._meta_for(sym))
        return t

    def _fast_on_message(self, data) -> None:
        """Replaces SocketEventBreeze.on_message on the handler (runs on the consumer thread)."""
        recv_ms = int(time.time() * 1000)
        t = None
        try:
            t = self._parse_fast(data)
        except Exception:
            t = None
        if t is not None:
            self.fast_parsed += 1
            # 2.3: the contract half of a tick (code, kind, strike, right, key) never changes for a
            # token. The first tick of a token goes through _normalize_tick (which also raises the
            # unmapped-name warning) and its result is remembered; later ticks only convert numbers.
            st = self._static.get(t["symbol"])
            if st is not None:
                self._ingest_fast(st, t, recv_ms)
                return
            self._ingest([t], recv_ms)
            sym = t["symbol"]
            try:
                n0 = self._normalize_tick(t)
                if n0 is not None:
                    name = (t.get("stock_name") or "").strip()
                    is_vix = ("VIX" in name.upper()) or (t.get("stock_code") or sym) == INDIA_VIX_CODE
                    self._static[sym] = (n0["stock_code"], n0["kind"], n0["strike"], n0["right"],
                                         n0["_key"], is_vix)
            except Exception:
                pass
            return
        try:                                             # anything else: the SDK's own parser
            d = self._breeze.parse_data(data)
            if isinstance(d, dict) and d.get("symbol"):
                d.update(self._breeze.get_data_from_stock_token_value(d["symbol"]) or {})
            if isinstance(d, dict) and d:
                self._ingest([d], recv_ms)
            else:
                self.bad_ticks += 1
        except Exception:
            self.bad_ticks += 1

    def _ingest_fast(self, st: tuple, t: dict, recv_ms: int) -> None:
        """One tick of a known token -> `latest` (and the raw recorder). Field for field what
        _normalize_tick + _ingest produce for the dict _parse_fast builds (tests/test_intake_23.py)."""
        code, kind, strike, right, key, is_vix = st
        v = t["last"] or t["close"]
        ltp = _f(v)
        if is_vix:
            ltp = normalize_vix(ltp, self.vix_fix)
        v = t["avgPrice"]
        vwap = _f(v) if v else None
        v = t["ttq"]
        if v is None or v == "":
            v = t["ltq"]
            if v == "":
                v = None
        vol = _i(v)
        v = t.get("OI")
        oi = _i(v) if v is not None and v != "" else None
        v = t["bPrice"]
        bid = _f(v) if v is not None and v != "" else None
        v = t["sPrice"]
        ask = _f(v) if v is not None and v != "" else None
        norm = {"stock_code": code, "kind": kind, "strike": strike, "right": right, "ltp": ltp,
                "vwap": vwap, "volume": vol, "oi": oi, "bid": bid, "ask": ask, "_key": key,
                "_recv_ms": recv_ms}
        ltt = t["ltt"]
        lag_sample = None
        if isinstance(ltt, (int, float)) and not isinstance(ltt, bool):
            norm["_ltt_s"] = int(ltt)
            if kind == "cash" and code == "NIFTY":
                lag_sample = recv_ms / 1000.0 - float(ltt)
        self.last_tick_mono = time.monotonic()
        rec = self.raw_recorder
        with self._lock:
            self.ticks_received += 1
            if rec is not None:
                try:
                    rec.record(norm, {"ltt": self._ltt_text(ltt)} if ltt is not None else t, recv_ms)
                except Exception:
                    pass
            prev = self.latest.get(key)
            if prev is not None:                    # carry forward a field a price-less tick omits
                if ltp is None and prev.get("ltp") is not None:
                    norm["ltp"] = prev["ltp"]
                if vwap is None and prev.get("vwap") is not None:
                    norm["vwap"] = prev["vwap"]
                if vol is None and prev.get("volume") is not None:
                    norm["volume"] = prev["volume"]
                if oi is None and prev.get("oi") is not None:
                    norm["oi"] = prev["oi"]
                if bid is None and prev.get("bid") is not None:
                    norm["bid"] = prev["bid"]
                if ask is None and prev.get("ask") is not None:
                    norm["ask"] = prev["ask"]
            self.latest[key] = norm
        if lag_sample is not None:
            prev_lag = self.tick_lag_s
            self.tick_lag_s = lag_sample if prev_lag is None else round(0.8 * prev_lag + 0.2 * lag_sample, 2)

    def _ltt_text(self, ltt):
        """Exchange time for the raw tape: epoch seconds -> 'YYYY-MM-DD HH:MM:SS' (cached per second;
        ticks share seconds), anything else unchanged."""
        if isinstance(ltt, (int, float)) and not isinstance(ltt, bool):
            sec = int(ltt)
            if self._ltt_cache[0] == sec:
                return self._ltt_cache[1]
            try:
                txt = datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                txt = str(sec)
            self._ltt_cache = (sec, txt)
            return txt
        return ltt

    @staticmethod
    def _relax_eio_timeouts(h) -> None:
        """python-engineio aborts the socket itself when its send queue stays empty for
        max(pingInterval, pingTimeout)+5 s (30 s on Breeze: 25 s / 20 s). Only the server's PINGs feed
        that queue, so a read loop lagging behind a tick flood kills the connection every 30 s (3 Nov
        tape). Raising the client's copy of pingTimeout widens that self-abort window; the server's
        own timeout is untouched."""
        try:
            eio = h.sio.eio
            if getattr(eio, "ping_timeout", None) is not None and eio.ping_timeout < 90:
                eio.ping_timeout = 90
        except Exception:
            pass

    def _keepalive(self) -> None:
        """Re-join one already-joined token (a no-op server side) so the write loop never idles out."""
        if self.keepalive_secs <= 0:
            return
        now = time.monotonic()
        if now - self._last_keepalive < self.keepalive_secs:
            return
        self._last_keepalive = now
        h = self._handler()
        sio = getattr(h, "sio", None) if h is not None else None
        if sio is None or not getattr(sio, "connected", False):
            return
        with self._lock:
            tok = next((t for t in sorted(self._tokens) if not _bogus_token(t)), None)
        if tok is None:
            return
        try:
            sio.emit("join", tok)
            self.keepalives += 1
        except Exception as e:
            self._log("ws_keepalive_failed", reason=repr(e))

    def _sio_state(self) -> tuple[bool, bool]:
        """(socket connected, SDK reconnect task alive)"""
        h = self._handler()
        if h is None:
            return False, False
        sio = getattr(h, "sio", None)
        if sio is None:
            return False, False
        task = getattr(sio, "_reconnect_task", None)
        alive = bool(task is not None and getattr(task, "is_alive", lambda: False)())
        return bool(getattr(sio, "connected", False)), alive

    # ---- supervisor thread (2.1) ---------------------------------------------
    def _start_supervisor(self) -> None:
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._stop.clear()
        self._supervisor = threading.Thread(target=self._supervise, name="vajra-ws-supervisor", daemon=True)
        self._supervisor.start()

    def request_reconnect(self, reason: str = "") -> bool:
        """Ask the supervisor to re-open the websocket. Non-blocking; returns False if one is
        already in progress."""
        if self._busy:
            return False
        self._reconnect_reason = reason
        self._reconnect_req.set()
        return True

    def _supervise(self) -> None:
        while not self._stop.is_set():
            if not self._busy:
                self._keepalive()
                try:
                    self._stream_watch()
                except Exception as e:                  # the watch must never take the supervisor down
                    self._log("stream_watch_error", reason=repr(e)[:160])
            tick = min(0.5, max(0.02, self.keepalive_secs / 4)) if self.keepalive_secs > 0 else 0.5
            if self._reconnect_req.wait(tick):
                self._reconnect_req.clear()
                if self._stop.is_set():
                    break
                self._busy, self._busy_since = True, time.monotonic()
                try:
                    self._do_reconnect(self._reconnect_reason)
                except Exception as e:
                    self.reconnects_failed += 1
                    self._log("ws_reconnect", ok=False, reason=repr(e))
                finally:
                    self._busy, self._busy_since = False, None

    def _do_reconnect(self, reason: str) -> bool:
        t0 = time.monotonic()
        connected, sdk_reconnecting = self._sio_state()
        # (1) the SDK's own reconnect loop may already be on it: give it a short grace window
        if not connected and sdk_reconnecting:
            deadline = t0 + self.sdk_reconnect_grace_secs
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.25)
                connected, _ = self._sio_state()
                if connected:
                    break
            if connected and time.monotonic() - self.last_tick_mono < self.sdk_reconnect_grace_secs:
                self.reconnects_ok += 1
                self._log("ws_reconnect", ok=True, how="sdk", secs=round(time.monotonic() - t0, 1), reason=reason)
                return True
        # (2) full teardown: abort the old client (and its reconnect thread), open a NEW one, re-join
        self._shutdown_handler()
        self._breeze.sio_rate_refresh_handler = None
        try:
            self._breeze.ws_connect()
        except Exception as e:
            self.reconnects_failed += 1
            self._connected = False
            self._log("ws_reconnect", ok=False, how="new_socket", reason=repr(e),
                      secs=round(time.monotonic() - t0, 1))
            return False
        self._hook_handler()
        self._connected = True
        n = self._rejoin_all()
        self.last_tick_mono = time.monotonic()          # fresh silence window for the new socket
        self._schedule_verify()
        self.reconnects_ok += 1
        self._log("ws_reconnect", ok=True, how="new_socket", resubscribed=n,
                  secs=round(time.monotonic() - t0, 1), reason=reason)
        return True

    # ---- 2.2.2 stream watch -------------------------------------------------------------------------
    def set_spot_watch(self, instrument: str = "NIFTY", until_hhmm: str | None = None) -> None:
        """Watch ``instrument``'s cash stream (the NIFTY spot) in market hours, up to the index freeze."""
        self.spot_key = self._key(self.code_for(instrument), "cash", None, None)
        if until_hhmm:
            hhmm = str(until_hhmm)
            if len(hhmm) == 5:                          # stop a minute before the freeze ("15:15" -> "15:14:00")
                h_, m_ = int(hhmm[:2]), int(hhmm[3:5])
                m_ -= 1
                if m_ < 0:
                    h_, m_ = h_ - 1, 59
                hhmm = f"{h_:02d}:{m_:02d}:00"
            self.spot_watch_hours = (self.spot_watch_hours[0], hhmm)

    @staticmethod
    def _in_ist_window(now_s: float, window: tuple[str, str]) -> bool:
        t = (int(now_s) + 19_800) % 86_400
        wd = int((now_s + 19_800) // 86_400 + 3) % 7      # 0 = Monday (1 Jan 1970 was a Thursday)
        if wd >= 5:
            return False

        def sec(x):
            p = [int(v) for v in str(x).split(":")] + [0, 0]
            return p[0] * 3600 + p[1] * 60 + p[2]
        return sec(window[0]) <= t < sec(window[1])

    def _schedule_verify(self) -> None:
        self._connect_ms = int(time.time() * 1000)
        self._verify_at = time.monotonic() + self.stream_verify_secs
        self._verify_attempt = 0

    def _clean_sdk_tokens(self, h) -> None:
        """Make the SDK's own re-join list = our clean token set: garbage tokens ('4.1!False', which the
        SDK records for a contract it could not resolve) out, every token we track in."""
        try:
            tl = getattr(h, "tokenlist", None)
            if not isinstance(tl, set):
                return
            for t in [t for t in tl if _bogus_token(t)]:
                tl.discard(t)
            with self._lock:
                ours = {t for t in self._tokens if not _bogus_token(t)}
            tl |= ours
        except Exception:
            pass

    def _streams_since(self, since_ms: int) -> int:
        with self._lock:
            return sum(1 for v in self.latest.values() if (v.get("_recv_ms") or 0) >= since_ms)

    def _stream_watch(self) -> None:
        """Runs on the supervisor thread. (1) a few seconds after every connect, check that the streams
        came back; if not, re-join from OUR token list (outside the connect callback), once more, then
        rebuild the socket. (2) the NIFTY spot silent while other streams tick -> same escalation."""
        h = self._handler()
        sio = getattr(h, "sio", None) if h is not None else None
        if sio is None or not getattr(sio, "connected", False):
            return
        now_m, now_s = time.monotonic(), time.time()
        with self._lock:
            n_tok = sum(1 for t in self._tokens if not _bogus_token(t))
        # (1) post-connect verification
        if self._verify_at is not None and now_m >= self._verify_at:
            self._verify_at = None
            if n_tok and self._in_ist_window(now_s, self.stream_watch_hours):
                seen = self._streams_since(self._connect_ms or 0)
                need = max(3, int(self.stream_min_frac * n_tok))
                if seen < need:
                    self._verify_attempt += 1
                    if self._verify_attempt <= 2:
                        n = self._rejoin_all()
                        self.stream_rejoins += 1
                        self._log("ws_rejoin", reason="streams did not resume after connect", seen=seen,
                                  need=need, attempt=self._verify_attempt, resubscribed=n)
                        self._verify_at = now_m + self.stream_verify_secs     # look again
                    else:
                        self.stream_rebuilds += 1
                        self._log("ws_streams_missing", seen=seen, need=need, action="rebuild socket")
                        self.request_reconnect(f"streams did not resume ({seen}/{n_tok})")
                    return
        # (2) the NIFTY spot
        key = self.spot_key
        if not key or not self._in_ist_window(now_s, self.spot_watch_hours):
            self._spot_attempt = 0
            return
        if self._verify_at is not None:                 # a verification is pending: let it act first
            return
        with self._lock:
            last = (self.latest.get(key) or {}).get("_recv_ms")
        now_ms = now_s * 1000.0
        silent = (now_ms - last) / 1000.0 if last else None
        if silent is None or silent < self.spot_silent_secs:
            self._spot_attempt = 0
            return
        if now_m - self.last_tick_mono > 3.0:           # everything is silent: the ws_silent path handles it
            return
        if now_m - self._spot_action_at < self.spot_silent_secs or now_m < self._spot_backoff_until:
            return
        recent = [t for t in self._spot_rebuilds if now_m - t < 600]
        if len(recent) >= 3:                            # rebuilding is not helping (an exchange-side halt?):
            self._spot_backoff_until = now_m + 300      # stop churning the socket for 5 minutes
            self._spot_rebuilds.clear()
            self._log("ws_streams_missing", silent_secs=round(silent, 1), action="backing off 300 s",
                      reason="NIFTY spot still silent after 3 socket rebuilds")
            return
        self._spot_action_at = now_m
        self._spot_attempt += 1
        if self._spot_attempt <= 2:
            n = self._rejoin_all()
            self.stream_rejoins += 1
            self._log("ws_rejoin", reason="NIFTY spot silent while other streams tick",
                      silent_secs=round(silent, 1), attempt=self._spot_attempt, resubscribed=n)
        else:
            self._spot_attempt = 0
            self.stream_rebuilds += 1
            self._spot_rebuilds.append(now_m)
            self._log("ws_streams_missing", silent_secs=round(silent, 1), action="rebuild socket",
                      reason="NIFTY spot silent")
            self.request_reconnect(f"NIFTY spot silent {silent:.0f}s")

    def _log_sub_error(self, msg: str) -> None:
        """Put a subscription error on the tape (177 on 12 Nov, none of them readable afterwards):
        one event per distinct message every 5 minutes, with the count since the last one."""
        now = time.monotonic()
        k = msg[:80]
        last, n = self._sub_err_seen.get(k, (0.0, 0))
        n += 1
        if now - last >= 300 or not last:
            self._log("sub_error", reason=msg, count=n)
            self._sub_err_seen[k] = (now, 0)
        else:
            self._sub_err_seen[k] = (last, n)
        if len(self._sub_err_seen) > 200:
            self._sub_err_seen.clear()

    def _absorb_sdk_tokens(self, h) -> None:
        """Anything the SDK joined on its own (a subscribe whose token we could not resolve) lives only
        in the handler's tokenlist -- copy it into ours before that handler is thrown away."""
        try:
            toks = set(getattr(h, "tokenlist", ()) or ())
        except Exception:
            return
        with self._lock:
            for t in toks:
                if isinstance(t, str) and t and not _bogus_token(t):
                    self._tokens.add(t)

    def _shutdown_handler(self) -> None:
        h = self._handler()
        if h is None:
            return
        self._absorb_sdk_tokens(h)
        sio = getattr(h, "sio", None)
        try:
            if sio is not None:
                if getattr(sio, "connected", False):
                    sio.disconnect()
                else:
                    abort = getattr(sio, "_reconnect_abort", None)
                    task = getattr(sio, "_reconnect_task", None)
                    if abort is not None:
                        abort.set()
                    if task is not None and getattr(task, "is_alive", lambda: False)():
                        task.join(timeout=15)
                    eio = getattr(sio, "eio", None)
                    if eio is not None and getattr(eio, "state", "disconnected") != "disconnected":
                        try:
                            eio.disconnect(abort=True)
                        except Exception:
                            pass
        except Exception:
            pass

    def _rejoin_all(self) -> int:
        """Re-join every remembered token on the current socket in ONE packet."""
        h = self._handler()
        with self._lock:
            toks = sorted(t for t in self._tokens if not _bogus_token(t))
        if h is None or not toks:
            return 0
        h.authentication = True
        try:
            h.watch(toks)
            self._last_keepalive = time.monotonic()
            return len(toks)
        except Exception as e:
            self._log("ws_rejoin_failed", reason=repr(e))
            return 0

    def reconnect_ws(self) -> tuple[bool, int]:
        """Synchronous reconnect (kept for callers that want to wait, e.g. the smoke test).
        The live feed uses request_reconnect() instead — never on the engine thread."""
        ok = self._do_reconnect("manual")
        return ok, (len(self._tokens) if ok else 0)

    def health(self) -> dict:
        connected, sdk_rec = self._sio_state()
        now = time.monotonic()
        return {
            "connected": bool(connected), "sdk_reconnecting": bool(sdk_rec),
            "reconnecting": bool(self._busy),
            "tick_age_s": round(now - self.last_tick_mono, 1),
            "ticks": self.ticks_received, "bad_ticks": self.bad_ticks,
            "tokens": len(self._tokens), "connects": self.ws_connects, "drops": self.ws_drops,
            "connect_errors": self.ws_connect_errors, "reconnects_ok": self.reconnects_ok,
            "reconnects_failed": self.reconnects_failed, "sub_errors": self.sub_errors,
            "keepalives": self.keepalives,
            # 2.1.1 intake gauges: backlog = packets waiting for the consumer; exch_lag_s = how far the
            # NIFTY cash stream is behind the exchange clock (the 'stuck ticker' number)
            "backlog": len(self._inbox), "backlog_max": self.inbox_max_seen,
            "inbox_dropped": self.inbox_dropped, "fast_parsed": self.fast_parsed,
            "exch_lag_s": self.tick_lag_s, "threads": threading.active_count(),
            "buffered": self.sockets_buffered,
            "stream_rejoins": self.stream_rejoins, "stream_rebuilds": self.stream_rebuilds,
            "last_error": self.last_connect_error or self.last_sub_error,
        }

    # ---- instrument-code resolution ----------------------------------------
    def resolve_codes(self, stocks: list[str], isec_override: dict | None = None) -> dict[str, str]:
        """Resolve heavyweight stock codes at runtime via get_names().

        Falls back to the operator override map, then to the symbol itself.  A
        wrong code subscribes to the wrong stock, so we never guess silently.
        """
        import logging
        import json
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        log = logging.getLogger("vajra.breeze")
        override = dict(isec_override or {})
        # 2.1.1: 21 sequential REST calls took 4-10 s of the restart; run them in parallel and keep a
        # cache of yesterday's answers (codes do not change day to day) for a call that fails/hangs.
        cache_path = Path("tapes/_cache/isec_codes.json")
        cached: dict = {}
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        except Exception:
            cached = {}

        def _one(sym):
            try:
                r = self._breeze.get_names(exchange_code="NSE", stock_code=sym)
                if isinstance(r, dict) and r.get("isec_stock_code"):
                    return sym, r["isec_stock_code"], None
                return sym, None, "no isec_stock_code in reply"
            except Exception as e:
                return sym, None, repr(e)

        results = {}
        if stocks:
            with ThreadPoolExecutor(max_workers=min(8, len(stocks))) as ex:
                futs = [ex.submit(_one, s) for s in stocks]
                deadline = time.monotonic() + 25.0
                for f in futs:
                    try:
                        sym, isec, err = f.result(timeout=max(0.1, deadline - time.monotonic()))
                        results[sym] = (isec, err)
                    except Exception:
                        pass
        for sym in stocks:
            isec, err = results.get(sym, (None, "timed out"))
            if err:
                log.warning("get_names failed for %s: %s", sym, err)
            resolved = isec or cached.get(sym) or override.get(sym) or sym
            source = ("get_names" if isec else ("cache" if cached.get(sym) else
                                                ("override" if override.get(sym) else "symbol_fallback")))
            self._stock_codes[sym] = resolved
            self._code_source[sym] = source
            if source not in ("get_names", "cache"):     # audit: a fallback may subscribe to the wrong stock
                log.warning("code resolution for %s used %s -> %s (verify if its bloc-bias matters)",
                            sym, source, resolved)
        try:
            good = {s: c for s, c in self._stock_codes.items() if self._code_source.get(s) in ("get_names", "cache")}
            if good:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({**cached, **good}, indent=1), encoding="utf-8")
        except Exception:
            pass
        return dict(self._stock_codes)

    def code_for(self, instrument: str) -> str:
        if instrument in INDEX_CODE:
            return INDEX_CODE[instrument]
        return self._stock_codes.get(instrument, instrument)

    def option_step(self, instrument: str, expiry_iso: str) -> float | None:
        """Authoritative near-ATM strike spacing for an instrument, from the LIVE option chain
        (get_option_chain_quotes). Stocks have wildly different steps (RELIANCE 10, HDFCBANK 5,
        BSE 50, ITC ~2.5) that cannot be guessed — query the real strikes and take the modal
        spacing of the strikes nearest the chain centre. Returns None on any failure."""
        try:
            r = self._breeze.get_option_chain_quotes(
                stock_code=self.code_for(instrument), exchange_code="NFO", product_type="options",
                expiry_date=ws_expiry(expiry_iso), right="call")
            rows = (r or {}).get("Success") or []
            ks = sorted({float(x["strike_price"]) for x in rows if x.get("strike_price")})
            if len(ks) < 3:
                return None
            mid = ks[len(ks) // 2]                              # chain centre ≈ ATM
            near = sorted(sorted(ks, key=lambda k: abs(k - mid))[:11])
            from collections import Counter
            diffs = Counter(round(near[i + 1] - near[i], 2) for i in range(len(near) - 1))
            step = diffs.most_common(1)[0][0]
            return step if step > 0 else None
        except Exception:
            return None

    # ---- subscriptions ------------------------------------------------------
    @staticmethod
    def _key(stock_code: str, kind: str, strike=None, right=None) -> str:
        return f"{stock_code}|{kind}|{strike}|{right}"

    def _token_for(self, **kw) -> str | None:
        """The broker's stream token for a subscription (a local dict lookup in the SDK, no REST)."""
        fn = getattr(self._breeze, "get_stock_token_value", None)
        if fn is None:
            return None
        try:
            r = fn(exchange_code=kw.get("exchange_code", ""), stock_code=kw.get("stock_code", ""),
                   product_type=kw.get("product_type", ""), expiry_date=kw.get("expiry_date", ""),
                   strike_price=kw.get("strike_price", ""), right=kw.get("right", ""),
                   get_exchange_quotes=kw.get("get_exchange_quotes", True),
                   get_market_depth=kw.get("get_market_depth", False))
        except Exception:
            return None
        if isinstance(r, tuple) and r and isinstance(r[0], str) and r[0]:
            if _bogus_token(r[0]):                      # the SDK spells "not in the script list" as "4.1!False"
                return _UNKNOWN_CONTRACT
            return r[0]
        return None

    def _sub(self, **kw) -> bool:
        """Subscribe one stream. Remembered as its TOKEN so a reconnect can re-join everything in one
        packet; the SDK's auth latch is cleared before every call."""
        self._subs.append(kw)                           # remembered (smoke test / audit)
        tok = self._token_for(**kw)
        if tok == _UNKNOWN_CONTRACT:
            # 3 Nov tape: 74 joins of the literal token "4.1!False" (sister strikes for an expiry the
            # script list does not carry). Joining garbage buys nothing; count it and stop.
            self.sub_errors += 1
            self.last_sub_error = f"unknown contract: {kw.get('stock_code')} {kw.get('product_type')} " \
                                  f"{kw.get('expiry_date')} {kw.get('strike_price')} {kw.get('right')}"[:160]
            return False
        if tok is not None:
            with self._lock:
                self._tokens.add(tok)
                self._token_kw[tok] = kw
        h = self._handler()
        if h is None:                                   # socket being rebuilt: the re-join covers it
            return False
        h.authentication = True
        before = set(getattr(h, "tokenlist", ()) or ())
        try:
            if tok is not None:
                r = self._breeze.subscribe_feeds(stock_token=tok)
            else:                                       # unknown contract: let the SDK resolve/complain
                r = self._breeze.subscribe_feeds(**kw)
        except Exception as e:
            r = repr(e)
        if tok is None:                                 # learn the token the SDK resolved itself, so a
            learned = set(getattr(h, "tokenlist", ()) or ()) - before      # rebuilt socket re-joins it
            if learned:
                with self._lock:
                    for t in learned:
                        if isinstance(t, str) and not _bogus_token(t):
                            self._tokens.add(t)
                            self._token_kw.setdefault(t, kw)
        if isinstance(r, dict):
            return True
        self.sub_errors += 1
        self.last_sub_error = str(r)[:160]
        self._log_sub_error(self.last_sub_error)
        return False

    # ---- subscription kwargs (one place; used by the single and the batched subscribe) ----------
    def kw_spot(self, instrument: str) -> dict:
        return dict(exchange_code="NSE", stock_code=self.code_for(instrument), product_type="cash",
                    get_exchange_quotes=True, get_market_depth=False)

    def kw_vix(self) -> dict:
        return dict(exchange_code="NSE", stock_code=INDIA_VIX_CODE, product_type="cash",
                    get_exchange_quotes=True, get_market_depth=False)

    def kw_future(self, instrument: str, expiry_iso: str) -> dict:
        return dict(exchange_code="NFO", stock_code=self.code_for(instrument), product_type="futures",
                    expiry_date=ws_expiry(expiry_iso), get_exchange_quotes=True, get_market_depth=False)

    def kw_option(self, instrument: str, expiry_iso: str, strike: int, right: str) -> dict:
        # get_market_depth=False: depth ticks carry the order book but NO `last` price, and they
        # were clobbering the quote tick in `latest` -> the strike's LTP vanished every other tick
        # (the ATM flickered to "—"). Exchange quotes already give us ltp/bid/ask/oi/vol.
        return dict(exchange_code="NFO", stock_code=self.code_for(instrument), product_type="options",
                    expiry_date=ws_expiry(expiry_iso), strike_price=str(strike),
                    right=RIGHT_TO_BREEZE[right], get_exchange_quotes=True, get_market_depth=False)

    def subscribe_spot(self, instrument: str):
        self._sub(**self.kw_spot(instrument))

    def subscribe_vix(self):
        self._sub(**self.kw_vix())

    def subscribe_future(self, instrument: str, expiry_iso: str):
        self._sub(**self.kw_future(instrument, expiry_iso))

    def subscribe_option(self, instrument: str, expiry_iso: str, strike: int, right: str):
        return self._sub(**self.kw_option(instrument, expiry_iso, strike, right))

    def subscribe_many(self, kws: list[dict]) -> int:
        """2.1.1: subscribe a BATCH in one socket packet. The 3 Nov restart made ~700 single
        subscribes on the engine thread (2-5 ms each -> the first rounds ran 1.5-3 s). Tokens are
        resolved locally, remembered, and joined with ONE `watch(list)`; contracts the SDK cannot
        resolve fall back to the single path (and unknown contracts are refused there).
        Returns the number of tokens joined in the batch."""
        toks: list[str] = []
        for kw in kws:
            self._subs.append(kw)
            tok = self._token_for(**kw)
            if tok == _UNKNOWN_CONTRACT:
                self.sub_errors += 1
                self.last_sub_error = f"unknown contract: {kw.get('stock_code')} {kw.get('product_type')} " \
                                      f"{kw.get('expiry_date')} {kw.get('strike_price')} {kw.get('right')}"[:160]
                continue
            if tok is None:                              # lookup unavailable: single path resolves it
                self._sub(**kw)
                continue
            with self._lock:
                self._tokens.add(tok)
                self._token_kw[tok] = kw
            toks.append(tok)
        h = self._handler()
        if h is None or not toks:                        # socket being rebuilt: the re-join covers it
            return 0
        h.authentication = True
        try:
            h.watch(toks)
            self._last_keepalive = time.monotonic()
            return len(toks)
        except Exception as e:
            self.sub_errors += 1
            self.last_sub_error = f"batch subscribe failed: {e!r}"[:160]
            self._log_sub_error(self.last_sub_error)
            return 0

    # ---- tick handling ------------------------------------------------------
    def _on_ticks(self, ticks):
        """SDK-shaped tick dict(s) (the SDK's on_ticks callback, tests, the fallback parser)."""
        if isinstance(ticks, dict):
            ticks = [ticks]
        self._ingest(list(ticks), int(time.time() * 1000))

    def _ingest(self, ticks: list, recv_ms: int) -> None:
        self.last_tick_mono = time.monotonic()
        rec = self.raw_recorder
        unmapped: list[str] = []
        lag_sample = None
        with self._lock:
            for t in ticks:
                try:
                    norm = self._normalize_tick(t, unmapped)
                except Exception:                       # one malformed tick must not kill the batch
                    self.bad_ticks += 1
                    continue
                if norm is None:
                    continue
                self.ticks_received += 1
                norm["_recv_ms"] = recv_ms
                ltt = t.get("ltt")
                if isinstance(ltt, (int, float)) and not isinstance(ltt, bool):
                    norm["_ltt_s"] = int(ltt)
                    if norm["kind"] == "cash" and norm["stock_code"] == "NIFTY":
                        lag_sample = recv_ms / 1000.0 - float(ltt)
                if rec is not None:
                    try:
                        rec.record(norm, {"ltt": self._ltt_text(ltt)} if ltt is not None else t, recv_ms)
                    except Exception:
                        pass
                k = norm["_key"]
                prev = self.latest.get(k)
                if prev is not None:            # carry forward last-known-good values when a partial /
                    for fld in ("ltp", "vwap", "volume", "oi", "bid", "ask"):   # price-less tick omits a
                        if norm.get(fld) is None and prev.get(fld) is not None:  # field (a 0 is a value)
                            norm[fld] = prev[fld]
                self.latest[k] = norm
        if lag_sample is not None:                      # exchange lag of the NIFTY cash stream (EMA)
            prev_lag = self.tick_lag_s
            self.tick_lag_s = lag_sample if prev_lag is None else round(0.8 * prev_lag + 0.2 * lag_sample, 2)
        for name in unmapped:                           # console I/O OUTSIDE the tick lock
            try:
                from .console import warn
                warn(f"[VAJRA WARNING] ws tick stock_name {name!r} maps to NO tracked instrument "
                     f"(WS_NAME_TO_INSTRUMENT) — it is silently absent from the universe. Capture its "
                     f"real name and add it to the map.")
            except Exception:
                pass

    def _normalize_tick(self, t: dict, unmapped: list | None = None) -> dict | None:
        stock = t.get("stock_code") or t.get("symbol")
        name = (t.get("stock_name") or "").strip()
        if not stock and not name:
            return None
        ltp = _f(t.get("last") or t.get("ltp") or t.get("close"))
        product = (t.get("product_type") or "").lower()
        strike = t.get("strike_price")
        right_raw = (t.get("right") or "").lower()
        right = "CE" if right_raw in ("call", "ce") else ("PE" if right_raw in ("put", "pe") else None)
        kind = "option" if product == "options" else ("future" if product == "futures" else "cash")

        # The ws gives stock_name, not stock_code — map it to OUR canonical code so the key matches.
        is_vix = ("VIX" in name.upper()) or stock == INDIA_VIX_CODE
        inst = WS_NAME_TO_INSTRUMENT.get(name) or WS_NAME_TO_INSTRUMENT.get(name.upper())
        if is_vix:
            key_code = INDIA_VIX_CODE
        elif inst:
            key_code = self.code_for(inst)                      # index -> e.g. NIFTY / CNXBAN / NIFFIN
        else:
            # An unmapped ws stock_name is keyed under name.upper(), which _build_snapshot's
            # code_for() lookup will never match -> the instrument silently vanishes from the
            # universe. Warn ONCE per name so the hole is visible (council: 21 stock names unverified).
            if name and name.upper() not in self._unmapped_warned:
                self._unmapped_warned.add(name.upper())
                if unmapped is not None:
                    unmapped.append(name)
            key_code = name.upper() or stock                    # stock best-effort key
        out = {
            "stock_code": key_code,
            "kind": kind,
            "strike": int(float(strike)) if strike not in (None, "") else None,
            "right": right,
            "ltp": normalize_vix(ltp, self.vix_fix) if is_vix else ltp,
            "vwap": _f(t.get("vwap") or t.get("avgPrice") or t.get("average_price") or t.get("ltq_vwap")),
            "volume": _i(_first(t, "volume", "ttq", "total_quantity_traded", "ltq")),
            "oi": _i(_first(t, "OI", "open_interest", "oi")),
            "bid": _f(_first(t, "bPrice", "best_bid_price", "bid")),
            "ask": _f(_first(t, "sPrice", "best_ask_price", "ask")),
        }
        out["_key"] = self._key(key_code, kind, out["strike"], out["right"])
        return out

    def snapshot_raw(self) -> dict[str, dict]:
        with self._lock:
            return dict(self.latest)

    # ---- orders -------------------------------------------------------------
    def place_option_order(self, side: str, strike: int, right: str, qty: int,
                           expiry_iso: str | None, product: str = "options",
                           order_type: str = "market", price: float = 0.0,
                           instrument: str = "NIFTY") -> str:
        """Place a NIFTY option order (BUY/SELL).  Reached only when LIVE_ORDERS."""
        if self._breeze is None:
            raise RuntimeError("Breeze not connected — cannot place a live order")
        resp = self._breeze.place_order(
            stock_code=self.code_for(instrument), exchange_code="NFO", product=product,
            action=side.lower(), order_type=order_type, stoploss="",
            quantity=str(int(qty)), price=str(price) if order_type == "limit" else "",
            validity="day", expiry_date=ws_expiry(expiry_iso) if expiry_iso else "",
            right=RIGHT_TO_BREEZE[right], strike_price=str(int(strike)),
        )
        try:
            oid = resp["Success"]["order_id"]
        except (KeyError, TypeError, IndexError):
            # NOT a fill — a failed broker order must never be returned as a phantom order_id.
            raise OrderPlacementError(f"Breeze order rejected/failed: {resp!r}")
        return str(oid)

    def disconnect(self):
        self._stop.set()
        self._reconnect_req.set()                       # wake the supervisor so it can exit
        self._inbox_ev.set()                            # and the tick consumer
        if self._breeze is not None:
            self._shutdown_handler()
            try:
                self._breeze.ws_disconnect()
            except Exception:
                pass
        self._connected = False


def _first(t: dict, *keys):
    """First key PRESENT in the tick (a present 0 is a value; 2.0's `a or b` chain treated 0 as
    missing and fell through to the next key / the previous tick)."""
    for k in keys:
        if k in t and t[k] not in (None, ""):
            return t[k]
    return None


def _f(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x) -> int | None:
    v = _f(x)
    return int(v) if v is not None else None


__all__ = ["BreezeWrapper", "BreezeCredentials", "OrderPlacementError", "BreezeConnectionError",
           "normalize_vix", "ws_expiry", "INDEX_CODE", "INDIA_VIX_CODE"]
