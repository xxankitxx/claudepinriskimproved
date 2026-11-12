"""2.2.2 -- the 12 Nov live day: after 3 of the day's reconnects the server ignored the re-join (0-6 streams
ticked in the next 20 s; a good reconnect gets 300-400 within 5 s) and the NIFTY spot stayed dead for 14, 27
and 10 minutes, because a few near-ATM options came back through the stale-quote re-subscribe and the socket
never looked silent.

  * every connect is verified a few seconds later; too few streams -> re-join from OUR clean token list,
    once more, then rebuild the socket;
  * the NIFTY spot silent while other streams tick (market hours, before the index freeze) -> same;
  * garbage tokens are removed from the SDK's own re-join list on every connect.
"""
from __future__ import annotations

import sys

from datetime import datetime, timezone, timedelta

import pytest

import vajra.breeze as B
from vajra.breeze import BreezeWrapper, BreezeCredentials

IST = timezone(timedelta(hours=5, minutes=30))
THU_1100 = datetime(2026, 11, 12, 11, 0, 0, tzinfo=IST).timestamp()      # a Thursday, market hours
SPOT = "NIFTY|cash|None|None"


class _Clock:
    def __init__(self, wall):
        self.wall, self.mono = wall, 1000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, s):
        self.advance(s)

    def advance(self, s):
        self.wall += s
        self.mono += s

    def __getattr__(self, k):                     # anything else from the real module
        import time as _t
        return getattr(_t, k)


class _Sio:
    connected = True


class _Handler:
    def __init__(self):
        self.sio = _Sio()
        self.tokenlist = set()
        self.watched = []

    def watch(self, toks):
        self.watched.append(list(toks))
        self.tokenlist |= set(toks)


class _SDK:
    def __init__(self, h):
        self.sio_rate_refresh_handler = h


@pytest.fixture
def rig(monkeypatch):
    clk = _Clock(THU_1100)
    monkeypatch.setattr(B, "time", clk)
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    h = _Handler()
    bw._breeze = _SDK(h)
    toks = [f"4.1!{n}" for n in range(100)] + ["4.1!NIFTY 50"]
    bw._tokens = set(toks)
    bw.set_spot_watch("NIFTY", "15:15")
    return bw, h, clk


def _tick(bw, clk, keys):
    now_ms = int(clk.time() * 1000)
    for k in keys:
        bw.latest[k] = {"_recv_ms": now_ms}
    bw.last_tick_mono = clk.monotonic()


def _events(bw):
    return [(stage, p) for _t, stage, p in bw.drain_events()]


def test_a_connect_whose_join_was_ignored_is_rejoined_then_rebuilt(rig):
    bw, h, clk = rig
    bw._schedule_verify()
    clk.advance(11)
    bw._stream_watch()                                      # nothing ticked since the connect
    assert len(h.watched) == 1 and len(h.watched[0]) == 101 and bw.stream_rejoins == 1
    ev = _events(bw)
    assert ev[-1][0] == "ws_rejoin" and ev[-1][1]["seen"] == 0 and ev[-1][1]["attempt"] == 1
    clk.advance(11)
    bw._stream_watch()                                      # still nothing: second re-join
    assert len(h.watched) == 2
    clk.advance(11)
    bw._stream_watch()                                      # third strike: rebuild the socket
    assert bw._reconnect_req.is_set() and "streams did not resume" in bw._reconnect_reason
    assert bw.stream_rebuilds == 1 and _events(bw)[-1][0] == "ws_streams_missing"


def test_a_healthy_reconnect_is_left_alone(rig):
    bw, h, clk = rig
    bw._schedule_verify()
    clk.advance(8)                                          # a slow but healthy reconnect
    _tick(bw, clk, [f"S{n}|option|1|CE" for n in range(40)] + [SPOT])
    clk.advance(3)
    bw._stream_watch()
    assert h.watched == [] and bw._verify_at is None and not bw._reconnect_req.is_set()


def test_old_ticks_do_not_count_as_resumed_streams(rig):
    bw, h, clk = rig
    _tick(bw, clk, [f"S{n}|option|1|CE" for n in range(80)])      # before the connect
    clk.advance(1)
    bw._schedule_verify()
    clk.advance(11)
    bw._stream_watch()
    assert len(h.watched) == 1


def test_nifty_spot_silent_while_options_tick_is_rejoined_then_rebuilt(rig):
    """10:31 and 11:17 on 12 Nov: options ticked, the spot did not, for 14 and 27 minutes."""
    bw, h, clk = rig
    _tick(bw, clk, [SPOT, "NIFTY|option|25300|CE"])
    for _ in range(14):                                     # 14 s of options only: not yet
        clk.advance(1)
        _tick(bw, clk, ["NIFTY|option|25300|CE"])
        bw._stream_watch()
    assert h.watched == []
    clk.advance(2)
    _tick(bw, clk, ["NIFTY|option|25300|CE"])
    bw._stream_watch()
    assert len(h.watched) == 1
    assert "NIFTY spot silent" in _events(bw)[-1][1]["reason"]
    for _ in range(16):                                     # rate limited: no second action within 15 s
        clk.advance(1)
        _tick(bw, clk, ["NIFTY|option|25300|CE"])
        bw._stream_watch()
    assert len(h.watched) == 2
    for _ in range(16):
        clk.advance(1)
        _tick(bw, clk, ["NIFTY|option|25300|CE"])
        bw._stream_watch()
    assert bw._reconnect_req.is_set() and "NIFTY spot silent" in bw._reconnect_reason


