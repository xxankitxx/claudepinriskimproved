"""2.0.4 — NSE closing auction session: schedule by date, parity forward, restart restore."""
import json
from datetime import date
from pathlib import Path

from vajra.closing import resolve_closing, parity_forward
from vajra.config import load_config
from vajra.marketdata import MarketSnapshot, StrikeQuote

ROOT = Path(__file__).resolve().parent.parent


def _cfg():
    return load_config(str(ROOT / "vajra_config.yaml"))


def test_schedule_by_date_and_expiry():
    c = _cfg()
    pre = resolve_closing(c, date(2026, 6, 16), False)
    assert (pre.last_entry, pre.flatten_before, pre.session_close) == ("15:25", "15:25", "15:30")
    cas = resolve_closing(c, date(2026, 10, 21), False)
    assert (cas.index_frozen_from, cas.last_entry, cas.flatten_before, cas.fo_close) == ("15:15", "15:29", "15:38", "15:40")
    exp = resolve_closing(c, date(2026, 10, 19), True)
    assert (exp.last_entry, exp.flatten_before, exp.session_close) == ("15:29", "15:38", "15:40")
    dec = resolve_closing(c, date(2026, 12, 8), False)
    assert (dec.index_frozen_from, dec.fo_close, dec.last_entry, dec.flatten_before) == ("15:30", "15:45", "15:29", "15:38")
    decx = resolve_closing(c, date(2026, 12, 8), True)
    assert decx.fo_close == "15:30" and decx.flatten_before <= "15:28" and decx.last_entry <= decx.flatten_before


def test_square_off_is_clamped_before_the_fo_close():
    c = _cfg()
    c.raw["closing_session"]["flatten_before"] = "15:45"
    c.raw["closing_session"]["last_entry"] = "15:50"
    t = resolve_closing(c, date(2026, 10, 21), False)
    assert t.flatten_before == "15:38" and t.last_entry == "15:38"


def _q(k, r, ltp):
    return StrikeQuote(k, r, ltp, ltp, "feed", 1, 1, ltp - 0.5, ltp + 0.5)


def test_parity_forward_is_median_of_three_strikes():
    s = {}
    for k, ce, pe in ((22700, 60.0, 20.0), (22750, 30.0, 40.0), (22800, 10.0, 69.0)):
        s[(k, "CE")], s[(k, "PE")] = _q(k, "CE", ce), _q(k, "PE", pe)
    snap = MarketSnapshot(0, "NIFTY", 22741.0, 13, None, s)
    # 22740, 22740, 22741 -> 22740
    assert parity_forward(snap, 50, 22741.0) == 22740.0
    assert parity_forward(MarketSnapshot(0, "NIFTY", 1, 1, None, {}), 50, 22741.0) is None


def test_restart_restores_pre_freeze_parity_basis(tmp_path):
    from vajra.app import build_calendar
    from vajra.orchestrator import Orchestrator

    class _W:
        def append(self, *a): pass
        def event(self, *a, **k): pass
    c = _cfg()
    o = Orchestrator(c, _W(), build_calendar(c).resolve(date(2026, 10, 21)))
    from datetime import datetime, timezone, timedelta
    base = int(datetime(2026, 10, 21, 15, 13, tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp() * 1000)
    rows = [{"ts": base + i * 1000, "instrument": "NIFTY", "spot_source": "INDEX", "parity_basis": 20.0 + (i % 3)}
            for i in range(50)]
    rows.append({"ts": base + 120_000, "instrument": "NIFTY", "spot_source": "CLOSING_PROXY", "parity_basis": None})
    (tmp_path / "basis_tape.jsonl").write_text("\n".join(json.dumps(r) for r in [{"x": 1}] + rows) + "\n")
    assert o.restore_basis_history(tmp_path) == 50


def test_feed_window_follows_the_future_in_the_auction():
    """21 Oct: centred on the +435 pt indicative index the window lost every near-the-money strike."""
    from datetime import datetime, timezone, timedelta
    from vajra.feed import LiveBreezeSource
    ist = timezone(timedelta(hours=5, minutes=30))
    ms = lambda h, m: int(datetime(2026, 10, 21, h, m, tzinfo=ist).timestamp() * 1000)
    src = LiveBreezeSource.__new__(LiveBreezeSource)
    src._closing_times = resolve_closing(_cfg(), date(2026, 10, 21), False)
    assert src._window_center("NIFTY", 22742.0, 22750.0, ms(15, 14)) == 22742.0
    assert src._window_center("NIFTY", 23173.0, 22740.0, ms(15, 20)) == 22732.0      # 22742 + (22740 - 22750)
    assert src._window_center("BANKNIFTY", 51000.0, 51100.0, ms(15, 20)) == 51000.0


def test_a_steady_50pt_basis_is_healthy_but_a_sudden_shift_is_not():
    """23 Oct 2026: basis 46-66 pts all day; the 40-pt absolute limit blocked trading from 09:20."""
    from vajra.feed_health import FeedHealth
    c = _cfg()
    fs = c.get("feed_sanity")
    fh = FeedHealth(max_abs_basis=float(fs["max_abs_basis_pts"]), rel_basis_pts=float(fs["rel_basis_pts"]))
    t, spot = 1_000_000, 22730.0
    for i in range(4 * 300):                       # 5 min of a real, steady ~55 pt premium
        spot += (1 if i % 2 else -1) * 0.5
        st = fh.update(t + i * 250, spot, 55.0 + (i % 7) - 3)
    assert st.ok
    for k in range(20):                            # garbage: basis suddenly 40 pts off its own level
        st = fh.update(t + 300_000 + k * 250, spot + (0.5 if k % 2 else 0), 95.0)
    assert not st.ok
