"""2.2 operator tools: the what-if lab's day cache must hand the engine exactly what the tick tape holds,
and the trade review must read both the new TRADE events and a pre-2.2 order tape."""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def _tick_row(ts, inst, spot, strikes):
    return {"ts": ts, "instrument": inst, "spot": spot, "atm_strike": 24100, "future": spot + 12.5,
            "basis": -12.5, "basis_mode": "IMPLIED", "vix": 14.2,
            "strikes": [{"strike": k, "right": r, "ltp": ltp, "vwap": ltp - 1.0, "vwap_source": "feed",
                         "volume": 1000 + k % 7, "oi": 5000 + k % 11, "bid": ltp - 0.05, "ask": ltp + 0.05}
                        for (k, r, ltp) in strikes]}


def test_lab_cache_round_trips_the_nifty_rounds(tmp_path, monkeypatch):
    import lab
    from vajra.tape import TapeWriter
    d = date(2026, 11, 6)
    tw = TapeWriter(tmp_path, d)
    want = []
    for i in range(12):
        strikes = [(24050, "CE", 120.0 + i), (24100, "CE", 80.5 + i), (24100, "PE", 70.25 - i), (24150, "PE", 110.0)]
        tw.append("tick_tape", _tick_row(1000 + 250 * i, "NIFTY", 24100.0 + i, strikes))
        tw.append("tick_tape", _tick_row(1000 + 250 * i, "BANKNIFTY", 58000.0 + i, [(58000, "CE", 300.0, )]))
        want.append((1000 + 250 * i, 24100.0 + i, strikes))
    tw.close()
    monkeypatch.setattr(lab, "LAB", tmp_path / "labcache")
    msg = lab.build_cache(d.isoformat(), tape_root=tmp_path)
    assert "12 rounds" in msg and "48 quotes" in msg
    assert lab.build_cache(d.isoformat(), tape_root=tmp_path).endswith("cached")
    got = list(lab._CacheSource(d.isoformat(), stride=1).rounds())
    assert len(got) == 12
    for (ts, snaps), (wts, wspot, wstrikes) in zip(got, want):
        s = snaps["NIFTY"]
        assert set(snaps) == {"NIFTY"} and ts == wts and s.ts_ms == wts and s.spot == wspot and s.vix == 14.2
        assert s.future_traded is None                       # IMPLIED day: the future is not a traded input
        assert len(s.strikes) == len(wstrikes)
        for k, r, ltp in wstrikes:
            q = s.quote(k, r)
            assert (q.ltp, q.vwap, q.bid, q.ask, q.vwap_source) == (ltp, ltp - 1.0, ltp - 0.05, ltp + 0.05, "feed")
            assert (q.volume, q.oi) == (1000 + k % 7, 5000 + k % 11)
    assert [ts for ts, _ in lab._CacheSource(d.isoformat(), stride=4).rounds()] == [1000, 2000, 3000]


def test_lab_tape_keeps_orders_and_events_only():
    import lab
    t = lab._LabTape()
    t.append("order_tape", {"action": "ENTRY"})
    t.append("tick_tape", {"x": 1})
    t.event("TRADE", {"pnl": 5.0}, ts_ms=7)
    t.flush(); t.end_round(); t.round({"ts": 1})
    assert t.orders == [{"action": "ENTRY"}] and t.events[0]["event_type"] == "TRADE"


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_trade_review_reads_a_pre_22_day_from_the_order_tape(tmp_path):
    import trade_review as tr
    part = tmp_path / "2026-11-03"
    part.mkdir()
    o = lambda ts, act, px, why="", pnl=0.0: {"ts": ts, "action": act, "strike": 25000, "right": "CE", "qty": 650,
                                              "fill_price": px, "exit_reason": why, "pnl_realized": pnl}
    _write(part / "order_tape.jsonl", [o(1000, "ENTRY", 100.0), o(61000, "EXIT", 110.0, "TRAIL_SL", 6500.0),
                                       o(73000, "ENTRY", 111.0), o(133000, "EXIT", 97.7, "SL_HIT", -8645.0),
                                       o(900000, "ENTRY", 90.0), o(960000, "EXIT", 99.0, "TRAIL_SL", 5850.0)])
    _write(part / "event_tape.jsonl", [
        {"ts": 1, "event_type": "EXPIRY_RESOLVED", "payload": {"is_expiry_day": True}},
        {"ts": 73000, "event_type": "APPROACH_ALERT", "payload": {"dist_pts": -6.2, "verdict": "LONE"}}])
    trades = tr.day_trades(part)
    assert [t["pnl"] for t in trades] == [6500.0, -8645.0, 5850.0] and all(t["expiry"] for t in trades)
    second, third = trades[1], trades[2]
    assert second["ctx"]["secs_since_exit"] == 12.0 and second["ctx"]["after"] == "profit"
    assert second["ctx"]["breached"] is True and second["verdict"] == "LONE"
    assert third["ctx"]["after"] == "stop" and third["hold_s"] == 60
    key = dict(tr.GROUPS)["entry came"]
    assert [key(t) for t in trades] == ["first of the day", "within 20 s of a profit", "after a stop"]


def test_trade_review_prefers_the_22_trade_events(tmp_path, capsys):
    import trade_review as tr
    part = tmp_path / "2026-11-11"
    part.mkdir()
    _write(part / "order_tape.jsonl", [])
    _write(part / "event_tape.jsonl", [
        {"ts": 5, "event_type": "TRADE", "payload": {
            "entry_ts": 1000, "exit_ts": 91000, "direction": "DOWN", "right": "PE", "strike": 24900, "qty": 650,
            "entry_price": 80.0, "exit_price": 88.0, "pnl": 5200.0, "pnl_pct": 10.0, "exit_reason": "TRAIL_SL",
            "verdict": "MILD", "peak_pct": 14.0, "low_pct": -3.0,
            "ctx": {"move_30s": 6.5, "secs_since_exit": None, "after": None, "exch_lag_s": 1.1, "breached": False}}}])
    trades = tr.day_trades(part)
    assert len(trades) == 1 and trades[0]["peak_pct"] == 14.0 and trades[0]["ctx"]["move_30s"] == 6.5
    assert tr.main([part.name, "--tapes", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "24900PE" in out and "peak +14.0%" in out and "by exit reason" in out
