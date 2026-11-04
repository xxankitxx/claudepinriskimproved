"""2.0.2 sound-council fixes: engine sound decisions, the Windows fallback guard, and the page contract."""

from datetime import date

import pytest

from vajra.alerts import SoundBus, ApproachEpisode, PRIORITY
from vajra.engine.trigger_engine import TriggerBand
from vajra.soundguard import SoundGuard
from tests.test_v2 import _nonexpiry_orch, _snap_at, _ms

D = date(2026, 10, 15)
BAND = TriggerBand("NIFTY", 22950, 22900, 1.0, 1.0, 22960, 23000, 1.0, 1.0, 22900, 0.0,
                   "IMPLIED", 0.1, 1, 5.0, True, None)


def _kinds(o):
    return [e["kind"] for e in o.latest_state["sound_events"]]


# ---- SoundBus ---------------------------------------------------------------------------------
def test_feed_alarm_is_never_lost_to_the_global_gap():
    b = SoundBus({})
    assert b.emit("approach", 1_000)
    assert b.emit("feed_alarm", 1_500)                 # 0.5 s later: an alarm still rings


def test_trade_cue_restarts_the_approach_cooldown():
    b = SoundBus({})
    assert b.emit("approach", 0)
    assert not b.emit("approach", 30_000)              # 120 s cooldown
    b.emit("exit_loss", 40_000)                        # a trade closed -> new setup
    assert b.emit("approach", 50_000)


def test_force_bypasses_cooldown_and_gap_and_armed_never_blocks():
    b = SoundBus({})
    assert b.emit("armed", 0)
    assert b.emit("approach", 100)                     # the sound check does not count for the gap
    assert b.emit("approach", 200, force=True)
    assert PRIORITY["exit_loss"] > PRIORITY["entry"] > PRIORITY["feed_alarm"] > PRIORITY["approach"] > PRIORITY["armed"]


def test_approach_episode_rearm():
    ep = ApproachEpisode(15, 8)
    assert ep.update(10, 22960)
    assert not ep.update(9, 22960)
    ep.rearm()
    assert ep.update(9, 22960)


# ---- orchestrator -------------------------------------------------------------------------------
def test_armed_chime_once_at_the_first_round(monkeypatch):
    o, w, res = _nonexpiry_orch()
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: BAND)
    o.process_round(_ms(D, 10, 0), {"NIFTY": _snap_at(22900 + 30)})
    o.process_round(_ms(D, 10, 0, 1), {"NIFTY": _snap_at(22900 + 30)})
    assert _kinds(o).count("armed") == 1


def test_entry_imminent_cue_before_a_stop_cooldown_ends(monkeypatch):
    o, w, res = _nonexpiry_orch()
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: BAND)
    t = _ms(D, 10, 0)
    o._last_stop_ms = t - o.post_stop_cooldown_ms + 5_000        # 5 s of cooldown left
    o.process_round(t, {"NIFTY": _snap_at(22948)})               # 12 pts from the upper level
    ev = [e for e in o.latest_state["sound_events"] if e["kind"] == "approach"]
    assert len(ev) == 1 and "cooldown ends in" in ev[0]["text"]
    o.process_round(t + 1_000, {"NIFTY": _snap_at(22948)})       # once per cooldown
    assert len([e for e in o.latest_state["sound_events"] if e["kind"] == "approach"]) == 1


def test_approach_not_used_up_while_a_trade_is_impossible(monkeypatch):
    """Monday: the episode was consumed silently during cooldowns, so 30+ of 40 entries had no warning."""
    o, w, res = _nonexpiry_orch()
    monkeypatch.setattr(o, "_build_band", lambda *a, **k: BAND)
    t = _ms(D, 10, 0)
    o._last_stop_ms = t - 60_000                                 # deep inside the 11-min cooldown
    o.process_round(t, {"NIFTY": _snap_at(22948)})
    assert "approach" not in _kinds(o)
    t2 = o._last_stop_ms + o.post_stop_cooldown_ms + 1_000
    o.process_round(t2, {"NIFTY": _snap_at(22948)})              # cooldown gone, still near: warns now
    assert "approach" in _kinds(o)


def test_zero_pnl_exit_is_not_a_profit_sound():
    import inspect
    import vajra.orchestrator as m
    src = inspect.getsource(m)
    assert src.count('"exit_profit" if pnl > 0 else "exit_loss"') == 3
    assert "pnl >= 0" not in src.replace("pnl >= 0.0 and", "")


