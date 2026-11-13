"""2.1 live-feed hardening (from the 21 Oct / 2 Nov 2026 tapes).

A fake Breeze SDK with a socket.io-shaped client proves:
  * the SDK's permanent `authentication=False` latch is cleared on every connect and before every
    subscribe (6,655 refused subscriptions on 2 Nov);
  * a reconnect runs on the supervisor thread, never blocks the caller, tears the old client down
    (its reconnect thread aborted) and re-joins EVERY stream token in one packet;
  * a 0 bid/ask is a value, not a missing field (2.0 carried the previous value forward);
  * the tape is written in closed parts and survives a hard kill (merge on the next open);
  * the engine's watchdog only REQUESTS a reconnect (non-blocking) and tapes the supervisor's log.
"""
from __future__ import annotations

import threading
import time
from datetime import date

import pyarrow.parquet as pq
import pytest

from vajra.breeze import BreezeWrapper, BreezeCredentials


# ---------------------------------------------------------------- a socket.io-shaped fake SDK
class _FakeSio:
    def __init__(self):
        self.connected = True
        self.handlers = {}
        self.emitted = []
        self._reconnect_task = None
        self._reconnect_abort = threading.Event()
        self.eio = type("E", (), {"state": "connected"})()
        self.disconnected = 0

    def on(self, event, handler):
        self.handlers[event] = handler

    def emit(self, event, data=None):
        if not self.connected:
            raise RuntimeError("not connected")
        self.emitted.append((event, data))

    def disconnect(self):
        self.disconnected += 1
        self.connected = False


class _FakeHandler:
    """Mirror of breeze_connect.SocketEventBreeze's surface."""
    def __init__(self):
        self.sio = _FakeSio()
        self.tokenlist = set()
        self.authentication = True
        self.rewatched = 0

    def rewatch(self):
        self.rewatched += 1
        if self.tokenlist:
            self.sio.emit("join", sorted(self.tokenlist))

    def watch(self, data):
        if not self.sio.connected:
            raise Exception("Failed to connect to live stream")
        if isinstance(data, list):
            self.tokenlist.update(data)
        else:
            self.tokenlist.add(data)
        self.sio.emit("join", data)
        self.sio.on("connect", self.rewatch)

    def my_connect_error(self, *a):
        self.authentication = False


class _FakeSDK:
    def __init__(self):
        self.sio_rate_refresh_handler = None
        self.on_ticks = None
        self.ws_connects = 0
        self.fail_next_connect = False
        self.subscribed = []

    def generate_session(self, api_secret, session_token):
        pass

    def ws_connect(self):
        if self.fail_next_connect:
            self.fail_next_connect = False
            raise Exception("Failed to connect to live stream")
        self.ws_connects += 1
        if self.sio_rate_refresh_handler is None:
            self.sio_rate_refresh_handler = _FakeHandler()

    def ws_disconnect(self):
        self.sio_rate_refresh_handler = None

    # mirrors breeze_connect 1.0.69: `interval` is first assigned INSIDE subscribe_feeds, so a lookup
    # before the first subscribe raises (and the SDK returns the exception object instead of a tuple);
    # a contract missing from the script list is spelled "<exch>1!False".
    unknown: set = set()

    def get_stock_token_value(self, exchange_code="", stock_code="", product_type="", expiry_date="",
                              strike_price="", right="", get_exchange_quotes=True, get_market_depth=True):
        try:
            _ = self.interval
            if (stock_code, strike_price) in self.unknown:
                return "4.1!False", False
            tok = f"4.1!{stock_code}-{product_type}-{expiry_date}-{strike_price}-{right}"
            return tok, False
        except Exception as e:
            return e

    def subscribe_feeds(self, stock_token="", **kw):
        self.interval = ""
        h = self.sio_rate_refresh_handler
        if h is not None and h.authentication is False:
            return "Exception while subscribing to feeds Could not authenticate credentials"
        if stock_token == "":                            # the SDK resolves the contract itself
            stock_token, _ = self.get_stock_token_value(**kw)
        self.subscribed.append(stock_token)
        try:
            h.watch(stock_token)
        except Exception as e:
            return f"Exception while subscribing to feeds {e}"
        return {"message": f"Stock {stock_token} subscribed successfully"}


