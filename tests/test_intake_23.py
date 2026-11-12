"""2.3 tick intake speed-ups must not change what lands in `latest` or on the raw tick tape.

  * a known token's tick is normalised in one pass (`_ingest_fast`) -- compared field for field with
    `_normalize_tick` + `_ingest` over thousands of random ticks, including missing / empty / zero
    fields, carry-forward, the VIX fix and the NIFTY lag gauge;
  * the consumer decodes the 'stock' event packet itself and leaves every other packet to the library.
"""
from __future__ import annotations

import json
import random
import time

from vajra.breeze import BreezeWrapper, BreezeCredentials


class _SDK:
    def get_data_from_stock_token_value(self, s):
        return {}

    def parse_data(self, d):
        return {}


class _Rec:
    def __init__(self):
        self.rows = []

    def record(self, norm, raw, recv_ms):
        self.rows.append((dict(norm), dict(raw) if isinstance(raw, dict) else raw, recv_ms))


TOKENS = {
    "4.1!NIFTY 50": {"stock_code": "NIFTY", "product_type": "cash"},
    "4.1!INDIA VIX": {"stock_code": "INDVIX", "product_type": "cash"},
    "4.1!2885": {"stock_code": "RELIND", "product_type": "cash"},
    "4.1!61471": {"stock_code": "NIFTY", "product_type": "futures"},
    "4.1!35054": {"stock_code": "NIFTY", "product_type": "options", "strike_price": "25300", "right": "Call"},
    "4.1!35055": {"stock_code": "NIFTY", "product_type": "options", "strike_price": "25300", "right": "Put"},
    "4.1!77001": {"stock_code": "CNXBAN", "product_type": "options", "strike_price": "58000.0", "right": "call"},
    "4.1!99999": None,                       # a token we never subscribed (SDK reverse lookup finds nothing)
}


def _wrapper():
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    bw._breeze = _SDK()
    bw._stock_codes = {"NIFTY": "NIFTY", "RELIANCE": "RELIND"}
    bw._token_kw = {k: v for k, v in TOKENS.items() if v}
    bw.raw_recorder = _Rec()
    return bw


def _rand_tick(rnd, tok):
    fonse = TOKENS[tok] is None or TOKENS[tok]["product_type"] != "cash"
    num = lambda: rnd.choice([round(rnd.uniform(0.05, 400), 2), 0, 0.0, None, "", str(round(rnd.uniform(1, 300), 2)), "0"])
    ltt = rnd.choice([int(time.time()) - rnd.randint(0, 30), None, "x", float(int(time.time()))])
    d = [tok, num(), num(), num(), num(), num(), num(), num(), num(), num(), num(), num(), num(), num(), num(),
         num(), num(), num(), "up", num(), num()]
    if fonse:
        d += [ltt, num()]
        d[21] = ltt
    else:
        d[19] = ltt
    if tok == "4.1!INDIA VIX":
        d[2] = rnd.choice([0.14, 14.2, 0, None, "0.2"])
    return d


def _nan(v):
    return "nan" if isinstance(v, float) and v != v else v      # NaN never equals itself


def _strip(latest):
    return {k: {f: _nan(v) for f, v in d.items() if f != "_recv_ms"} for k, d in latest.items()}


def test_fast_ingest_equals_the_reference_path_on_random_ticks():
    rnd = random.Random(23)
    old, new = _wrapper(), _wrapper()
    toks = list(TOKENS)
    for i in range(6000):
        d = _rand_tick(rnd, rnd.choice(toks))
        recv = 1_000_000 + i
        t = old._parse_fast(list(d))
        old._ingest([t], recv)                              # the 2.2 path, every tick
        t2 = new._parse_fast(list(d))
        st = new._static.get(t2["symbol"])
        if st is None:                                      # first tick of a token: reference path + learn
            new._ingest([t2], recv)
            n0 = new._normalize_tick(t2)
            name = (t2.get("stock_name") or "").strip()
            new._static[t2["symbol"]] = (n0["stock_code"], n0["kind"], n0["strike"], n0["right"], n0["_key"],
                                         ("VIX" in name.upper()) or (t2.get("stock_code") or t2["symbol"]) == "INDVIX")
        else:
            new._ingest_fast(st, t2, recv)
        assert _strip(old.latest) == _strip(new.latest), (i, d)
        assert old.tick_lag_s == new.tick_lag_s
    assert old.ticks_received == new.ticks_received == 6000
    assert len(old.raw_recorder.rows) == len(new.raw_recorder.rows) == 6000
    for a, b in zip(old.raw_recorder.rows, new.raw_recorder.rows):
        assert ({k: _nan(v) for k, v in a[0].items()}, a[1], a[2]) == ({k: _nan(v) for k, v in b[0].items()}, b[1], b[2])
    assert {v["kind"] for v in new.latest.values()} == {"cash", "future", "option"}
    assert len(new._static) == len(TOKENS)


def test_fast_on_message_learns_a_token_on_its_first_tick_and_matches():
    rnd = random.Random(5)
    ref, bw = _wrapper(), _wrapper()
    for i in range(400):
        d = _rand_tick(rnd, rnd.choice(list(TOKENS)))
        ref._ingest([ref._parse_fast(list(d))], 0)
        bw._fast_on_message(list(d))
    assert _strip(ref.latest) == _strip(bw.latest) and bw.fast_parsed == 400
    vix = bw.latest["INDVIX|cash|None|None"]
    assert vix["stock_code"] == "INDVIX"


