"""2.2 entry-quality guards (the 15-day tape study: re-buys within 20 s of a trail profit lost
Rs 1.61 lakh over 22 trades; entries with NIFTY moving away from the level lost Rs 1.24 lakh over 15).

Every guard is a WAIT: it returns a reason while its condition holds and lets the same approach fire
once it clears. Off (0 / "off") must be exactly the 2.1 behaviour.
"""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from vajra.app import build_calendar
from vajra.config import load_config
from vajra.orchestrator import Orchestrator
from vajra.schema import Reason

ROOT = Path(__file__).resolve().parent.parent


class _NullWriter:
    def __getattr__(self, n):
        return lambda *a, **k: None


def _orch(**entry):
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    en = cfg.raw.setdefault("entry", {})
    for k in ("momentum_guard_secs", "momentum_min_pts", "max_breach_pts"):
        en[k] = 0
    en["rebuy_guard"] = "off"
    en.update(entry)
    res = build_calendar(cfg).resolve(date(2026, 6, 23))
    return Orchestrator(cfg, _NullWriter(), res)


def _entry(direction="UP", dist=5.0):
    return {"direction": direction, "side": "UPPER" if direction == "UP" else "LOWER", "dist": dist,
            "level": 24100.0, "breached": dist < 0}


def _feed(o, spots, t0=1_000_000, step_ms=1000, entry=None):
    ts = t0
    for sp in spots:
        o._track_spot(ts, sp, entry)
        ts += step_ms
    return ts - step_ms


def test_all_guards_off_is_the_old_behaviour():
    o = _orch()
    ts = _feed(o, [24100 - i for i in range(60)])            # falling hard
    assert o._entry_guards(ts, _entry("UP", -30.0), SimpleNamespace(spot=24041.0)) is None


def test_retreat_guard_waits_while_nifty_moves_away_and_releases_when_it_turns():
    o = _orch(momentum_guard_secs=30, momentum_min_pts=-2)
    ts = _feed(o, [24100.0 - 0.5 * i for i in range(40)])    # UP trade, spot fell ~15 pts in 30 s
    assert o._spot_move(ts, 30, "UP") < -10
    assert o._entry_guards(ts, _entry("UP"), SimpleNamespace(spot=24080.5)) == Reason.ENTRY_RETREATING
    assert o._entry_guards(ts, _entry("DOWN"), SimpleNamespace(spot=24080.5)) is None   # with the move
    ts = _feed(o, [24081.0 + 0.5 * i for i in range(40)], t0=ts + 1000)                 # it turns up
    assert o._entry_guards(ts, _entry("UP"), SimpleNamespace(spot=24100.5)) is None


def test_retreat_guard_tolerance_and_short_history():
    o = _orch(momentum_guard_secs=30, momentum_min_pts=-2)
    ts = _feed(o, [24100.0, 24099.5, 24099.0])               # 3 s of history: no verdict yet
    assert o._spot_move(ts, 30, "UP") is None
    assert o._entry_guards(ts, _entry("UP"), SimpleNamespace(spot=24099.0)) is None
    o2 = _orch(momentum_guard_secs=30, momentum_min_pts=-2)
    ts = _feed(o2, [24100.0 - 0.05 * i for i in range(40)])  # drifting 1.5 pts in 30 s: inside tolerance
    assert o2._entry_guards(ts, _entry("UP"), SimpleNamespace(spot=24098.0)) is None


def test_momentum_guard_can_demand_real_progress_toward_the_level():
    o = _orch(momentum_guard_secs=30, momentum_min_pts=3)
    ts = _feed(o, [24090.0 + 0.05 * i for i in range(40)])   # creeping up 1.5 pts in 30 s: not enough
    assert o._entry_guards(ts, _entry("UP"), SimpleNamespace(spot=24092.0)) == Reason.ENTRY_RETREATING
    ts = _feed(o, [24092.0 + 0.2 * i for i in range(40)], t0=ts + 1000)   # 6 pts in 30 s
    assert o._entry_guards(ts, _entry("UP"), SimpleNamespace(spot=24100.0)) is None


def test_max_breach_guard():
    o = _orch(max_breach_pts=10)
    ts = _feed(o, [24100.0] * 5)
    assert o._entry_guards(ts, _entry("UP", -12.0), SimpleNamespace(spot=24112.0)) == Reason.BREACH_TOO_DEEP
    assert o._entry_guards(ts, _entry("UP", -8.0), SimpleNamespace(spot=24108.0)) is None


