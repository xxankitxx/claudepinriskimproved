"""Live-feed option-expiry regression (2026-07-01 incident): NSE's Nov-2024 rationalization left
ONLY NIFTY with a weekly index-options expiry -- BANKNIFTY, FINNIFTY, and every stock are
MONTHLY-ONLY now. Subscribing/quoting them against NIFTY's weekly expiry requests a contract that
does not exist: Breeze ACKs the subscribe ('subscribed successfully') but never streams a tick, so
the ATM quote is permanently missing for every non-NIFTY instrument, all session, with zero recovery
(confirmed live: basis_tape had 4,185 NIFTY rows and ZERO for anything else on a real non-monthly week).
"""
from pathlib import Path

from vajra.config import load_config
from vajra.feed import LiveBreezeSource

ROOT = Path(__file__).resolve().parent.parent
WEEKLY = "2026-07-07"      # this week's NIFTY weekly expiry
MONTHLY = "2026-07-28"     # this month's monthly expiry (BANKNIFTY/FINNIFTY/stocks' ONLY contract)


def _cfg():
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    # 2.0.1 lists every shipped stock's step; these tests exercise the runtime-resolution path that
    # still serves any UNLISTED stock, so un-list the four they use.
    for k in ("IDFCFIRSTB", "BSE", "SHRIRAMFIN", "BAJAJFINSV"):
        cfg.raw["strike_step"].pop(k, None)
    return cfg


class _FakeBreeze:
    def __init__(self):
        self.option_step_calls = []
        self.subscribe_option_calls = []

    def resolve_codes(self, stocks, override):
        pass

    def option_step(self, instrument, expiry_iso):
        self.option_step_calls.append((instrument, expiry_iso))
        return 10.0

    def subscribe_spot(self, instrument):
        pass

    def subscribe_future(self, instrument, expiry_iso):
        pass

    def subscribe_vix(self):
        pass

    def subscribe_option(self, instrument, expiry_iso, strike, right):
        self.subscribe_option_calls.append((instrument, expiry_iso, strike, right))

    def snapshot_raw(self):
        return {}

    def code_for(self, instrument):
        return instrument

    def _key(self, code, kind, strike=None, right=None):
        return f"{code}|{kind}|{strike}|{right}"


def _src(fake):
    cfg = _cfg()
    return LiveBreezeSource(fake, cfg, expiry_iso=WEEKLY, basis_mode="IMPLIED",
                           future_expiry_iso=MONTHLY, event_sink=None), cfg


def test_options_expiry_for_nifty_is_weekly_everyone_else_monthly():
    src, cfg = _src(_FakeBreeze())
    assert src._options_expiry_for("NIFTY") == WEEKLY
    assert src._options_expiry_for("BANKNIFTY") == MONTHLY
    assert src._options_expiry_for("FINNIFTY") == MONTHLY
    for stock in cfg.stocks:
        assert src._options_expiry_for(stock) == MONTHLY, f"{stock} must use the monthly expiry"


def test_option_subscription_uses_monthly_expiry_for_sisters_and_stocks():
    """the actual per-round subscribe_option call (the one that determines whether Breeze streams
    real ticks) must use the monthly expiry for BANKNIFTY/FINNIFTY/stocks, weekly ONLY for NIFTY."""
    fake = _FakeBreeze()
    src, cfg = _src(fake)
    src._ensure_option_subscriptions("NIFTY", 24100, 50, 1)
    src._ensure_option_subscriptions("BANKNIFTY", 58000, 100, 1)
    src._ensure_option_subscriptions("FINNIFTY", 24000, 50, 1)
    src._ensure_option_subscriptions("HDFCBANK", 2000, 10, 1)
    used = {inst: expiry for inst, expiry, strike, right in fake.subscribe_option_calls}
    assert used["NIFTY"] == WEEKLY
    assert used["BANKNIFTY"] == MONTHLY
    assert used["FINNIFTY"] == MONTHLY
    assert used["HDFCBANK"] == MONTHLY


def test_stock_step_resolution_queries_the_monthly_chain_not_the_weekly():
    """option_step() (the runtime strike-spacing discovery) must query the MONTHLY chain for
    stocks -- querying the (non-existent) weekly chain is exactly what caused every stock to fail
    with 'no_chain' and permanently fall back to the wrong step=5.0 default."""
    fake = _FakeBreeze()
    src, cfg = _src(fake)
    # 2.0: stocks listed in strike_step use the measured table (no runtime discovery); drop two
    # so the runtime-resolution path (still used for any unlisted stock) is exercised.
    for k in ("HDFCBANK", "ITC"):
        cfg.raw["strike_step"].pop(k, None)
    src._ensure_base_subscriptions()
    queried = dict(fake.option_step_calls)
    assert queried, "no step-resolution calls were made at all"
    for stock, expiry in queried.items():
        assert expiry == MONTHLY, f"{stock} step-resolution queried {expiry}, must be the monthly {MONTHLY}"


def test_unmapped_ws_name_is_tape_durable_once_per_name():
    """R19: an unmapped ws tick name must produce a tape EVENT (not just stderr), exactly once per
    name, emitted from the round thread (never the ws callback thread)."""
    fake = _FakeBreeze()
    fake._unmapped_warned = {"SOME UNKNOWN CO LTD"}
    events = []
    cfg = _cfg()
    cfg.raw.setdefault("expiry", {})["session_close"] = "23:59"   # keep rounds() alive at any test hour
    cfg.raw.setdefault("feed", {})["poll_interval_secs"] = 0.01
    src = LiveBreezeSource(fake, cfg, expiry_iso=WEEKLY, basis_mode="IMPLIED",
                           future_expiry_iso=MONTHLY,
                           event_sink=lambda t, p, ts_ms=None: events.append((t, p)))
    gen = src.rounds()
    next(gen)                                   # round 1: emits the event
    next(gen)                                   # round 2: must NOT re-emit
    src.stop()
    hits = [(t, p) for t, p in events if p.get("kind") == "UNMAPPED_WS_NAME"]
    assert len(hits) == 1, f"expected exactly one UNMAPPED_WS_NAME event, got {len(hits)}"
    assert hits[0][0] == "ANOMALY" and hits[0][1]["name"] == "SOME UNKNOWN CO LTD"