class _Sio:
    def __init__(self):
        self.lib = []

    def _handle_eio_message(self, data):
        self.lib.append(data)


def test_consumer_decodes_stock_ticks_itself_and_leaves_the_rest_to_the_library():
    bw = _wrapper()
    sio = _Sio()
    tick = ["4.1!35054", 100.0, 101.5, 110.0, 90.0, 1.2, 99.5, 50, 100.5, 75, 25, 100.2, 12345, 10, 999, 500, 400,
            1e6, "up", 50.0, 150.0, int(time.time()), 98.0]
    packets = ['42' + json.dumps(["stock", tick], separators=(",", ":")),
               '40{"sid":"abc"}',                                   # CONNECT ack
               '42["order",{"x":1}]',                               # another event
               '42["stock",[1,2],"extra"]',                         # not the 2-element shape
               '42["stock",not json',                               # garbage: counted, never raised
               ["4.1!35054", 1]]                                    # a non-string packet (tests / old path)
    # what the consumer really receives: engine.io's decoded packet data (its leading '4' stripped)
    from engineio import packet as eio_packet
    wire = packets
    packets = [eio_packet.Packet(encoded_packet=p).data if isinstance(p, str) else p for p in wire]
    assert packets[0].startswith('2["stock",')
    bw._start_consumer()
    for p in packets:
        bw._inbox.append((time.time(), sio, p))
    bw._inbox_ev.set()
    deadline = time.time() + 5
    while time.time() < deadline and bw.consumed < len(packets):
        time.sleep(0.01)
    bw.disconnect()
    assert bw.consumed == len(packets)
    assert bw.latest["NIFTY|option|25300|CE"]["ltp"] == 101.5 and bw.fast_parsed == 1
    assert sio.lib == ['0{"sid":"abc"}', '2["order",{"x":1}]', '2["stock",[1,2],"extra"]', ["4.1!35054", 1]]
    assert bw.bad_ticks == 1


def test_subscription_errors_reach_the_tape_once_per_message():
    bw = _wrapper()
    for _ in range(50):
        bw._log_sub_error("socket not connected")
    bw._log_sub_error("Authentication Failed")
    ev = [(st, p) for _t, st, p in bw.drain_events() if st == "sub_error"]
    assert [p["reason"] for _s, p in ev] == ["socket not connected", "Authentication Failed"]


def test_snapshot_quote_reuse_equals_rebuilding_every_quote():
    """2.3: an unchanged tick reuses last round's StrikeQuote. Two feeds over the same evolving tick
    store -- one with the reuse switched off -- must build identical snapshots, round after round,
    including computed VWAPs, price-less ticks and ticks that appear late."""
    from pathlib import Path
    from vajra.config import load_config
    from vajra.feed import LiveBreezeSource
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "vajra_config.yaml"))
    rnd = random.Random(77)
    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"))
    bw._stock_codes = {s: s[:6] for s in cfg.stocks}
    insts = ["NIFTY", "BANKNIFTY", "HDFCBANK"]
    keys = []
    for inst in insts:
        code, step = bw.code_for(inst), cfg.strike_step(inst)
        spot = 25000.0 if inst == "NIFTY" else 58000.0 if inst == "BANKNIFTY" else 1000.0
        bw.latest[bw._key(code, "cash", None, None)] = {"ltp": spot}
        for k in range(-16, 17):
            for r in ("CE", "PE"):
                keys.append(bw._key(code, "option", int(spot + k * step), r))

    def new_tick():
        return {"ltp": rnd.choice([round(rnd.uniform(1, 300), 2), None]),
                "vwap": rnd.choice([None, 0, round(rnd.uniform(1, 300), 2)]),
                "volume": rnd.choice([None, rnd.randint(0, 99999)]), "oi": rnd.choice([None, 5000]),
                "bid": rnd.choice([None, 10.0]), "ask": rnd.choice([None, 10.5])}

    mk = lambda: LiveBreezeSource(bw, cfg, expiry_iso="2026-11-17", basis_mode="IMPLIED",
                                  future_expiry_iso="2026-11-23", event_sink=lambda *a, **k: None)
    fast, ref = mk(), mk()
    ref._quote_cache = None                                  # the 2.2 behaviour: rebuild everything
    reused = 0
    for rnd_no in range(120):
        for k in rnd.sample(keys, 25):                       # ~12% of the streams tick each round
            bw.latest[k] = new_tick()
        raw = bw.snapshot_raw()
        for inst in insts:
            a = fast._build_snapshot(inst, 1000 + rnd_no * 250, raw)
            b = ref._build_snapshot(inst, 1000 + rnd_no * 250, raw)
            assert a.strikes == b.strikes and a.spot == b.spot, (rnd_no, inst)
            if rnd_no:
                reused += sum(1 for k2, q in a.strikes.items() if q is prev[inst].get(k2))
            prev = locals().get("prev", {})
            prev[inst] = dict(a.strikes)
    assert reused > 3000                                     # the reuse really happened
