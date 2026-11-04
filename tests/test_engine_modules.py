"""Unit tests for the stateful engine modules (§5.4-§5.7)."""

from datetime import date, datetime, time as dtime
from vajra.timeutil import IST, basis_signal_weight
from vajra.engine.basis_monitor import BasisMonitor
from vajra.engine.continuation_validator import ContinuationValidator
from vajra.engine.decision_maker import DecisionMaker
from vajra.engine.continuation_validator import ContinuationSignal


def _ts(h, m, s=0):
    return int(datetime.combine(date(2026, 6, 16), dtime(h, m, s), tzinfo=IST).timestamp() * 1000)


# ---- §5.5 basis monitor ----------------------------------------------------
def test_basis_signal_weight_decay():
    assert basis_signal_weight(_ts(11, 0)) == 1.0
    assert basis_signal_weight(_ts(14, 30)) == 0.05          # floor after near_zero
    mid = basis_signal_weight(_ts(13, 15))                   # between 12:30 and 14:00
    assert 0.05 < mid < 1.0


def test_basis_noise_floor_ignored():
    import pytest
    bm = BasisMonitor(noise_floor_pts=1.0)
    bm.update(_ts(10, 0), 10.0)
    r = bm.update(_ts(10, 0, 30), 10.4)                      # 0.4 < noise floor
    assert r.abrupt_flag is False
    assert r.d_basis == pytest.approx(0.4)                   # raw delta still reported


def test_basis_abrupt_fires_on_jump():
    bm = BasisMonitor(abrupt_k=2.5, noise_floor_pts=0.5)
    v = 10.0
    t0 = _ts(10, 0)
    for i in range(20):                                      # build a quiet baseline
        v += 0.6 if i % 2 else -0.6
        bm.update(t0 + i * 30000, v)
    r = bm.update(t0 + 20 * 30000, v + 40.0)                # sudden premium spike
    assert r.abrupt_flag is True
    assert r.abrupt_dir == 1


# ---- §5.7 continuation -----------------------------------------------------
def test_breach_then_time_confirm_with_volume():
    # band wide enough that the neutral spot is outside the 0.05% linger zone
    cv = ContinuationValidator(break_buffer_pct=0.03, confirm_secs=120,
                               confirm_points_pct=0.10, confirm_vol_mult=1.2)
    t0 = _ts(10, 0)
    # inside band, far from both levels
    s = cv.update(t0, 24100, 24160, 24040, 1.0)
    assert s.state == "NEUTRAL"
    # breach up with volume behind it (beyond 24160 + buffer)
    s = cv.update(t0 + 10000, 24175, 24160, 24040, 1.6)
    assert s.state == "BREACH_UP" and s.breached
    # hold 120s -> confirm (volume latched from the break tick)
    s = cv.update(t0 + 130000, 24176, 24160, 24040, 1.0)
    assert s.state == "CONFIRM_UP" and s.confirmed


def test_false_break_snaps_back():
    cv = ContinuationValidator(confirm_secs=120, confirm_points_pct=0.10)
    t0 = _ts(10, 0)
    cv.update(t0, 24100, 24160, 24040, 1.0)
    cv.update(t0 + 10000, 24175, 24160, 24040, 1.6)         # breach
    s = cv.update(t0 + 20000, 24100, 24160, 24040, 1.0)     # snap back before confirm
    assert s.false_break is True
    assert s.state == "NEUTRAL"


# ---- §5.6 decision-maker ---------------------------------------------------
def _sig(state, d, strength, breached=False, confirmed=False):
    return ContinuationSignal(state, d, strength, breached, confirmed, False, 0.0, 24105.0, 0.0)


def _dm():
    return DecisionMaker(weight_table={"HDFCBANK": 0.1056}, w_self=1.0, w_bloc=0.35)


def test_fire_on_nifty_confirm_plus_bias():
    dm = _dm()
    sigs = {
        "NIFTY": _sig("CONFIRM_UP", 1, 0.9, True, True),
        "BANKNIFTY": _sig("CONFIRM_UP", 1, 0.8, True, True),
        "FINNIFTY": _sig("CONFIRM_UP", 1, 0.8, True, True),
    }
    r = dm.evaluate(_ts(10, 0), sigs, abrupt_against_bias=False, expiry_gate_ok=True,
                    routing_mode="CONFIRM", bias_threshold=0.25, bias_threshold_lead=0.55,
                    position_active=False)
    assert r.decision == "FIRE_UP"


def test_no_continuation_holds():
    dm = _dm()
    sigs = {"NIFTY": _sig("BREACH_UP", 1, 0.5, True, False)}
    r = dm.evaluate(_ts(10, 0), sigs, abrupt_against_bias=False, expiry_gate_ok=True,
                    routing_mode="CONFIRM", bias_threshold=0.25, bias_threshold_lead=0.55,
                    position_active=False)
    assert r.decision == "HOLD" and r.reason_code == "NO_CONTINUATION"


def test_position_active_blocks_new_entry():
    dm = _dm()
    sigs = {"NIFTY": _sig("CONFIRM_UP", 1, 0.9, True, True)}
    r = dm.evaluate(_ts(10, 0), sigs, abrupt_against_bias=False, expiry_gate_ok=True,
                    routing_mode="CONFIRM", bias_threshold=0.25, bias_threshold_lead=0.55,
                    position_active=True)
    assert r.decision == "HOLD" and r.reason_code == "POSITION_ACTIVE"


def test_reversal_freeze_blocks_fire():
    dm = _dm()
    sigs = {"NIFTY": _sig("CONFIRM_UP", 1, 0.9, True, True)}
    r = dm.evaluate(_ts(10, 0), sigs, abrupt_against_bias=True, expiry_gate_ok=True,
                    routing_mode="CONFIRM", bias_threshold=0.25, bias_threshold_lead=0.55,
                    position_active=False)
    assert r.decision == "HOLD" and r.reason_code == "REVERSAL_FREEZE"


def test_divergence_damps_but_can_still_fire():
    dm = _dm()
    sigs = {
        "NIFTY": _sig("CONFIRM_UP", 1, 0.9, True, True),
        "BANKNIFTY": _sig("BREACH_DOWN", -1, 0.8, True, False),
        "FINNIFTY": _sig("BREACH_DOWN", -1, 0.8, True, False),
    }
    r = dm.evaluate(_ts(10, 0), sigs, abrupt_against_bias=False, expiry_gate_ok=True,
                    routing_mode="CONFIRM", bias_threshold=0.25, bias_threshold_lead=0.55,
                    position_active=False)
    assert r.divergence_flag is True
    assert r.decision == "FIRE_UP"
    assert r.reason_code == "DIVERGENCE_DAMPED"


def test_nonexpiry_gate_blocks():
    dm = _dm()
    sigs = {"NIFTY": _sig("CONFIRM_UP", 1, 0.9, True, True)}
    r = dm.evaluate(_ts(10, 0), sigs, abrupt_against_bias=False, expiry_gate_ok=False,
                    routing_mode="CONFIRM", bias_threshold=0.25, bias_threshold_lead=0.55,
                    position_active=False)
    assert r.decision == "HOLD" and r.reason_code == "EXPIRY_GATE_BLOCKED"