def _wrapper() -> tuple[BreezeWrapper, _FakeSDK]:
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"), reconnect_max_retries=2, reconnect_backoff_secs=0.0,
                       sdk_reconnect_grace_secs=0.2)
    sdk = _FakeSDK()
    bw._breeze = sdk
    bw._session_ok = True
    sdk.on_ticks = bw._on_ticks
    bw.connect()
    return bw, sdk


def test_auth_latch_is_cleared_before_every_subscribe_and_on_connect():
    bw, sdk = _wrapper()
    h = sdk.sio_rate_refresh_handler
    h.authentication = False                          # what one connect_error did in 2.0, for the whole day
    assert bw.subscribe_option("NIFTY", "2026-11-03", 23350, "CE") is True
    assert h.authentication is True
    assert sdk.subscribed and sdk.subscribed[-1].endswith("23350-call")
    # a connect_error routed to OUR handler never sets the latch
    h.my_connect_error("rejected")
    assert h.authentication is True and bw.ws_connect_errors == 1
    # a (re)connect clears it too and re-joins the tokens through the SDK's own rewatch path
    h.authentication = False
    h.sio.handlers["connect"]()
    assert h.authentication is True and h.rewatched == 1
    bw.disconnect()


def test_reconnect_runs_off_thread_and_rejoins_every_token_in_one_packet():
    bw, sdk = _wrapper()
    bw.subscribe_spot("NIFTY"); bw.subscribe_future("NIFTY", "2026-11-24")
    for k in (23300, 23350, 23400):
        for r in ("CE", "PE"):
            bw.subscribe_option("NIFTY", "2026-11-03", k, r)
    old = sdk.sio_rate_refresh_handler
    old.sio.connected = False                          # the socket died silently
    t0 = time.perf_counter()
    assert bw.request_reconnect("test") is True
    assert time.perf_counter() - t0 < 0.05            # the caller never waited
    deadline = time.time() + 5
    while time.time() < deadline and bw.reconnects_ok == 0 and bw.reconnects_failed == 0:
        time.sleep(0.02)
    assert bw.reconnects_ok == 1
    new = sdk.sio_rate_refresh_handler
    assert new is not old and old.sio._reconnect_abort.is_set()
    joins = [d for e, d in new.sio.emitted if e == "join"]
    assert len(joins) == 1 and isinstance(joins[0], list) and len(joins[0]) == 8   # ONE packet, all 8 tokens
    assert set(joins[0]) == bw._tokens
    stages = [s for _, s, _ in bw.drain_events()]
    assert "ws_reconnect" in stages
    bw.disconnect()


def test_failed_reconnect_is_reported_not_raised():
    bw, sdk = _wrapper()
    sdk.sio_rate_refresh_handler.sio.connected = False
    sdk.fail_next_connect = True
    bw.request_reconnect("test")
    deadline = time.time() + 5
    while time.time() < deadline and bw.reconnects_failed == 0:
        time.sleep(0.02)
    assert bw.reconnects_failed == 1 and bw.connected is False
    ev = {s: p for _, s, p in bw.drain_events()}
    assert ev.get("ws_reconnect", {}).get("ok") is False
    bw.disconnect()