# ---- engine sound watch (never plays) ---------------------------------------------------------------
def _ev(seq, kind, ts, text=""):
    return {"seq": seq, "kind": kind, "ts": ts, "text": text}


def test_watch_reports_missed_trade_cues_only_when_no_page_can_play():
    logs = []
    g = SoundGuard(log=logs.append)
    now = 1_000_000
    g.tick([], "b", now)                                                      # prime
    assert g.tick([_ev(1, "entry", now)], "b", now) == ["entry"]              # no page at all
    g.beacon({"snd": {"on": True, "locked": False, "leader": True}}, now)
    assert g.tick([_ev(1, "entry", now), _ev(2, "exit_loss", now)], "b", now + 10) == []
    later = now + 31_000                                                      # page silent > 30 s
    assert g.tick([_ev(3, "exit_profit", later)], "b", later) == ["exit_profit"]
    g.tick([_ev(3, "exit_profit", later)], "b", later + 31_000)
    assert any("NOT PLAYED" in l for l in logs) and any("NO DASHBOARD" in l for l in logs)


def test_watch_respects_off_and_never_plays():
    import vajra.soundguard as m
    assert "winsound" not in open(m.__file__).read().replace("never", "")
    g = SoundGuard(log=lambda s: None)
    g.tick([], "b", 0)
    g.beacon({"snd": {"muted": True, "leader": True}}, 0)
    assert g.tick([_ev(1, "entry", 0)], "b", 100) == [] and g.played_seqs() == []


# ---- page contract --------------------------------------------------------------------------------
def test_page_player_contract():
    from vajra import dashboard
    js = dashboard._PAGE
    assert "sndMode" not in js and "'trades'" not in js            # no "trades only" mode any more
    assert "vajra_sound'" not in js                                # the 1.0 key is ignored
    assert "Oscillator" not in js and "_beep" not in js and "new Audio(" not in js   # only MP3 files ever play
    assert "MUTE_MS" not in js                                     # OFF stays OFF until the operator turns it on
    for needle in ("vajra_snd_off", "armed:'armed.mp3'", "src.onended=fin", "(dur+0.5)*1000", "_stopCur(); _start(it)",
                   "cache:'no-store'", "'?v='+rev", "vajra_snd_leader", "_pendLocked",
                   "SOUND LOCKED", "id=\"sndbanner\"", "snd_outcomes", "_clkOff", "120000"):
        assert needle in js, needle


def test_sound_files_served_no_cache_and_rev_in_state():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from vajra.dashboard import build_app
    o, w, res = _nonexpiry_orch()
    c = TestClient(build_app(o))
    r = c.get("/sounds/entry.mp3")
    assert r.status_code == 200 and "no-cache" in r.headers.get("cache-control", "")
    st = c.get("/api/state").json()
    assert st["sounds_rev"] and "snd_fallback" in st
    assert c.post("/api/client", json={"snd": {"on": True, "leader": True}}).json()["ok"]


def test_every_cue_has_a_real_mp3():
    from pathlib import Path
    d = Path(__file__).resolve().parents[1] / "vajra" / "sounds"
    for n in ("entry", "exit_profit", "exit_loss", "approach_up", "approach_dn", "feed_alarm", "armed"):
        assert (d / f"{n}.mp3").stat().st_size > 15_000


# ---- 2.0.3: findings from the 21 Oct live tape ---------------------------------------------------
def test_closing_session_spot_ignores_the_indicative_index(monkeypatch):
    """2.0.4: from 15:15 the NIFTY spot is forward - basis_ref; the 15:20 indicative jump is ignored."""
    o, w, res = _nonexpiry_orch()
    o.process_round(_ms(D, 15, 10), {"NIFTY": _snap_at(22950)})      # clean pre-freeze basis
    o.process_round(_ms(D, 15, 16), {"NIFTY": _snap_at(22950)})
    bad = _snap_at(22950)
    bad.spot = 23190.0                                       # the 15:20 indicative jump
    o.process_round(_ms(D, 15, 20), {"NIFTY": bad})
    assert o.feed_health.last.ok and "feed_alarm" not in _kinds(o)
    assert abs(o.latest_state["nifty"]["spot"] - 22950) < 1 and o.latest_state["nifty"]["index_spot"] == 23190.0
    assert any(t == "ANOMALY" and (p or {}).get("kind") == "CLOSING_SESSION_SPOT"
               and p["basis_ref_source"] == "median_clean_basis" for t, p, _ in w.events)


