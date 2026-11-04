"""§11.5 determinism + §11.2 tape-emission (pytest mirror of the verify gate).

Records a synthetic session into a temp tape, replays it twice, and asserts the
decision_tape is byte-identical and the full tape set is produced & populated.
"""

from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from vajra.config import load_config
from vajra.app import build_calendar, choose_synthetic_session, write_startup_events
from vajra.tape import TapeWriter
from vajra.orchestrator import Orchestrator
from vajra.synthetic import generate_rounds
from vajra import schema as S
from replay import replay_session

ROOT = Path(__file__).resolve().parent.parent


def _record(tmp: Path) -> Path:
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    cal = build_calendar(cfg)
    sess = choose_synthetic_session(cal, date(2026, 6, 1))
    res = cal.resolve(sess)
    writer = TapeWriter(tmp / "rec", sess, schema_version=cfg.schema_version)
    write_startup_events(writer, cfg, res, 0)
    orch = Orchestrator(cfg, writer, res, breeze=None)

    class _Static:
        def rounds(self_inner):
            yield from generate_rounds(cfg, sess)
    orch.run(_Static())
    writer.close()
    return tmp / "rec" / sess.isoformat()


def test_breach_time_is_stamped(tmp_path):
    """breach TIME: instruments_view must carry breach_ts = when the current breach started (and
    None when not breached). Run a synthetic session and assert the invariant + that at least one
    breach got a timestamp <= its round_ts."""
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    cal = build_calendar(cfg)
    sess = choose_synthetic_session(cal, date(2026, 6, 1))
    res = cal.resolve(sess)
    o = Orchestrator(cfg, TapeWriter(tmp_path / "bt", sess), res, breeze=None)
    stamped = 0
    for ts, sn in generate_rounds(cfg, sess):
        st = o.process_round(ts, sn)
        for inst, v in (st.get("instruments") or {}).items():
            if v.get("breached") is not None and not v.get("stale"):
                assert v.get("breach_ts") is not None, f"{inst} breached but breach_ts is None"
                assert v["breach_ts"] <= ts
                stamped += 1
            elif v.get("breached") is None and not v.get("stale"):
                assert v.get("breach_ts") is None, f"{inst} not breached but breach_ts set"
    assert stamped > 0, "no breach was ever stamped in the synthetic session"


def test_post_stop_cooldown_blocks_reentry(tmp_path):
    """operator rule: after a hard-stop (SL_HIT) loss, take NO new trade for post_stop_cooldown_secs
    (180s). Find the synthetic round that WOULD enter, inject a recent stop, and assert it is blocked
    while the cooldown is active and fires once it expires."""
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    cool_ms = int(float(cfg.get("execution", "post_stop_cooldown_secs")) * 1000)
    assert cool_ms > 0
    cal = build_calendar(cfg)
    sess = choose_synthetic_session(cal, date(2026, 6, 1))
    res = cal.resolve(sess)
    rounds = list(generate_rounds(cfg, sess))

    def run_to_first_entry(o, upto=None):
        for i, (ts, sn) in enumerate(rounds):
            if upto is not None and i >= upto:
                return i
            o.process_round(ts, sn)
            if o.executor.position_active:
                return i
        return None

    o0 = Orchestrator(cfg, TapeWriter(tmp_path / "a", sess), res, breeze=None)
    fire_idx = run_to_first_entry(o0)
    assert fire_idx is not None, "synthetic session never entered — cannot test the cooldown"
    fire_ts = rounds[fire_idx][0]

    # replay up to the firing round, arm a stop 10s ago -> within the 180s cooldown -> must NOT enter
    o1 = Orchestrator(cfg, TapeWriter(tmp_path / "b", sess), res, breeze=None)
    run_to_first_entry(o1, upto=fire_idx)
    o1._last_stop_ms = fire_ts - 10_000
    st = o1.process_round(*rounds[fire_idx])
    assert not o1.executor.position_active, "entered despite being inside the post-stop cooldown"
    assert str((st.get("arming") or {}).get("entry_block", "")).startswith("POST-STOP COOLDOWN")

    # same round, but the stop was just past the cooldown -> expired -> it DOES enter
    o2 = Orchestrator(cfg, TapeWriter(tmp_path / "c", sess), res, breeze=None)
    run_to_first_entry(o2, upto=fire_idx)
    o2._last_stop_ms = fire_ts - (cool_ms + 1_000)
    o2.process_round(*rounds[fire_idx])
    assert o2.executor.position_active, "did not enter after the cooldown expired"


