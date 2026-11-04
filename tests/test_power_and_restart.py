"""2.1 — power-loss due diligence (2 Nov 2026: the laptop switched off at 15:28 with a position open).

  * the machine-power helpers never raise and report a sane shape;
  * an on-battery edge is loud exactly once (event + forced alarm cue) and AC restore is logged;
  * a partition whose event tape has no SHUTDOWN is reported as an abnormal end, with the open position;
  * a restart on such a partition restores the position / capital / journal / cooldowns.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
D = date(2026, 11, 2)


def _ms(h, m, s=0):
    return int(datetime(D.year, D.month, D.day, h, m, s, tzinfo=IST).timestamp() * 1000)


def test_power_helpers_never_raise():
    from vajra import power
    st = power.status()
    assert set(st) == {"on_battery", "battery_pct", "ac"}
    power.keep_awake()                     # True on Windows, False elsewhere; never raises
    power.release()


def _orch(tmp_path):
    from vajra.config import load_config
    from vajra.app import build_calendar
    from vajra.orchestrator import Orchestrator
    from vajra.tape import TapeWriter
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    res = build_calendar(cfg).resolve(D)
    tw = TapeWriter(tmp_path, D)
    return Orchestrator(cfg, tw, res), tw


def test_on_battery_edge_is_loud_once(tmp_path, monkeypatch):
    o, tw = _orch(tmp_path)
    seq = [{"on_battery": False, "battery_pct": 90, "ac": True}, {"on_battery": True, "battery_pct": 42, "ac": False},
           {"on_battery": True, "battery_pct": 41, "ac": False}, {"on_battery": False, "battery_pct": 41, "ac": True}]
    it = iter(seq)
    o.power_status_fn = lambda: next(it)
    events = []
    monkeypatch.setattr(o, "_emit_event", lambda t, p, ts_ms=None: events.append((t, p)))
    for i in range(4):
        o._power_round = i * 120                   # sample on every call
        o._check_power(_ms(10, 0, i))
    kinds = [p.get("kind") for t, p in events]
    assert kinds == ["POWER_ON_BATTERY", "POWER_AC_RESTORED"]
    assert any(e["kind"] == "feed_alarm" and "BATTERY" in e["text"] for e in o.sounds.snapshot())
    o._update_view(_ms(10, 1), {}, o.decision.evaluate(_ms(10, 1), {}, abrupt_against_bias=False, expiry_gate_ok=True,
                                                       routing_mode="CONFIRM", bias_threshold=0.25, bias_threshold_lead=0.55,
                                                       position_active=False), {"state": "STAND_BY"})
    assert o.latest_state["power"]["ac"] is True
    tw.close()


def _write_killed_partition(root: Path, with_open_position: bool):
    part = root / D.isoformat()
    part.mkdir(parents=True)
    ev = [{"ts": _ms(8, 30), "event_type": "STARTUP_CONFIG", "schema_version": "2.0.0", "payload": {}},
          {"ts": _ms(9, 20), "event_type": "REGIME", "schema_version": "2.0.0", "payload": {}}]
    (part / "event_tape.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    (part / "round_tape.jsonl").write_text(json.dumps({"ts": _ms(15, 28, 10), "spot": 23336.77}) + "\n", encoding="utf-8")
    rows = [{"ts": _ms(9, 45, 18), "action": "ENTRY", "instrument": "NIFTY", "strike": 23300, "right": "CE", "qty": 910,
             "intended_price": 107.9, "fill_price": 107.9, "slippage": 0.0, "order_id": "PAPER-1", "live_orders_flag": False,
             "exit_reason": "", "pnl_realized": 0.0},
            {"ts": _ms(10, 0, 32), "action": "EXIT", "instrument": "NIFTY", "strike": 23300, "right": "CE", "qty": 910,
             "intended_price": 94.5, "fill_price": 94.5, "slippage": 0.0, "order_id": "PAPER-2", "live_orders_flag": False,
             "exit_reason": "SL_HIT", "pnl_realized": -12194.0}]
    if with_open_position:
        rows.append({"ts": _ms(15, 13, 49), "action": "ENTRY", "instrument": "NIFTY", "strike": 23350, "right": "CE",
                     "qty": 910, "intended_price": 59.85, "fill_price": 59.85, "slippage": 0.0, "order_id": "PAPER-3",
                     "live_orders_flag": False, "exit_reason": "", "pnl_realized": 0.0})
    (part / "order_tape.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return part


def test_abnormal_end_is_reported_with_the_open_position(tmp_path):
    from vajra.app import abnormal_end_report
    _write_killed_partition(tmp_path, with_open_position=True)
    lines = abnormal_end_report(tmp_path, D)
    assert len(lines) == 1 and "ENDED ABNORMALLY" in lines[0] and "15:28:10" in lines[0]
    assert "CE 23350 x910" in lines[0]
    # a clean day (SHUTDOWN after the start) is silent
    part = tmp_path / D.isoformat()
    with open(part / "event_tape.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ms(15, 40), "event_type": "SHUTDOWN", "schema_version": "2.0.0", "payload": {}}) + "\n")
    assert abnormal_end_report(tmp_path, D) == []


def test_restart_after_power_off_restores_and_squares_off(tmp_path):
    """The killed session left CE 23350 x910 open. A restart must restore it (capital after the earlier
    loss, journal, cooldown) and the next rounds must manage it: at/after flatten time it is squared off."""
    from vajra.marketdata import MarketSnapshot, StrikeQuote
    part = _write_killed_partition(tmp_path, with_open_position=True)
    o, tw = _orch(tmp_path)
    info = o.restore_from_order_tape(part)
    assert info["open_position"].startswith("CE 23350 x910") and info["capital"] == 100000 - 12194.0
    assert len(o.journal) == 1 and o._open_trade and o._open_trade["status"] == "OPEN"
    assert o.executor.position_active and o._stop_cooldown_active

    def snap(ts, spot=23336.0, prem=63.0):
        ks = {}
        for k in range(23050, 23650, 50):
            for r in ("CE", "PE"):
                p = prem if (k == 23350 and r == "CE") else max(1.0, 100 - abs(k - spot) / 5)
                ks[(k, r)] = StrikeQuote(k, r, p, p, "feed", 100, 1000, p - 0.5, p + 0.5)
        return MarketSnapshot(ts, "NIFTY", spot, 15.0, None, ks)
    o.process_round(_ms(15, 30, 0), {"NIFTY": snap(_ms(15, 30, 0))})
    assert o.executor.position_active                       # managed, not yet flattened
    o.process_round(_ms(15, 38, 0), {"NIFTY": snap(_ms(15, 38, 0), prem=63.0)})
    assert not o.executor.position_active
    last = o.journal[-1]
    assert last["status"] == "CLOSED" and last["exit_reason"] == "EXPIRY_FLATTEN" and last["strike"] == 23350
    assert abs(last["pnl"] - (62.5 - 59.85) * 910) < 1e-6    # sold at the bid
    tw.close()
