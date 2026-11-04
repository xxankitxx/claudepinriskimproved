"""2.1.1 — the tick intake (3 Nov 2026: NIFTY spot 15-25 s behind the exchange all day).

  * the fast parser produces the same normalised tick as the SDK's parse_data + reverse lookup;
  * engine.io 'message' packets are queued and dispatched IN ORDER by one consumer thread (no thread per
    packet), the backlog / drop / exchange-lag gauges are live, and non-message events keep the library path;
  * garbage payloads are counted, never raised; the consumer survives a handler exception.
"""
from __future__ import annotations

import threading
import time

import pytest

from vajra.breeze import BreezeWrapper, BreezeCredentials


def _sdk_for_parse():
    """A BreezeConnect without a session: only the parsers and the token reverse map are used. The
    SDK's import-time security-master download is served from vajra.sdk_boot's daily cache."""
    try:
        from vajra.sdk_boot import import_sdk
        BreezeConnect = import_sdk().BreezeConnect
    except Exception as e:                      # no SDK / no network and no cache
        pytest.skip(f"breeze_connect unavailable: {e!r}")
    b = BreezeConnect.__new__(BreezeConnect)
    b.token_script_dict_list = [{}, {}, {}, {}, {}, {}]
    b.token_script_dict_list[1] = {"NIFTY 50": ["NIFTY", "NIFTY 50"], "13": ["RELIND", "RELIANCE INDUSTRIES LIMITED"]}
    b.token_script_dict_list[4] = {"35054": ["OPT-NIFTY-03-Nov-2026-23600-CE", "NIFTY"],
                                   "61471": ["FUT-NIFTY-23-Nov-2026", "NIFTY"]}
    b.on_ticks = None
    b.on_ticks2 = None
    b.interval = ""
    return b


def _wrapper(sdk):
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    bw._breeze = sdk
    bw._stock_codes = {"NIFTY": "NIFTY", "RELIANCE": "RELIND"}
    return bw


def _fonse():
    return ["4.1!35054", 100.0, 101.5, 110.0, 90.0, 1.2, 99.5, 50, 100.5, 75, 25, 100.2, 12345, 10, 999, 500, 400,
            1e6, "up", 50.0, 150.0, int(time.time()), 98.0]


def _cash():
    return ["4.1!NIFTY 50", 23700.0, 23810.85, 23850.0, 23650.0, 0.4, 0, 0, 0, 0, 0, 23790.0, 0, 0, 0, 0, "", 0, 0,
            int(time.time()), 23720.0]


def test_fast_parse_matches_the_sdk_parser():
    sdk = _sdk_for_parse()
    bw = _wrapper(sdk)
    for raw in (_fonse(), _cash()):
        ref = sdk.parse_data(list(raw))
        ref.update(sdk.get_data_from_stock_token_value(raw[0]))
        want = bw._normalize_tick(ref)
        got = bw._normalize_tick(bw._parse_fast(list(raw)))
        assert got == want, (got, want)
    assert bw._parse_fast({"not": "a list"}) is None
    assert bw._parse_fast(["4.2!35054", int(time.time()), []]) is None        # market depth -> SDK path


def test_our_token_map_beats_the_sdk_reverse_lookup():
    sdk = _sdk_for_parse()
    bw = _wrapper(sdk)
    bw._token_kw["4.1!99999"] = {"exchange_code": "NFO", "stock_code": "NIFTY", "product_type": "options",
                                 "strike_price": "23650", "right": "put", "expiry_date": "03-Nov-2026"}
    raw = _fonse(); raw[0] = "4.1!99999"
    norm = bw._normalize_tick(bw._parse_fast(raw))
    assert norm["_key"] == "NIFTY|option|23650|PE" and norm["ltp"] == 101.5 and norm["oi"] == 12345


class _FakeEio:
    def __init__(self):
        self.calls = []

    def _trigger_event(self, event, *args, **kwargs):
        self.calls.append((event, args))
        return "lib"


class _FakeSio:
    def __init__(self, handler):
        self.eio = _FakeEio()
        self.handler = handler
        self.connected = True
        self.handled = []

    def on(self, *a):
        pass

    def _handle_eio_message(self, data):
        self.handled.append(data)
        if data == "boom":
            raise RuntimeError("bad packet")
        self.handler.on_message(data)