def test_bad_prev_close_seed_does_not_crash_startup(tmp_path):
    """council RISK: seed_from_prev_close runs OUTSIDE the run-loop try/except — a seed that raises
    must NOT kill the session before it starts; run() should swallow it and finalize cleanly."""
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    cal = build_calendar(cfg)
    sess = choose_synthetic_session(cal, date(2026, 6, 1))
    res = cal.resolve(sess)
    o = Orchestrator(cfg, TapeWriter(tmp_path / "s", sess), res, breeze=None)

    class _BoomSeed:
        def prev_close_snapshot(self_inner):
            raise ValueError("simulated bad seed")
        def rounds(self_inner):
            return iter(())                       # no rounds -> run() just seeds then finalizes
    o.run(_BoomSeed())                            # must NOT raise


def test_post_stop_cooldown_recomputes_fresh_levels_when_it_ends(tmp_path):
    """operator rule: levels must be refreshed not just right after the stop-loss, but AGAIN the
    instant the 11-minute post-stop cooldown window closes — the market can move meaningfully in
    11 minutes, so trading must resume on a current band, not the one from right after the loss."""
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    # 2.0: this test re-feeds the SAME snapshot 11 minutes later; the feed-sanity gate (correctly)
    # calls that a frozen spot and refuses to re-anchor. The test is about cooldown timing only.
    cfg.raw.setdefault("feed_sanity", {})["enabled"] = "off"
    cool_ms = int(float(cfg.get("execution", "post_stop_cooldown_secs")) * 1000)
    cal = build_calendar(cfg)
    sess = choose_synthetic_session(cal, date(2026, 6, 1))
    res = cal.resolve(sess)
    rounds = list(generate_rounds(cfg, sess))
    nifty_rounds = [(ts, sn) for ts, sn in rounds if sn.get("NIFTY") is not None]
    assert len(nifty_rounds) >= 3

    o = Orchestrator(cfg, TapeWriter(tmp_path / "s", sess), res, breeze=None)
    ts0, sn0 = nifty_rounds[0]
    o.process_round(ts0, sn0)                          # anchor the initial band (09:20 "open")
    o._last_stop_ms = ts0                               # simulate a stop just occurred

    ts1, sn1 = nifty_rounds[1]
    o.process_round(ts1, sn1)                           # still well inside the 660s window -> no recompute
    assert o._stop_cooldown_active is True
    before = len(o._level_history)

    ts_end = ts1 + cool_ms + 250                        # first round PAST the cooldown window
    o.process_round(ts_end, sn1)
    assert o._stop_cooldown_active is False
    after = o._level_history[before:]
    assert any(row.get("trigger") == "post_cooldown" for row in after), \
        "no fresh level recompute fired the instant the post-stop cooldown ended"


def test_post_profit_cooldown_blocks_reentry_silently(tmp_path):
    """operator rule: after a PROFITABLE exit, wait post_profit_cooldown_secs (120s) before the next
    trade — SILENTLY (no cockpit banner, no event). It blocks within the window and releases after."""
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    pcool = int(float(cfg.get("execution", "post_profit_cooldown_secs")) * 1000)
    assert pcool > 0
    cal = build_calendar(cfg)
    sess = choose_synthetic_session(cal, date(2026, 6, 1))
    res = cal.resolve(sess)
    rounds = list(generate_rounds(cfg, sess))

    def run_to_first_entry(o, upto=None):
        for i, (ts, sn) in enumerate(rounds):
            if upto is not None and i >= upto:
                return i
            o.process_round(ts, sn)
            if o.executor.position_active:
                return i
        return None

    fire_idx = run_to_first_entry(Orchestrator(cfg, TapeWriter(tmp_path / "a", sess), res, breeze=None))
    assert fire_idx is not None
    fire_ts = rounds[fire_idx][0]

    # a profit 10s ago -> within the cooldown -> must NOT enter, and SILENTLY (no banner, no event)
    o1 = Orchestrator(cfg, TapeWriter(tmp_path / "b", sess), res, breeze=None)
    run_to_first_entry(o1, upto=fire_idx)
    seen = []
    _orig = o1._emit_event
    o1._emit_event = lambda t, p=None, ts_ms=None: (seen.append(t), _orig(t, p, ts_ms=ts_ms))[1]
    o1._last_profit_ms = fire_ts - 10_000
    st = o1.process_round(*rounds[fire_idx])
    assert not o1.executor.position_active, "entered despite the post-profit cooldown"
    assert not (st.get("arming") or {}).get("entry_block"), "post-profit cooldown must be SILENT (no banner)"

    # past the cooldown -> it DOES enter
    o2 = Orchestrator(cfg, TapeWriter(tmp_path / "c", sess), res, breeze=None)
    run_to_first_entry(o2, upto=fire_idx)
    o2._last_profit_ms = fire_ts - (pcool + 1_000)
    o2.process_round(*rounds[fire_idx])
    assert o2.executor.position_active, "did not enter after the post-profit cooldown expired"