def test_spot_back_resets_the_escalation(rig):
    bw, h, clk = rig
    _tick(bw, clk, [SPOT])
    clk.advance(16)
    _tick(bw, clk, ["NIFTY|option|25300|CE"])
    bw._stream_watch()
    assert bw._spot_attempt == 1
    _tick(bw, clk, [SPOT])
    bw._stream_watch()
    assert bw._spot_attempt == 0


def test_spot_watch_respects_the_window_and_a_dead_socket(rig, monkeypatch):
    bw, h, clk = rig
    assert bw.spot_watch_hours == ("09:15:30", "15:14:00")
    _tick(bw, clk, [SPOT])
    clk.wall = datetime(2026, 11, 12, 15, 20, 0, tzinfo=IST).timestamp()   # after the index freeze
    clk.mono += 300
    _tick(bw, clk, ["NIFTY|option|25300|CE"])
    bw._stream_watch()
    assert h.watched == []
    clk.wall = datetime(2026, 11, 14, 11, 0, 0, tzinfo=IST).timestamp()    # a Saturday
    bw._stream_watch()
    assert h.watched == []
    clk.wall = THU_1100 + 600                                              # in hours, but EVERYTHING silent
    clk.mono += 600
    bw._stream_watch()
    assert h.watched == []                                  # (the ws_silent path owns that case)
    h.sio.connected = False
    _tick(bw, clk, ["NIFTY|option|25300|CE"])
    bw._stream_watch()
    assert h.watched == []


def test_connect_cleans_the_sdk_token_list():
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    h = _Handler()
    h.tokenlist = {"4.1!False", "4.1!None", "4.1!111", "4.1!222"}
    bw._tokens = {"4.1!111", "4.1!333"}
    bw._clean_sdk_tokens(h)
    assert h.tokenlist == {"4.1!111", "4.1!222", "4.1!333"}


def test_ist_window_and_spot_watch_until():
    w = ("09:15:30", "15:14:00")
    at = lambda h, m, d=12: datetime(2026, 11, d, h, m, tzinfo=IST).timestamp()
    assert BreezeWrapper._in_ist_window(at(9, 16), w) and BreezeWrapper._in_ist_window(at(15, 13), w)
    assert not BreezeWrapper._in_ist_window(at(9, 15), w) and not BreezeWrapper._in_ist_window(at(15, 14), w)
    assert not BreezeWrapper._in_ist_window(at(11, 0, 15), w)                 # Sunday
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    bw.set_spot_watch("NIFTY", "15:30")
    assert bw.spot_key == SPOT and bw.spot_watch_hours == ("09:15:30", "15:29:00")
    assert "stream_rejoins" in bw.health() and "stream_rebuilds" in bw.health()


def test_connect_leaves_the_interpreter_switch_interval_alone_by_default(monkeypatch):
    """2.2's 0.5 ms switch interval made every engine round ~10x slower on a busy laptop (12 Nov)."""
    import sys
    import types
    from tests.test_feed_supervisor import _FakeSDK
    sdk = _FakeSDK()
    fake_mod = types.ModuleType("breeze_connect")
    fake_mod.BreezeConnect = lambda api_key: sdk
    monkeypatch.setitem(sys.modules, "breeze_connect", fake_mod)
    before = sys.getswitchinterval()
    try:
        bw = BreezeWrapper(BreezeCredentials("k", "s", "t"), reconnect_max_retries=1, reconnect_backoff_secs=0.0)
        assert bw.switch_interval_ms == 0.0
        bw.connect()
        assert sys.getswitchinterval() == before
        bw.disconnect()
        bw2 = BreezeWrapper(BreezeCredentials("k", "s", "t"), reconnect_max_retries=1,
                            reconnect_backoff_secs=0.0, switch_interval_ms=1.0)
        bw2.connect()                                        # still possible when configured explicitly
        assert abs(sys.getswitchinterval() - 0.001) < 1e-9
        bw2.disconnect()
    finally:
        sys.setswitchinterval(before)


def test_power_throttling_opt_out_is_harmless_everywhere():
    from vajra import power
    r = power.disable_power_throttling()
    assert r in (True, False)
    if sys.platform == "win32":
        assert r is True


def test_spot_watch_backs_off_after_three_rebuilds_that_did_not_help(rig):
    bw, h, clk = rig
    _tick(bw, clk, [SPOT])
    rebuilds = 0
    for _ in range(400):                                    # the spot never comes back
        clk.advance(1)
        _tick(bw, clk, ["NIFTY|option|25300|CE"])
        bw._reconnect_req.clear()
        bw._stream_watch()
        if bw._reconnect_req.is_set():
            rebuilds += 1
    assert rebuilds == 3
    ev = [p for st, p in _events(bw) if st == "ws_streams_missing"]
    assert ev[-1]["action"] == "backing off 300 s"