class _FakeHandler:
    def __init__(self):
        self.sio = _FakeSio(self)
        self.tokenlist = set()
        self.authentication = True

    def rewatch(self):
        pass

    def on_message(self, data):               # the SDK's; replaced by the hook
        raise AssertionError("SDK on_message must not run")


def test_messages_are_queued_and_dispatched_in_order_by_one_thread():
    sdk = _sdk_for_parse()
    bw = _wrapper(sdk)
    h = _FakeHandler()
    sdk.sio_rate_refresh_handler = h
    bw._start_consumer()
    bw._hook_handler()
    assert h.on_message == bw._fast_on_message
    eio = h.sio.eio
    seen_threads = set()
    orig = bw._fast_on_message

    def spy(data):
        seen_threads.add(threading.get_ident())
        orig(data)
    h.on_message = spy
    # 300 FONSE ticks with increasing ltp, from several producer threads -> applied in queue order
    def producer(base):
        for i in range(100):
            raw = _fonse(); raw[2] = base + i
            eio._trigger_event("message", raw)
    ts = [threading.Thread(target=producer, args=(b,)) for b in (1000.0, 2000.0, 3000.0)]
    for t in ts: t.start()
    for t in ts: t.join()
    deadline = time.time() + 5
    while time.time() < deadline and bw.consumed < 300:
        time.sleep(0.01)
    assert bw.consumed == 300 and bw.ticks_received == 300 and bw.fast_parsed == 300
    assert len(seen_threads) == 1 and threading.get_ident() not in seen_threads   # one consumer thread
    assert h.sio.handled == [d for d in h.sio.handled]                           # dispatched in receive order
    assert bw.latest["NIFTY|option|23600|CE"]["ltp"] == h.sio.handled[-1][2]
    # non-message events still go to the library
    assert eio._trigger_event("disconnect", "x") == "lib" and eio.calls[-1][0] == "disconnect"
    hz = bw.health()
    assert hz["backlog"] == 0 and hz["backlog_max"] >= 1 and hz["threads"] >= 2 and "exch_lag_s" in hz
    bw.disconnect()


def test_garbage_and_handler_exceptions_are_counted_not_fatal():
    sdk = _sdk_for_parse()
    bw = _wrapper(sdk)
    h = _FakeHandler()
    sdk.sio_rate_refresh_handler = h
    bw._start_consumer()
    bw._hook_handler()
    eio = h.sio.eio
    eio._trigger_event("message", "boom")                       # _handle_eio_message raises
    eio._trigger_event("message", {"weird": 1})                 # not a list: SDK parser -> counted
    eio._trigger_event("message", ["no-bang", 1, 2])
    raw = _cash()
    eio._trigger_event("message", raw)                          # a good one after the garbage
    deadline = time.time() + 5
    while time.time() < deadline and bw.consumed < 4:
        time.sleep(0.01)
    assert bw.consumed == 4 and bw._consumer_alive()
    assert bw.ticks_received == 1 and bw.latest["NIFTY|cash|None|None"]["ltp"] == 23810.85
    assert bw.bad_ticks >= 2
    assert bw.tick_lag_s is not None and bw.tick_lag_s < 5        # NIFTY cash exchange lag is measured
    bw.disconnect()


def test_inbox_overflow_drops_oldest_and_counts():
    sdk = _sdk_for_parse()
    bw = _wrapper(sdk)
    h = _FakeHandler()
    sdk.sio_rate_refresh_handler = h
    bw._consumer = threading.Thread(target=lambda: time.sleep(2), daemon=True)   # "alive" but not draining
    bw._consumer.start()
    bw._hook_handler()
    from collections import deque
    bw._inbox = deque(maxlen=5)
    h.sio.eio = _FakeEio(); h.sio.eio._vajra_hooked = False
    bw._hook_eio(h)
    for i in range(8):
        h.sio.eio._trigger_event("message", ["4.1!x", i])
    assert len(bw._inbox) == 5 and bw.inbox_dropped == 3
    assert bw._inbox[0][2][1] == 3                                 # oldest evicted first