def test_missing_weight_is_tape_durable():
    """R19: a tracked stock with no weight row must produce a tape EVENT at startup (TCS today)."""
    from datetime import date
    from vajra.app import build_calendar
    from vajra.orchestrator import Orchestrator

    class _RecordingTape:
        def __init__(self): self.events = []
        def event(self, event_type, payload, ts_ms=None): self.events.append((event_type, payload))
        def __getattr__(self, n): return lambda *a, **k: None

    cfg = _cfg()
    res = build_calendar(cfg).resolve(date(2026, 7, 1))
    tape = _RecordingTape()
    o = Orchestrator(cfg, tape, res)
    if o._missing_weights:                       # TCS currently has no weight row -> must be taped
        hits = [p for t, p in tape.events if t == "ANOMALY" and p.get("kind") == "MISSING_WEIGHT"]
        assert hits and set(hits[0]["instruments"]) == set(o._missing_weights)
    else:                                        # weights CSV was completed -> nothing to emit
        assert not any(p.get("kind") == "MISSING_WEIGHT" for _, p in tape.events)


def test_step_resolution_retries_until_the_chain_answers():
    """capture fix: pre-market step resolution always fails (no chains before open) and used to
    leave stocks on the 5.0 default all day. Failed stocks must RETRY at runtime and adopt the
    chain-resolved step once the market answers."""
    class _LateBreeze(_FakeBreeze):
        def __init__(self):
            super().__init__()
            self.answer = None                       # None = market closed, chain not answering
        def option_step(self, instrument, expiry_iso):
            self.option_step_calls.append((instrument, expiry_iso))
            return self.answer

    fake = _LateBreeze()
    events = []
    cfg = _cfg()
    src = LiveBreezeSource(fake, cfg, expiry_iso=WEEKLY, basis_mode="IMPLIED",
                           future_expiry_iso=MONTHLY,
                           event_sink=lambda t, p, ts_ms=None: events.append((t, p)))
    src._step_retry = {"BAJAJFINSV": 0}              # failed at startup, due immediately
    src._step_maintenance(1_000, {})                 # market still closed -> fails, reschedules
    assert "BAJAJFINSV" in src._step_retry and src._step_retry["BAJAJFINSV"] == 1_000 + 90_000
    assert "BAJAJFINSV" not in cfg.raw.get("strike_step", {})
    src._step_maintenance(50_000, {})                # not due yet -> no call
    fake.answer = 20.0                               # market opens, chain answers
    src._step_maintenance(100_000, {})               # due -> the REST call is submitted to the pool
    # 2.1.1: the round never waits on the pool; the answer is collected on a LATER round
    import time as _t
    for _ in range(200):
        if "BAJAJFINSV" in cfg.raw.get("strike_step", {}):
            break
        _t.sleep(0.01)
        src._step_maintenance(100_250, {})
    assert cfg.raw["strike_step"]["BAJAJFINSV"] == 20.0
    assert "BAJAJFINSV" not in src._step_retry
    assert any(p.get("kind") == "STEP_RESOLVED_LATE" for _, p in events)


def test_streamed_grid_corrects_a_too_fine_step():
    """capture fix: FEDERALBNK/KOTAKBANK resolved a 2.5 step from the full exchange list while the
    REAL streamed grid is 5 -> ATM±6 spanned only ~7 strikes. Three consecutive rounds of streamed
    evidence must adopt the real spacing; a non-multiple or index must never be touched."""
    from vajra.marketdata import MarketSnapshot, StrikeQuote

    def _snap20(inst, base):
        ks = {}
        for i in range(5):                            # 5 strikes at REAL 20-pt spacing
            k = base + i * 20
            ks[(k, "CE")] = StrikeQuote(k, "CE", 50.0, 50.0, "feed", 1, 1, 50.0, 50.0)
        return MarketSnapshot(0, inst, base + 40.0, 14.0, None, ks)

    fake = _FakeBreeze()
    events = []
    cfg = _cfg()
    src = LiveBreezeSource(fake, cfg, expiry_iso=WEEKLY, basis_mode="IMPLIED",
                           future_expiry_iso=MONTHLY,
                           event_sink=lambda t, p, ts_ms=None: events.append((t, p)))
    b = {"BAJAJFINSV": _snap20("BAJAJFINSV", 1720)}   # default step 5.0, streamed grid 20 (4x)
    src._step_maintenance(1_000, b)
    src._step_maintenance(2_000, b)
    assert "BAJAJFINSV" not in cfg.raw.get("strike_step", {})   # needs 3 consecutive confirmations
    src._step_maintenance(3_000, b)
    assert cfg.raw["strike_step"]["BAJAJFINSV"] == 20.0
    assert any(p.get("kind") == "STRIKE_STEP_CORRECTED" for _, p in events)
    # indices are NEVER touched even if their snapshot looks coarse
    b2 = {"BANKNIFTY": _snap20("BANKNIFTY", 58000)}
    for ts in (4_000, 5_000, 6_000):
        src._step_maintenance(ts, b2)
    assert "BANKNIFTY" not in src._step_corrected
    assert cfg.raw["strike_step"]["BANKNIFTY"] == 100    # explicit config value untouched