def test_rebuy_guard_needs_a_new_extreme_and_ends_with_the_episode():
    o = _orch(rebuy_guard="on")
    ts = 2_000_000
    o._rebuy = {"dir": "UP", "best": 24120.0, "ts": ts}       # a profitable UP trade topped at 24120
    e = _entry("UP", -3.0)
    o._track_spot(ts + 1000, 24112.0, e)
    assert o._entry_guards(ts + 1000, e, SimpleNamespace(spot=24112.0)) == Reason.REBUY_WAIT
    assert o._entry_guards(ts + 1000, _entry("DOWN"), SimpleNamespace(spot=24112.0)) is None  # other side is free
    o._track_spot(ts + 2000, 24121.0, e)                      # new high -> the move resumed
    assert o._entry_guards(ts + 2000, e, SimpleNamespace(spot=24121.0)) is None
    o._track_spot(ts + 3000, 24080.0, None)                   # spot left the entry zone: episode over
    assert o._rebuy is None
    o._rebuy = {"dir": "UP", "best": 24120.0, "ts": ts}
    o._track_spot(ts + o._rebuy_expiry_ms + 5000, 24112.0, e)   # the wait is bounded
    assert o._rebuy is None


def test_best_spot_is_tracked_while_in_a_trade():
    o = _orch(rebuy_guard="on")
    pos = SimpleNamespace(direction="DOWN", entry_spot=24100.0, open=True)
    o.executor.position = pos
    for i, sp in enumerate([24098.0, 24090.0, 24095.0]):
        o._track_spot(1000 * (i + 1), sp, None)
    assert o._trade_best_spot == 24090.0                      # the low, for a DOWN trade


def test_entry_context_is_recorded_on_the_alert():
    o = _orch()
    ts = _feed(o, [24090.0 + 0.2 * i for i in range(130)])
    o._last_profit_ms = ts - 12_000
    c = o._entry_context(ts, _entry("UP", 4.0))
    assert c["move_30s"] == 6.0 and c["move_120s"] == 24.0
    assert c["secs_since_exit"] == 12.0 and c["after"] == "profit" and c["breached"] is False
    assert c["exch_lag_s"] is None                            # replay: no broker clock


def test_closed_trade_is_journalled_with_peak_low_context_and_taped_as_a_trade_event():
    class _Tape(_NullWriter):
        def __init__(self):
            self.events = []

        def event(self, event_type, payload, ts_ms=None):
            self.events.append((event_type, payload, ts_ms))

    o = _orch()
    o.tape = _Tape()
    row = dict(instrument="NIFTY", strike=24100, right="CE", qty=650, intended_price=100.0, slippage=0.0,
               order_id="PAPER-1", live_orders_flag=False)
    o._last_entry_ctx = {"move_30s": 5.5, "after": "profit", "secs_since_exit": 14.0}
    o._emit_orders([dict(row, ts=1000, action="ENTRY", fill_price=100.0, exit_reason="", pnl_realized=0.0)],
                   confluence={"verdict": "LONE"})
    assert o._open_trade["ctx"]["move_30s"] == 5.5
    o.executor.position = SimpleNamespace(entry_fill=100.0, peak_premium=112.0, trough_premium=97.5, open=False)
    o._emit_orders([dict(row, ts=61000, action="EXIT", fill_price=107.0, exit_reason="TRAIL_SL", pnl_realized=4550.0)])
    t = o.journal[-1]
    assert (t["peak_pct"], t["low_pct"], t["pnl"], t["exit_reason"]) == (12.0, -2.5, 4550.0, "TRAIL_SL")
    assert o._last_trade_dir == "UP"
    kind, payload, ts = o.tape.events[-1]
    assert kind == "TRADE" and ts == 61000 and payload["peak_pct"] == 12.0 and payload["ctx"]["after"] == "profit"
    assert payload["entry_ts"] == 1000 and payload["exit_ts"] == 61000 and payload["strike"] == 24100


def test_momentum_history_restarts_when_the_spot_source_switches_to_the_closing_proxy():
    o = _orch(momentum_guard_secs=30, momentum_min_pts=3)
    ts = _feed(o, [24100.0 + 0.3 * i for i in range(40)])    # index rising
    assert o._spot_move(ts, 30, "UP") > 8
    o._proxy_on = True                                       # 15:15: spot = forward - basis from here on
    o._track_spot(ts + 1000, 24060.0, None)                  # a 50-pt step that is not a market move
    assert len(o._spot_hist) == 1 and o._spot_move(ts + 1000, 30, "UP") is None
    assert o._entry_guards(ts + 1000, _entry("DOWN"), SimpleNamespace(spot=24060.0)) is None