def test_zero_bid_is_a_value_not_a_gap():
    bw, sdk = _wrapper()
    tick = {"stock_name": "NIFTY 50", "symbol": "4.1!1", "last": 23300.5, "bPrice": 23300, "sPrice": 23301,
            "ttq": 10, "product_type": ""}
    bw._on_ticks(tick)
    bw._on_ticks({**tick, "last": 23301.0, "bPrice": 0, "sPrice": 0, "ttq": 0})
    q = bw.snapshot_raw()["NIFTY|cash|None|None"]
    assert q["ltp"] == 23301.0 and q["bid"] == 0.0 and q["ask"] == 0.0 and q["volume"] == 0
    # a tick WITHOUT the field keeps the last-known value
    bw._on_ticks({"stock_name": "NIFTY 50", "symbol": "4.1!1", "last": 23302.0, "product_type": ""})
    q = bw.snapshot_raw()["NIFTY|cash|None|None"]
    assert q["ltp"] == 23302.0 and q["bid"] == 0.0 and q["_recv_ms"] > 0
    assert bw.ticks_received == 3
    bw._on_ticks({"stock_name": "NIFTY 50", "symbol": "4.1!1", "last": "garbage"})     # must not raise
    bw.disconnect()


def test_feed_watchdog_requests_not_blocks(monkeypatch):
    """The engine-thread watchdog asks the supervisor and copies its log into the tape."""
    from vajra.config import load_config
    from vajra.feed import LiveBreezeSource
    from pathlib import Path
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "vajra_config.yaml"))
    bw, sdk = _wrapper()
    events = []
    src = LiveBreezeSource(bw, cfg, "2026-11-03", "IMPLIED", future_expiry_iso="2026-11-24",
                           event_sink=lambda t, p, ts_ms=None: events.append((t, p)))
    bw.last_tick_mono = time.monotonic() - 60                 # a minute of silence
    sdk.sio_rate_refresh_handler.sio.connected = False
    from datetime import datetime, timezone, timedelta
    ts = int(datetime(2026, 11, 3, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp() * 1000)
    t0 = time.perf_counter()
    src._ws_watchdog(ts)
    assert time.perf_counter() - t0 < 1.5        # never blocks (2.0 blocked 3-5.5 s); slack for a busy laptop
    stages = [p.get("stage") for t, p in events]
    assert "ws_silent" in stages and "ws_reconnect_requested" in stages
    deadline = time.time() + 5
    while time.time() < deadline and bw.reconnects_ok == 0:
        time.sleep(0.02)
    src._ws_watchdog(ts + 250)                                # the supervisor's log reaches the tape
    stages = [p.get("stage") for t, p in events]
    assert "ws_reconnect" in stages and "ws_resumed" in stages
    h = src.health()
    assert h["connected"] is True and h["reconnects_ok"] == 1
    bw.disconnect()


# ---------------------------------------------------------------- crash-safe tapes
def _rows(n, ts0=1_000):
    from vajra import schema as S
    cols = S.TAPE_SCHEMAS["decision_tape"].names
    return [{c: None for c in cols} | {"ts": ts0 + i, "decision": "HOLD"} for i in range(n)]


def test_parts_rotate_and_survive_a_hard_kill(tmp_path):
    from vajra.tape import TapeWriter, part_paths, parquet_sources, iter_parquet_rows
    d = date(2026, 11, 3)
    tw = TapeWriter(tmp_path, d, rotate_secs=0.05, flush_threshold=10)
    for r in _rows(60):
        tw.append("decision_tape", r)
        time.sleep(0.002)
    tw.flush()
    for k in range(3):                       # a part older than rotate_secs is closed after the next batch
        time.sleep(0.12)                     # and the batch after that opens a new one -> >= 2 parts
        for r in _rows(10, ts0=1_060 + 10 * k):
            tw.append("decision_tape", r)
        tw.flush()
    part = tmp_path / d.isoformat()
    assert len(part_paths(part, "decision_tape")) >= 2       # rotated at least once while running
    # simulate a hard kill: forget the writer without close() -> parts stay, main does not exist
    assert not (part / "decision_tape.parquet").exists()
    for w in tw._writers.values():
        w.close()                                              # the OS closes handles on a kill; parts are whole
    tw._writers.clear()
    srcs = parquet_sources(part, "decision_tape")              # a reader merges the leftovers
    assert len(srcs) == 1 and srcs[0].name == "decision_tape.parquet"
    assert pq.ParquetFile(str(srcs[0])).metadata.num_rows == 90
    assert not part_paths(part, "decision_tape")
    assert [r["ts"] for r in iter_parquet_rows(part, "decision_tape")] == list(range(1_000, 1_090))


def test_clean_close_merges_parts_and_restart_keeps_the_earlier_session(tmp_path):
    from vajra.tape import TapeWriter, part_paths, parquet_sources
    d = date(2026, 11, 3)
    tw = TapeWriter(tmp_path, d, rotate_secs=0.01, flush_threshold=5)
    for r in _rows(23):
        tw.append("decision_tape", r); time.sleep(0.003)
    tw.close()
    part = tmp_path / d.isoformat()
    assert not part_paths(part, "decision_tape")
    assert pq.ParquetFile(str(part / "decision_tape.parquet")).metadata.num_rows == 23
    tw2 = TapeWriter(tmp_path, d, rotate_secs=60)              # same-day restart
    for r in _rows(7, ts0=5_000):
        tw2.append("decision_tape", r)
    tw2.close()
    srcs = parquet_sources(part, "decision_tape")
    assert len(srcs) == 2 and srcs[0].name.endswith(".bak") and srcs[1].name == "decision_tape.parquet"
    assert sum(pq.ParquetFile(str(s)).metadata.num_rows for s in srcs) == 30


def test_big_tapes_have_no_jsonl_sidecar_by_default_small_ones_do(tmp_path):
    from vajra.tape import TapeWriter
    from vajra import schema as S
    d = date(2026, 11, 3)
    tw = TapeWriter(tmp_path, d)
    tcols = S.TAPE_SCHEMAS["tick_tape"].names
    tw.append("tick_tape", {c: None for c in tcols} | {"ts": 1, "instrument": "NIFTY", "strikes": []})
    tw.append("decision_tape", _rows(1)[0])
    tw.close()
    part = tmp_path / d.isoformat()
    assert not (part / "tick_tape.jsonl").exists() and (part / "decision_tape.jsonl").exists()
    assert pq.ParquetFile(str(part / "tick_tape.parquet")).metadata.num_rows == 1


def test_rebuild_from_jsonl_repairs_a_footerless_2_0_file(tmp_path):
    import json
    from vajra.tape import rebuild_from_jsonl
    part = tmp_path / "2026-11-02"; part.mkdir()
    rows = _rows(12)
    (part / "decision_tape.jsonl").write_text("\n".join(json.dumps(r | {"schema_version": "2.0.0"}) for r in rows) + "\n{torn", encoding="utf-8")
    (part / "decision_tape.parquet").write_bytes(b"PAR1" + b"\x00" * 100)    # no footer
    assert rebuild_from_jsonl(part, "decision_tape") == 12
    assert pq.ParquetFile(str(part / "decision_tape.parquet")).metadata.num_rows == 12
    assert list(part.glob("decision_tape.parquet.*.unreadable.bak"))


def test_raw_tick_recorder_parts_merge_at_close(tmp_path):
    from vajra.rawtape import RawTickRecorder
    rr = RawTickRecorder(tmp_path, rotate_secs=0.01, max_batch_secs=0.01)
    for i in range(50):
        rr.record({"_key": "NIFTY|cash|None|None", "stock_code": "NIFTY", "kind": "cash", "ltp": 23000.0 + i},
                  {"ltt": "10:00:00"}, 1000 + i)
        time.sleep(0.002)
    rr.close()
    t = pq.read_table(tmp_path / "raw_ticks.parquet")
    assert t.num_rows == 50 and not list(tmp_path.glob("raw_ticks.part-*.parquet"))


def test_console_output_never_blocks(monkeypatch):
    import queue
    from vajra import console
    monkeypatch.setattr(console, "_started", True)          # no drain thread: the queue fills up
    monkeypatch.setattr(console, "_q", queue.Queue(maxsize=3))
    t0 = time.perf_counter()
    for i in range(500):
        console.say(f"line {i}")
    assert time.perf_counter() - t0 < 0.5 and console.dropped >= 497


# ---------------------------------------------------------------- end to end: rounds keep flowing across a drop
def test_live_rounds_keep_flowing_across_a_socket_drop(monkeypatch):
    """Ticks are pushed from a 'broker' thread into the wrapper while LiveBreezeSource.rounds() runs on the
    engine thread. The socket dies mid-way: the engine must not stall, must keep producing rounds (with
    the last-known quotes), must request ONE reconnect, and must pick the new ticks up afterwards."""
    from pathlib import Path
    from vajra.config import load_config
    from vajra import feed as F
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "vajra_config.yaml"))
    cfg.raw["universe"] = {"indices": ["NIFTY"], "stocks": []}
    cfg.raw.setdefault("feed", {}).update({"poll_interval_secs": 0.01, "ws_silent_secs": 0.3,
                                           "ws_reconnect_every_secs": 0.2})
    cfg.raw.setdefault("expiry", {})["session_close"] = "23:59"
    monkeypatch.setattr(F, "hhmm_reached", lambda ts, hhmm: hhmm in ("09:15", "09:16"))   # always market hours
    bw, sdk = _wrapper()
    events = []
    src = F.LiveBreezeSource(bw, cfg, "2026-11-03", "IMPLIED", future_expiry_iso="2026-11-24",
                             event_sink=lambda t, p, ts_ms=None: events.append((t, p)))
    stop = threading.Event()
    spot = [23300.0]

    def broker():
        i = 0
        while not stop.is_set():
            if sdk.sio_rate_refresh_handler is not None and sdk.sio_rate_refresh_handler.sio.connected:
                i += 1
                s = spot[0] + (i % 5) * 0.5
                bw._on_ticks([{"stock_name": "NIFTY 50", "symbol": "4.1!1", "last": s, "product_type": ""}] + [
                    {"stock_name": "NIFTY 50", "symbol": "4.1!x", "product_type": "Options", "strike_price": str(k),
                     "right": r, "last": 100.0 + (k - 23300) / 10 * (1 if r == "Call" else -1) + 50, "ttq": i,
                     "OI": 1000, "bPrice": 99.0, "sPrice": 101.0, "expiry_date": "03-Nov-2026"}
                    for k in range(23000, 23650, 50) for r in ("Call", "Put")])
            time.sleep(0.005)
    th = threading.Thread(target=broker, daemon=True); th.start()
    gen = src.rounds()
    rounds = []
    try:
        for _ in range(40):                                       # ~0.4 s of healthy rounds
            rounds.append(next(gen))
        assert sum(1 for ts, b in rounds if b.get("NIFTY")) >= 30
        assert len(rounds[-1][1]["NIFTY"].strikes) >= 20
        # --- the socket dies silently: no more ticks, sio reports disconnected
        sdk.sio_rate_refresh_handler.sio.connected = False
        t0 = time.perf_counter(); n_before = len(rounds)
        while time.perf_counter() - t0 < 1.5:
            ts, b = next(gen)                                      # the engine thread never blocks
            rounds.append((ts, b))
            assert b.get("NIFTY") is not None                      # last-known quotes still served
        gaps = [rounds[i + 1][0] - rounds[i][0] for i in range(n_before, len(rounds) - 1)]
        assert max(gaps) < 300, f"a round stalled {max(gaps)} ms during the drop/reconnect"
        stages = [p.get("stage") for t, p in events]
        assert "ws_silent" in stages and "ws_reconnect_requested" in stages
        assert bw.reconnects_ok >= 1 and sdk.sio_rate_refresh_handler.sio.connected
        joins = [d for e, d in sdk.sio_rate_refresh_handler.sio.emitted if e == "join"]
        assert joins and isinstance(joins[0], list) and len(joins[0]) >= 26   # every token re-joined at once
        # --- ticks flow again on the new socket and reach the rounds
        spot[0] = 23400.0
        for _ in range(60):
            rounds.append(next(gen))
        assert abs(rounds[-1][1]["NIFTY"].spot - 23400.0) < 3
        stages = [p.get("stage") for t, p in events]
        assert "ws_resumed" in stages
    finally:
        stop.set(); src.stop(); bw.disconnect()