def test_levels_stale_clears_after_nifty_gap_recovers(tmp_path):
    """council F9 regression: a NIFTY-missing round sets _levels_stale=True, but the recovery round
    (valid held band, no interval recompute due) must CLEAR it — otherwise the cockpit falsely shows
    LEVELS STALE for up to a full re-anchor interval after a transient blink."""
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    cal = build_calendar(cfg)
    sess = choose_synthetic_session(cal, date(2026, 6, 1))
    res = cal.resolve(sess)
    w = TapeWriter(tmp_path / "s", sess, schema_version=cfg.schema_version)
    orch = Orchestrator(cfg, w, res, breeze=None)
    rounds = list(generate_rounds(cfg, sess))
    warm = [(ts, sn) for ts, sn in rounds if sn.get("NIFTY") is not None][:60]
    for ts, sn in warm:
        orch.process_round(ts, sn)
    assert orch.states["NIFTY"].band is not None and orch._levels_stale is False
    last_ts = warm[-1][0]
    orch.process_round(last_ts + 250, {})                 # NIFTY feed gap -> stale + latch
    assert orch._levels_stale is True and orch._nifty_gap_on is True
    orch.process_round(last_ts + 500, warm[-1][1])        # recovery on a valid held band
    assert orch._levels_stale is False                    # must un-stick (was the regression)
    assert orch._nifty_gap_on is False
    w.close()


def test_same_day_restart_backs_up_parquet_not_truncates(tmp_path):
    """council HIGH: a same-day restart (daily token refresh) opened a new ParquetWriter that
    TRUNCATED the prior session's rows while jsonl kept appending. The prior parquet must be backed
    up (renamed .bak), never silently destroyed."""
    sess = date(2026, 6, 23)
    w1 = TapeWriter(tmp_path / "p", sess)
    w1.append("decision_tape", {"ts": 1, "instrument": "NIFTY"})
    w1.close()
    parquet = (tmp_path / "p" / sess.isoformat() / "decision_tape.parquet")
    assert parquet.exists()
    w2 = TapeWriter(tmp_path / "p", sess)            # same-day restart
    w2.append("decision_tape", {"ts": 2, "instrument": "NIFTY"})
    w2.close()
    baks = list((tmp_path / "p" / sess.isoformat()).glob("decision_tape.parquet.*.bak"))
    assert len(baks) == 1, "prior-session parquet was not preserved on restart"
    assert pq.read_table(baks[0]).num_rows == 1      # the first session's row survives in the backup
    assert pq.read_table(parquet).num_rows == 1      # the restart wrote a fresh, non-truncating file


def test_all_tapes_emitted_and_populated(tmp_path):
    part = _record(tmp_path)
    for name in S.PARQUET_TAPES:
        f = part / f"{name}.parquet"
        assert f.exists(), f"{name} not produced"
        t = pq.read_table(f)
        assert set(S.required_columns(name)) <= set(t.schema.names)
        assert t.num_rows > 0, f"{name} empty"
    assert (part / "event_tape.jsonl").exists()


def test_replay_is_deterministic(tmp_path):
    part = _record(tmp_path)
    r1 = replay_session(part, tmp_path / "r1", config_path=str(ROOT / "vajra_config.yaml"))
    r2 = replay_session(part, tmp_path / "r2", config_path=str(ROOT / "vajra_config.yaml"))
    d1 = (r1 / "decision_tape.jsonl").read_bytes()
    d2 = (r2 / "decision_tape.jsonl").read_bytes()
    assert d1 == d2 and len(d1) > 0