def test_closing_session_without_pre_freeze_basis_blocks_entries(monkeypatch):
    o, w, res = _nonexpiry_orch()
    o.process_round(_ms(D, 15, 16), {"NIFTY": _snap_at(22950)})       # started after 15:15: no basis_ref
    assert w.rows["decision_tape"][-1]["reason_code"] in ("FEED_UNHEALTHY", "NO_LEVELS")
    assert not o.executor.position_active


def test_no_feed_alarm_for_the_nse_closing_session_freeze(monkeypatch):
    o, w, res = _nonexpiry_orch()
    o.closing = o.closing.__class__(**{**o.closing.as_dict(), "spot_proxy": False})   # proxy off: raw index
    t0 = _ms(D, 15, 16)
    o.process_round(t0, {"NIFTY": _snap_at(22950)})
    bad = _snap_at(22950)
    bad.spot = 23190.0                                       # the 15:20 indicative jump
    o.process_round(t0 + 250, {"NIFTY": bad})
    assert not o.feed_health.last.ok
    assert "feed_alarm" not in _kinds(o)
    assert any(t == "ANOMALY" and (p or {}).get("kind") == "CLOSING_SESSION_INDEX" for t, p, _ in w.events)


def test_big_jsonl_sidecar_dropped_only_when_parquet_verified(tmp_path):
    from vajra.tape import TapeWriter
    from vajra import schema as S
    tw = TapeWriter(tmp_path, D)
    tw.drop_verified_jsonl = True
    name = "decision_tape"
    cols = S.TAPE_SCHEMAS[name].names
    for i in range(300):
        tw.append(name, {c: None for c in cols} | {"ts": 1_000 + i})
    tw.event("SHUTDOWN", {"reason": "test"}, ts_ms=5)
    tw.close()
    part = tmp_path / D.isoformat()
    assert (part / f"{name}.parquet").exists() and not (part / f"{name}.jsonl").exists()
    assert (part / "event_tape.jsonl").exists()                       # small tapes always kept


def test_big_jsonl_kept_after_a_same_day_restart(tmp_path):
    from vajra.tape import TapeWriter
    from vajra import schema as S
    name = "decision_tape"
    cols = S.TAPE_SCHEMAS[name].names
    for _ in range(2):
        tw = TapeWriter(tmp_path, D)
        tw.drop_verified_jsonl = True
        tw.append(name, {c: None for c in cols} | {"ts": 1})
        tw.close()
    assert (tmp_path / D.isoformat() / f"{name}.jsonl").exists()      # earlier rows only in jsonl/.bak


def test_page_beacon_and_audio_resume_are_driven_by_the_stream():
    from vajra import dashboard
    js = dashboard._PAGE
    assert "function _sendBeacon()" in js and "_ctx.state==='suspended') _ctx.resume()" in js
    assert "Date.now()-_lastBeaconAt>=10000 && typeof _sendBeacon==='function') _sendBeacon();" in js


def test_silently_refused_nifty_subscription_is_detected_and_retried():
    import vajra.feed as F
    cls = [c for c in vars(F).values() if isinstance(c, type) and hasattr(c, "_verify_option_data")][0]
    f = cls.__new__(cls)
    f._subscribed_options, f._sub_sent_ms, f._sub_retry_at, f._sub_failed_reported = set(), {}, {}, set()
    f.sub_failures = 0
    events = []
    f._feed_event = lambda t, p, ts: events.append((t, p))
    f.breeze = type("B", (), {"_key": staticmethod(lambda c, k, s, r: f"{c}|{k}|{s}|{r}")})()
    t = _ms(D, 10, 0)
    key = ("NIFTY", 22500, "PE")
    f._subscribed_options.add(key); f._sub_sent_ms[key] = t - 40_000
    ok = ("NIFTY", 22550, "PE")
    f._subscribed_options.add(ok); f._sub_sent_ms[ok] = t - 40_000
    raw = {"NIFTY|option|22550|PE": {"ltp": 1.0}}
    f._verify_option_data("NIFTY", "NIFTY", [(22500, "PE"), (22550, "PE")], raw, t)
    assert key not in f._subscribed_options and f._sub_retry_at[key] == t + 60_000
    assert ok in f._subscribed_options and ok not in f._sub_sent_ms
    assert events and events[0][1]["stage"] == "subscribe_no_data"