# ---------------------------------------------------------------- 2.1.1 (3 Nov 2026: spot frozen 36 min)
def test_first_subscription_token_is_learned_and_rejoined_on_a_new_socket():
    """The real SDK's token lookup raises before the first subscribe (no `interval` yet). The FIRST
    subscription is the NIFTY spot: it must still be remembered so a rebuilt socket re-joins it."""
    bw, sdk = _wrapper()
    assert bw._token_for(exchange_code="NSE", stock_code="NIFTY", product_type="cash") is None   # SDK quirk
    bw.subscribe_spot("NIFTY")                        # resolved by the SDK itself (kw path)
    bw.subscribe_option("NIFTY", "2026-11-03", 23600, "CE")
    spot_tok = [t for t in sdk.subscribed if "NIFTY-cash" in t][0]
    assert spot_tok in bw._tokens                     # learned from the handler's tokenlist
    old = sdk.sio_rate_refresh_handler
    old.sio.connected = False
    assert bw.request_reconnect("test")
    deadline = time.time() + 5
    while time.time() < deadline and bw.reconnects_ok == 0 and bw.reconnects_failed == 0:
        time.sleep(0.02)
    new = sdk.sio_rate_refresh_handler
    joins = [d for e, d in new.sio.emitted if e == "join"]
    assert joins and spot_tok in joins[0] and len(joins[0]) == 2
    bw.disconnect()


def test_connect_primes_the_sdk_interval_so_the_first_lookup_works(monkeypatch):
    import types, sys
    sdk = _FakeSDK()
    fake_mod = types.ModuleType("breeze_connect")
    fake_mod.BreezeConnect = lambda api_key: sdk
    monkeypatch.setitem(sys.modules, "breeze_connect", fake_mod)
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"), reconnect_max_retries=1, reconnect_backoff_secs=0.0)
    bw.connect()
    assert sdk.interval == ""
    tok = bw._token_for(exchange_code="NSE", stock_code="NIFTY", product_type="cash")
    assert tok and tok.startswith("4.1!NIFTY")
    bw.disconnect()


def test_unknown_contracts_are_never_joined():
    bw, sdk = _wrapper()
    bw.subscribe_option("NIFTY", "2026-11-03", 23600, "CE")        # primes `interval`
    sdk.unknown.add(("CNXBAN", "53000"))
    assert bw.subscribe_option("BANKNIFTY", "2026-11-03", 53000, "CE") is False
    assert bw.sub_errors == 1 and "unknown contract" in bw.last_sub_error
    assert not any("False" in t for t in sdk.subscribed)
    assert not any(t.endswith("!False") for t in bw._tokens)
    bw.disconnect()


def test_keepalive_rejoins_one_token_when_idle():
    bw, sdk = _wrapper()
    bw.keepalive_secs = 0.1
    bw.subscribe_option("NIFTY", "2026-11-03", 23600, "CE")
    h = sdk.sio_rate_refresh_handler
    n0 = len([1 for e, d in h.sio.emitted if e == "join"])
    time.sleep(0.9)
    joins = [d for e, d in h.sio.emitted if e == "join"][n0:]
    assert bw.keepalives >= 3 and all(isinstance(d, str) for d in joins)   # a single, already-joined token
    assert bw.health()["keepalives"] == bw.keepalives
    bw.disconnect()
