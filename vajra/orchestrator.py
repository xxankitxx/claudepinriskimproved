"""Orchestrator — wires §4 feed -> §5 engine -> §6 routing -> §7 execution -> §9 tape.

One evaluation *round* per feed round; every instrument in the round shares the
round timestamp.  Per round it: computes basis, locates the pin, (re)builds the
fresh trigger band on a freshness decision, monitors basis change, validates
breach+continuation for EVERY instrument, assembles cross-instrument conviction,
applies the §6 fire gate, and routes a NIFTY-only order — emitting every tape.

The engine is deterministic in the (ts, snapshot) stream, so replay.py over a
recorded tick_tape reproduces decision_tape byte-for-byte (§11.5).
"""

from __future__ import annotations

import os
import math
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace as _dc_replace

from .mathx import RollingStats
from .marketdata import MarketSnapshot
from .schema import Reason
from .expiry import ExpiryResolution
from .timeutil import hhmm_reached
from .engine.basis_engine import compute_basis
from .engine.pin_locator import locate_pin
from .engine.trigger_engine import compute_band, compute_band_v2, TriggerBand
from .engine.freshness_engine import FreshnessEngine
from .engine.basis_monitor import BasisMonitor
from .engine.continuation_validator import ContinuationValidator, ContinuationSignal
from .engine.decision_maker import DecisionMaker
from .execution import Executor
from .regime import resolve_regime
from .feed_health import FeedHealth
from .alerts import SoundBus, ApproachEpisode
from .closing import resolve_closing, parity_forward


def _sign(x: float) -> int:
    return 1 if x > 1e-9 else (-1 if x < -1e-9 else 0)


def _heat(state: str, dist_up, dist_dn, spot: float) -> float:
    """0..1 'about to burst' score for ranking the universe by pin-risk proximity."""
    if "CONFIRM" in state:
        return 1.0
    if "BREACH" in state:
        return 0.88
    if "APPROACH" in state:
        return 0.62
    if dist_up is None or dist_dn is None:
        return 0.0
    nearest = min(abs(dist_up), abs(dist_dn))
    p = nearest / max(spot, 1.0)
    return max(0.0, min(0.45, 0.45 * (1.0 - p / 0.004)))   # within ~0.4% of a level = warming


@dataclass
class InstrumentState:
    instrument: str
    is_index: bool
    step: float
    freshness: FreshnessEngine
    basis_mon: BasisMonitor
    continuation: ContinuationValidator
    activity: RollingStats
    band: TriggerBand | None = None
    last_total_vol: float | None = None
    last_basis: float = 0.0


class Orchestrator:
    def __init__(self, cfg, tape, expiry: ExpiryResolution, breeze=None):
        self.cfg = cfg
        self.tape = tape
        self._breeze = breeze
        self._round_t0 = time.perf_counter()
        self.expiry = expiry
        self.routing_mode = cfg.routing_mode
        self.nonexpiry_mode = cfg.nonexpiry_mode
        self._stop = threading.Event()

        # thresholds (provisional placeholders are explicit in config, never hidden)
        self.bias_threshold = float(cfg.get("decision", "bias_threshold", default=0.25))
        self.bias_threshold_lead = float(cfg.get("decision", "bias_threshold_lead", default=0.55))
        # §5.5 time-weighting: only let an ABRUPT basis move trigger the reversal
        # freeze while the basis signal is still meaningful (weight >= floor); near
        # the close convergence makes the basis near-useless so it must not freeze.
        self.reversal_weight_floor = float(cfg.get("basis", "reversal_weight_floor", default=0.5))
        self.caution_after = str(cfg.get("execution", "caution_after", default="14:30"))
        self._caution_emitted = False
        # Session timing (operator rule): pre-open levels are SEEDED from the prev-day close;
        # the first INTRADAY recompute is at calc_start (09:20), then every 20 min; NO trade
        # until trade_start (see vajra_config.yaml — currently 09:21). Pre-open frames (~08:19)
        # are never processed.
        self.calc_start = str(cfg.get("expiry", "calc_start", default="09:20"))
        # 2.0: code defaults now EQUAL the operator-locked yaml values (1.x silently fell back to
        # superseded rules — 09:30 / 180 s / 120 s — whenever a key was missing; team finding 14).
        self.trade_start = str(cfg.get("expiry", "trade_start", default="09:21"))
        self.flatten_at = str(cfg.get("execution", "flatten_before", default="15:25"))
        # Operator rule (2.0.4): entries until 15:29, square-off 15:38 -- see closing_session below.
        # The garbage 15:15+ feed is handled by the feed-sanity gate, not by an earlier cutoff.
        self.strike_from = str(cfg.get("execution", "strike_from", default="current_atm")).lower()
        self.entry_cutoff = str(cfg.get("execution", "last_entry", default=self.flatten_at))
        # 2.0.4: NSE closing auction -- last entry / square-off / index-frozen time are resolved per
        # session date (+ expiry-day overrides) from closing_session in vajra_config.yaml.
        self.closing = resolve_closing(cfg, expiry.session_date, expiry.is_expiry_day)
        self.flatten_at = self.closing.flatten_before
        self.entry_cutoff = self.closing.last_entry
        if self.entry_cutoff > self.flatten_at:      # an entry after the flatten would be flattened at once (T29)
            self.entry_cutoff = self.flatten_at
        # post-stop cooldown: after a hard-stop (SL_HIT) loss, suppress new entries for this long.
        self.post_stop_cooldown_ms = int(float(cfg.get("execution", "post_stop_cooldown_secs", default=660)) * 1000)
        # silent cooldown after a PROFITABLE exit; fresh levels are recomputed when it ends.
        self.post_profit_cooldown_ms = int(float(cfg.get("execution", "post_profit_cooldown_secs", default=15)) * 1000)

        # ---- VAJRA 2.0: regime, feed health, sounds, event hygiene --------------------------
        cal = None
        try:
            from .app import build_calendar           # lazy: app imports this module
            cal = build_calendar(cfg)
        except Exception as _e:                      # label-only fallback (dte), but never silently
            print(f"  vajra · WARNING: calendar build failed ({_e!r}); trading-days-to-expiry label "
                  f"falls back to calendar days", file=sys.stderr, flush=True)
            cal = None
        self.regime = resolve_regime(cfg, expiry, cal)
        ne = cfg.get("nonexpiry", default={}) or {}
        self._band_kw = {
            "pad_floor": float(cfg.get("trigger", "pad_floor", default=0.10)),
            "level_round_step": float(cfg.get("trigger", "level_round_step", default=5)),
            "itm_depth_strikes": int(cfg.get("trigger", "itm_depth_strikes", default=1)),
        }
        self._mtm_kw = {
            "mtm_pad": float(ne.get("mtm_pad", 0.20)),
            "mtm_floor_pts": float(ne.get("mtm_floor_pts", 25)),
            "mtm_delta_fallback": float(ne.get("mtm_delta_fallback", 0.65)),
            "max_half_width_pts": float(ne.get("max_half_width_pts", 0) or 0),
        }
        fs = cfg.get("feed_sanity", default={}) or {}
        self.feed_health = FeedHealth(
            frozen_secs=float(fs.get("frozen_spot_secs", 25)),
            max_abs_basis=float(fs.get("max_abs_basis_pts", 150)),
            max_jump_pts=float(fs.get("max_jump_pts", 40)),
            recovery_secs=float(fs.get("recovery_secs", 60)),
            jump_recovery_secs=float(fs.get("jump_recovery_secs", 2)),
            jump_divergence_pts=float(fs.get("jump_divergence_pts", 25)),
            rel_basis_pts=float(fs.get("rel_basis_pts", 20)),
            rel_basis_hold_secs=float(fs.get("rel_basis_hold_secs", 2)),
            enabled=str(fs.get("enabled", "on")).lower() not in ("off", "false", "0", "no"),
        )
        snd = cfg.get("sounds", default={}) or {}
        self.sounds = SoundBus(snd)
        self.sounds.sink = self.tape.event
        self._boot_id = f"{os.getpid()}-{int(time.time() * 1000)}"   # per-process id: dashboard resets its sound cursor on restart
        self.hold_while_in_trade = str(cfg.get("freshness", "hold_while_in_trade", default="off")).lower() not in ("off", "false", "0", "no")
        self.reversal_min_bias = float(cfg.get("basis", "reversal_min_bias", default=0.125))
        self.reversal_refractory_ms = int(float(cfg.get("basis", "reversal_refractory_secs", default=300)) * 1000)
        self.false_break_refractory_ms = int(float(cfg.get("continuation", "false_break_refractory_secs", default=60)) * 1000)
        self._last_reversal_ms = None
        self._last_false_break_ms = None
        self._last_alert_key = None                 # (type, direction/reason) of the last APPROACH/REJECT event
        self._last_alert_ms = None
        self._err_on: dict = {}                    # per-instrument INSTRUMENT_ERROR latch (1.x re-fired every round)
        self._entries_today = 0
        self._entry_gate: str | None = None        # first closed gate this round (idle diagnostics)
        self._health = None
        self._round_err_seen: dict = {}
        self._alert_last: dict = {}
        self._health_reason_logged = None
        self._health_changed_ms = 0
        self._reject_seen: dict = {}
        self._reanchor_owed = False     # a NIFTY re-anchor was blocked by the feed gate -> do it on re-open
        self._nearest_dist = None

        # decision-maker weights from the weights file (stocks).
        weight_table = {inst: w.decision_weight for inst, w in cfg.weights.items()
                        if w.type == "stock"}
        # LOUD startup check: a tracked stock with no weight row gets decision weight 0.0 -> ZERO
        # cross-instrument voice, silently. Surface it (council: TCS was in the universe + ws-map but
        # missing from nifty_weights.csv). Real fix = add its NSE factsheet weight to the CSV.
        self._missing_weights = [s for s in cfg.stocks if not weight_table.get(s, 0.0)]
        if self._missing_weights:
            print(f"[VAJRA WARNING] {len(self._missing_weights)} tracked stock(s) have NO decision "
                  f"weight (zero cross-instrument voice): {', '.join(self._missing_weights)} — add "
                  f"their NSE factsheet weight to nifty_weights.csv.", file=sys.stderr, flush=True)
            # Tape-durable too (R19: "loud" must be provable from the tape alone, stderr vanishes
            # with the console). Direct tape.event — the cockpit strip deque isn't built yet here.
            self.tape.event("ANOMALY", {"kind": "MISSING_WEIGHT",
                                        "instruments": list(self._missing_weights),
                                        "note": "zero cross-instrument voice; add NSE factsheet "
                                                "weight to nifty_weights.csv"})
        self.decision = DecisionMaker(
            weight_table=weight_table,
            w_self=float(cfg.get("decision", "w_self", default=1.0)),
            w_bloc=float(cfg.get("decision", "w_bloc", default=0.35)),
            bias_ewma_halflife_min=float(cfg.get("decision", "bias_ewma_halflife_min", default=3.0)),
            divergence_penalty=float(cfg.get("decision", "divergence_penalty", default=0.5)),
            reversal_cooldown_min=float(cfg.get("decision", "reversal_cooldown_min", default=2.0)),
        )
        self.executor = Executor(cfg, breeze=breeze)
        self.executor.flatten_before = self.flatten_at     # 2.0.4: one square-off time for engine + executor

        self.states: dict[str, InstrumentState] = {}
        for inst in cfg.universe:
            self.states[inst] = self._make_state(inst)
        # REGIME is logged on the first calculated round with THAT round's timestamp (1.x-style
        # wall-clock stamping made replays non-reproducible and wrote a wrong-date row; T10).
        self._regime_logged = False

        # shared, read-only-ish snapshot for the dashboard.
        self.latest_state: dict = {"round_ts": None, "instruments": {}, "decision": None,
                                   "position": None, "events": [], "version": "2.0",
                                   "boot_id": self._boot_id,
                                   "session_date": self.expiry.session_date.isoformat(),
                                   "expiry": {"date": self.expiry.expiry_date.isoformat(),
                                              "basis_mode": self.expiry.basis_mode,
                                              "is_expiry_day": self.expiry.is_expiry_day,
                                              "is_monthly": self.expiry.is_monthly,
                "shifted_from_holiday": _shift_label(self.expiry)},
                                   "regime": {**self.regime.as_dict(), "last_entry": self.entry_cutoff, "closing": self.closing.as_dict(),
                                              "entries_today": 0},
                                   "live_orders": self.cfg.live_orders,
                                   "sound_events": []}
        self._position_dir: str | None = None
        # ENTRY = anticipation (operator rule): fire when NIFTY is within this many points
        # of a HELD level, BEFORE it breaches, so we buy the option cheap ahead of the
        # gamma explosion. No breach/continuation/bias gate.
        self.entry_proximity_pts = float(cfg.get("entry", "proximity_pts", default=7.0))
        self._imminent_ms = int(float(snd.get("imminent_secs", 10)) * 1000)
        self.closing_session_start = self.closing.index_frozen_from
        # 2.0.4 closing-session spot proxy (see vajra/closing.py): clean pre-freeze NIFTY basis history,
        # the frozen reference basis, the last proxy spot and the raw index print of this round.
        _cs = cfg.get("closing_session", default={}) or {}
        self._basis_ref_window_ms = int(float(_cs.get("basis_ref_window_mins", 10)) * 60_000)
        self._clean_basis: deque = deque()
        self._basis_ref: float | None = None
        self._proxy_spot: float | None = None
        self._proxy_on = False
        self._basis_ref_src: str | None = None
        self._parity_basis: float | None = None
        self._index_spot: dict = {}
        self._imminent_key = None
        self._approach_ep = ApproachEpisode(max(self.entry_proximity_pts,
                                                float(snd.get("approach_warn_pts", 15))),
                                            float(snd.get("approach_rearm_pts", 8)))
        # Weighted same-side pull above which the cross-instrument SCREAM reads STRONG (vs a
        # raw head-count). Display/label only — never gates or delays the entry (no-veto rule).
        self.strong_pull = float(cfg.get("entry", "strong_pull", default=0.35))
        self._entry_block = None                    # last round's entry outcome (for the scream)
        self._last_stop_ms = 0                       # ts of the last SL_HIT loss -> post-stop cooldown anchor
        self._last_profit_ms = 0                     # ts of the last profitable exit -> silent post-profit cooldown
        self._stop_cooldown_active = False           # edge tracker: recompute fresh levels when the 660s ends
        self._profit_cooldown_active = False         # edge tracker: recompute fresh levels when the 120s ends
        self._last_decision: dict | None = None    # authoritative NIFTY decision for the cockpit badge
        self._last_confluence: dict | None = None  # cross-instrument read logged with the open trade
        self._recent_events: deque = deque(maxlen=40)   # last events mirrored to the cockpit strip
        self._levels_stale = False                 # NIFTY band recompute failed -> held levels are stale
        self._basis_gap: dict = {}                 # per-instrument last basis-gap reason (edge-triggered alerts)
        self._reversal_on: dict = {}               # per-instrument abrupt-basis latch (edge-triggered)
        self._false_break_on: dict = {}            # NIFTY false-break latch (edge-triggered)
        self._nifty_gap_on = False                 # NIFTY-missing-round latch (edge-triggered alert)
        self._manage_gap_on = False                # 2.0: manage-quote FEED_GAP latch
        self._inst_last: dict = {}                 # last-known instruments_view per instrument (universe completeness)
        self._breach_since: dict = {}              # inst -> ts the CURRENT breach started (for the breach-time display)
        self._breach_side: dict = {}               # inst -> current breach side (edge detection for the stamp)
        self._nifty_band_inputs = None             # (snap, atm, basis, mode, step) for the post-exit recompute
        self.journal: list[dict] = []              # ROUND-TRIP trades (one row each) for the dashboard
        self._open_trade: dict | None = None
        self.trades_log: list[dict] = []           # every order row taken this session (order-level)
        self._prev_spot: dict[str, float] = {}     # per-instrument last spot (for deltas)
        self._nifty_head: dict = {}                # NIFTY spot/future/basis headline
        self._nifty_chain: list = []               # NIFTY option-chain ladder for the dashboard
        self._level_anchor: dict | None = None     # current NIFTY level calc (ts + inputs + levels)
        self._level_anchor_prev: dict | None = None  # the PREVIOUS NIFTY level calc (for the panel)
        self._level_history: list[dict] = []       # FULL log of every NIFTY recompute (time + inputs)
        # 2.1 power hygiene (2 Nov: laptop switched off mid-trade). app.py sets power_status_fn in live mode;
        # it is sampled every ~30 s on the engine thread and an on-battery edge is loud (event + alarm cue).
        self.power_status_fn = None
        self._power: dict = {}
        self._power_round = 0
        self._power_on_battery = False

    def _make_state(self, inst: str) -> InstrumentState:
        cfg = self.cfg
        is_index = inst in cfg.indices
        # NIFTY (the execution instrument) re-anchors on the 20-min rule — its level drives trades.
        # Sisters/stocks re-anchor on a SHORTER cadence (sister_hold_min_secs, 3 min) so their
        # pin-risk levels stay current for the cross-instrument confluence/lean — a breach against a
        # stale level is not predictive. (operator: "not noisy but very predictive".)
        hold = (float(cfg.get("freshness", "hold_min_secs", default=1200)) if inst == "NIFTY"
                else float(cfg.get("freshness", "sister_hold_min_secs", default=180)))
        return InstrumentState(
            instrument=inst, is_index=is_index, step=cfg.strike_step(inst),
            freshness=FreshnessEngine(
                hold_min_secs=hold,
                retry_secs=float(cfg.get("freshness", "retry_secs", default=30)),
            ),
            basis_mon=BasisMonitor(
                ewma_halflife_min=float(cfg.get("basis", "ewma_halflife_min", default=5)),
                abrupt_k=float(cfg.get("basis", "abrupt_k", default=2.5)),
                noise_floor_pts=float(cfg.get("basis", "noise_floor_pts", default=1.0)),
                full_until=cfg.get("basis", "signal_weight_decay", "full_until", default="12:30"),
                near_zero_after=cfg.get("basis", "signal_weight_decay", "near_zero_after", default="14:00"),
            ),
            continuation=ContinuationValidator(
                break_buffer_pct=float(cfg.get("continuation", "break_buffer_pct", default=0.03)),
                confirm_secs=float(cfg.get("continuation", "confirm_secs", default=120)),
                confirm_points_pct=float(cfg.get("continuation", "confirm_points_pct", default=0.10)),
                confirm_vol_mult=float(cfg.get("continuation", "confirm_vol_mult", default=1.2)),
                linger_band_pct=float(cfg.get("freshness", "linger_band_pct", default=0.05)),
            ),
            activity=RollingStats(window=int(cfg.get("continuation", "vol_window", default=10))),
        )

    # ---- VAJRA 2.0: one band builder for every call site ---------------------
    def _build_band(self, inst, snap, atm, basis, mode, step) -> TriggerBand:
        """NIFTY: the regime's method (expiry day = locked 1.x formula; non-expiry = MTM,
        narrow-only). Sisters/stocks: the 1.x formula, but rounded to 0.1 x their own strike step
        (1.x rounded a 5-pt-strike stock to 5 pts, so bands were 'born breached' — finding 28)."""
        if inst == "NIFTY" and (self.regime.is_expiry_day or self.regime.band_method == "legacy") \
                and not (self._mtm_kw.get("max_half_width_pts") and not self.regime.is_expiry_day):
            # expiry day (operator-locked) / legacy: the exact 1.x call, nothing added
            return compute_band(snap, atm, basis, mode, step, **self._band_kw)
        if inst == "NIFTY":
            return compute_band_v2(snap, atm, basis, mode, step, method=self.regime.band_method,
                                   **self._band_kw, **(self._mtm_kw if not self.regime.is_expiry_day
                                                       else {"max_half_width_pts": 0.0}))
        kw = dict(self._band_kw)
        kw["level_round_step"] = max(0.05, min(kw["level_round_step"], 0.1 * float(step)))
        return compute_band(snap, atm, basis, mode, step, **kw)

    # ---- per-instrument volume activity ratio (§5.7 volume gate) -----------
    def _vol_ratio(self, st: InstrumentState, snap: MarketSnapshot) -> float:
        total = float(sum(q.volume for q in snap.strikes.values()))
        if st.last_total_vol is None:
            st.last_total_vol = total
            delta = 0.0
        else:
            delta = max(0.0, total - st.last_total_vol)
            st.last_total_vol = total
        ratio = 1.0
        if len(st.activity) >= 2:
            m = st.activity.mean()
            ratio = (delta / m) if m > 0 else 1.0
        st.activity.push(delta)
        return ratio

    def _basis_mode_for(self, inst: str, is_index: bool) -> str:
        # Indices follow the session resolution; stocks use IMPLIED parity.
        return self.expiry.basis_mode if is_index else "IMPLIED"

    def _expiry_gate_ok(self) -> bool:
        if self.expiry.is_expiry_day:
            return True
        mode = self.nonexpiry_mode
        if mode == "off":
            return False
        if mode == "trade_small":
            return True
        return False   # observe -> track only, no fire

    def _emit_event(self, event_type, payload, ts_ms=None):
        """Write an event to the binding event_tape AND mirror a compact summary to the
        cockpit's recent-events strip, so a live operator sees feed gaps / rejections /
        stale levels instead of an unexplained frozen screen."""
        bits = [str(payload[k]) for k in ("instrument", "kind", "stage", "reason", "direction")
                if isinstance(payload, dict) and payload.get(k) is not None]
        summary = " ".join(bits)
        # COCKPIT STRIP ONLY: collapse CONSECUTIVE identical events into one row with a running
        # count (2026-07-06: 1,047 REVERSAL_WATCH episodes drowned every FEED_GAP out of the 40-slot
        # strip). This is run-length display dedup — no timer, no threshold, and the binding
        # event_tape below still records EVERY event individually.
        last = self._recent_events[-1] if self._recent_events else None
        if last and last["type"] == event_type and last["summary"] == summary:
            last["count"] = last.get("count", 1) + 1
            last["ts"] = ts_ms                        # strip shows the LATEST occurrence time
        else:
            self._recent_events.append({"ts": ts_ms, "type": event_type, "summary": summary, "count": 1})
        self.tape.event(event_type, payload, ts_ms=ts_ms)

    def _emit_alert_event(self, event_type, key, payload, ts_ms, min_interval_ms=60_000):
        """APPROACH_ALERT / ENTRY_REJECTED / EXIT_REJECTED: 1.x re-fired these every round while a
        reject persisted (e.g. premium floor) -> emit only when (type, key) changes or once a minute."""
        # 2.0.1 (T21): the last key/time is kept PER EVENT TYPE (a shared one let an alternating
        # APPROACH_ALERT / ENTRY_REJECTED pair both fire every round: 684 + 684 in 6 minutes)
        last = self._alert_last.get(event_type)
        if last is not None and last[0] == key and ts_ms - last[1] < min_interval_ms:
            return
        self._alert_last[event_type] = (key, ts_ms)
        self._emit_event(event_type, payload, ts_ms=ts_ms)

    # ---- main round --------------------------------------------------------
    @staticmethod
    def _is_reversal(abrupt, abrupt_dir, prev_bias, signal_weight, weight_floor) -> bool:
        """§5.6 reversal warning: an abrupt NIFTY basis move that CONTRADICTS the prevailing bias
        (abrupt_dir opposes sign(bias)), carrying enough weight. A WITH-bias abrupt (continuation)
        is NOT a reversal and must not freeze entries / mislabel the decision_tape (council HIGH)."""
        return bool(abrupt and _sign(prev_bias) != 0 and abrupt_dir != 0
                    and abrupt_dir != _sign(prev_bias) and signal_weight >= weight_floor)

    def _process_instrument(self, inst, snap, st, round_ts, signals, instruments_view) -> dict:
        """Per-instrument round work: basis -> pin -> freshness/band -> continuation -> view + tapes.
        Returns the NIFTY accumulators (empty dict for any non-NIFTY instrument). The CALLER wraps
        this in try/except so a single bad tick on ONE instrument can NEVER end the whole trading
        day (council CRITICAL: int(round_to(NaN)) used to crash process_round and finalize the run)."""
        # A non-finite spot (NaN/inf from a bad tick) crashes int(round_to(spot)) downstream -> skip
        # the instrument with a clean gap rather than letting it kill the round.
        if snap.spot is None or not math.isfinite(snap.spot):
            if self._basis_gap.get(inst) != "NONFINITE_SPOT":
                self._emit_event("FEED_GAP", {"instrument": inst, "reason": "NONFINITE_SPOT",
                                             "stage": "snapshot"}, ts_ms=round_ts)
                self._basis_gap[inst] = "NONFINITE_SPOT"
            signals[inst] = ContinuationSignal("NEUTRAL", 0, 0.0, False, False, False, 0.0, None, 0.0)
            return {}
        step = self.cfg.strike_step(inst, snap.inferred_step())
        mode = self._basis_mode_for(inst, st.is_index)

        # §5.1 basis
        br = compute_basis(snap, step, mode)
        if not br.ok:
            if self._basis_gap.get(inst) != br.reason:      # alert ONCE per gap episode (not every round)
                self._emit_event("FEED_GAP", {"instrument": inst, "reason": br.reason,
                                             "stage": "basis"}, ts_ms=round_ts)
                self._basis_gap[inst] = br.reason
            # cannot proceed for this instrument this round
            signals[inst] = ContinuationSignal("NEUTRAL", 0, 0.0, False, False, False, 0.0, None, 0.0)
            return {}
        self._basis_gap.pop(inst, None)                     # recovered -> a future gap re-alerts
        basis = br.basis
        if inst == "NIFTY":
            hs = self.feed_health.update(round_ts, snap.spot, basis)
            self._health = hs
            if hs.started:
                self._emit_event("FEED_GAP", {"instrument": "NIFTY", "stage": "feed_sanity",
                                             "reason": hs.reason, "spot": snap.spot,
                                             "basis": round(basis, 2),
                                             "spot_unchanged_secs": round(hs.spot_unchanged_secs, 1)},
                                 ts_ms=round_ts)
                # 2.0.3 (21 Oct tape): from 15:15 NSE's closing session freezes the NIFTY index and then
                # publishes indicative values (15:20 jump of ~+500 pts) EVERY day. That is market structure,
                # not a broken feed: log it, never ring the alarm for it.
                if hhmm_reached(round_ts, self.closing_session_start):
                    self._emit_event("ANOMALY", {"kind": "CLOSING_SESSION_INDEX", "reason": hs.reason,
                                                 "note": "NSE closing session: NIFTY index frozen/indicative "
                                                         "after 15:15 -- expected, no alarm"}, ts_ms=round_ts)
                else:
                    self.sounds.emit("feed_alarm", round_ts, f"NIFTY feed {hs.reason}")
            elif hs.ended:
                self._emit_event("FEED_HEALTH", {"instrument": "NIFTY", "status": "RECOVERED"}, ts_ms=round_ts)
            elif (not hs.ok and hs.reason not in (None, "RECOVERING")
                  and hs.reason != self._health_reason_logged
                  and round_ts - self._health_changed_ms >= 10_000):   # fault CHANGED mid-episode (T21), <= 1 per 10 s
                self._health_changed_ms = round_ts
                self._emit_event("FEED_HEALTH", {"instrument": "NIFTY", "status": "CHANGED", "reason": hs.reason,
                                                 "spot": snap.spot, "basis": round(basis, 2)}, ts_ms=round_ts)
            self._parity_basis = None
            if not self._proxy_on and not hhmm_reached(round_ts, self.closing.index_frozen_from):
                # 2.0.4 basis_ref source: the PARITY forward minus the index (the same forward the
                # closing-session proxy uses; option LTPs tick far more often than the future's)
                _pf = parity_forward(snap, step, snap.spot)
                if _pf is not None and math.isfinite(_pf):
                    self._parity_basis = round(_pf - snap.spot, 3)
                    if hs.ok:
                        self._clean_basis.append((round_ts, self._parity_basis))
                while self._clean_basis and round_ts - self._clean_basis[0][0] > self._basis_ref_window_ms:
                    self._clean_basis.popleft()
            if hs.started or (not hs.ok and hs.reason not in (None, "RECOVERING")):
                self._health_reason_logged = hs.reason
            elif hs.ok:
                self._health_reason_logged = None

        # §9.1 tick_tape
        self.tape.append("tick_tape", self._tick_row(round_ts, snap, br))

        # §5.5 basis monitor -> §9.3 basis_tape
        bm = st.basis_mon.update(round_ts, basis)
        self.tape.append("basis_tape", {
            "ts": round_ts, "instrument": inst, "spot": snap.spot,
            "index_spot": self._index_spot.get(inst, snap.spot),
            "spot_source": "CLOSING_PROXY" if inst in self._index_spot else "INDEX",
            "parity_basis": self._parity_basis if inst == "NIFTY" else None,
            "future": br.future, "basis": basis, "basis_mode": mode,
            "d_basis": bm.d_basis, "d_basis_ewma": bm.d_basis_ewma,
            "abrupt_flag": bm.abrupt_flag, "basis_signal_weight": bm.signal_weight,
        })
        # (REVERSAL_WATCH is emitted AFTER the per-instrument loop, gated on abrupt_against_bias — a
        # real reversal vs. the prevailing bias — not on the bare 4x/sec abrupt_flag flicker.)

        # §5.2 pin locator
        pin = locate_pin(snap, br.future, step)

        # §5.4 freshness -> (re)build §5.3 band -> §9.2 level_tape
        fd = st.freshness.update(round_ts, snap.spot)
        # §5.4 (operator rule): FIXED-interval re-anchor — first calc at 09:20, then every 20 min
        # (held in between so a moving spot can break a fixed level). Plus post-exit + cooldown-end.
        did_recompute = False
        need = fd.need_recompute
        if (inst == "NIFTY" and self._reanchor_owed and self._health is not None and self._health.ok
                and not (self.hold_while_in_trade and self.executor.position_active and st.band is not None)):
            need = True             # 2.0.1 (T03): a re-anchor the feed gate blocked is done the moment
            #                         the gate re-opens -- never enter on a level that went stale meanwhile
        if need and inst == "NIFTY":
            # 2.0: never anchor NIFTY levels on an implausible / frozen feed (1.x re-anchored on the
            # 15:22 garbage tick and then bought a strike 192 pts ITM), and do not move the band we
            # entered on while the trade is open.
            if self._health is not None and not self._health.ok:
                st.freshness.mark_attempt(round_ts)
                self._reanchor_owed = True
                need = False
            elif (self.hold_while_in_trade and self.executor.position_active
                  and fd.recalc_trigger == "interval" and st.band is not None):
                need = False
        if need:
            nb = self._build_band(inst, snap, br.atm_strike, basis, mode, step)
            if nb.ok:                       # NEVER arm the fixed-interval hold on a failed (None-level)
                st.band = nb               # band — keep the held band and retry next round.
                st.freshness.mark_recomputed(round_ts)
                did_recompute = True
                if inst == "NIFTY":
                    self._reanchor_owed = False
            else:
                # Re-anchor FAILED (feed gap): record the attempt so we retry on the short cadence
                # (~30s) and DON'T re-fire every round (else the age grows unbounded -> phantom recalcs).
                st.freshness.mark_attempt(round_ts)

        band = st.band
        # §9.2 level_tape — record the U/L levels EVERY round (held or freshly re-anchored).
        if band is not None:
            self.tape.append("level_tape", self._level_row(
                round_ts, inst, band, pin,
                fd.recalc_trigger if did_recompute else "held",
                fd.freshness_age_secs))

        # §5.7 continuation + state
        vr = self._vol_ratio(st, snap)
        sig = st.continuation.update(round_ts, snap.spot,
                                     band.upper_level if band else None,
                                     band.lower_level if band else None, vr)
        signals[inst] = sig
        # FALSE_BREAK only for NIFTY, edge-triggered: sister/stock signals jitter (illiquid).
        if inst == "NIFTY":
            if sig.false_break and not self._false_break_on.get(inst):
                if (self._last_false_break_ms is None
                        or round_ts - self._last_false_break_ms >= self.false_break_refractory_ms):
                    self._emit_event("ANOMALY", {"kind": "FALSE_BREAK", "instrument": inst,
                                                "spot": snap.spot}, ts_ms=round_ts)
                    self._last_false_break_ms = round_ts
                self._false_break_on[inst] = True
            elif not sig.false_break:
                self._false_break_on[inst] = False
        prev = self._prev_spot.get(inst)
        delta = (snap.spot - prev) if prev is not None else 0.0
        self._prev_spot[inst] = snap.spot

        weight = self.decision.applied_weight(inst) if inst in self.cfg.stocks else (
            self.decision.w_self if inst == "NIFTY" else self.decision.w_bloc)

        upper = band.upper_level if band else None
        lower = band.lower_level if band else None
        dist_up = round(upper - snap.spot, 2) if upper is not None else None   # +: below upper
        dist_dn = round(snap.spot - lower, 2) if lower is not None else None   # +: above lower
        breached = None
        if "BREACH" in sig.state or "CONFIRM" in sig.state:
            breached = "UP" if sig.state.endswith("UP") else "DOWN"
        heat = round(_heat(sig.state, dist_up, dist_dn, snap.spot), 3)
        # breach TIME: stamp round_ts when the CURRENT breach STARTS (edge None->UP/DOWN or a side
        # flip); cleared when no longer breached. This is the missing piece the dashboard needs to
        # show WHEN an instrument broke its level (previously breach onset was never recorded).
        if breached is None:
            self._breach_since.pop(inst, None)
            self._breach_side[inst] = None
        elif self._breach_side.get(inst) != breached:
            self._breach_since[inst] = round_ts
            self._breach_side[inst] = breached
        breach_ts = self._breach_since.get(inst)

        instruments_view[inst] = {
            "spot": snap.spot, "delta": round(delta, 2), "atm": br.atm_strike,
            "future": round(br.future, 2) if br.future is not None else None,
            "basis": round(basis, 3), "basis_mode": mode, "upper": upper, "lower": lower,
            "dist_up": dist_up, "dist_dn": dist_dn, "breached": breached, "heat": heat,
            "breach_ts": breach_ts,                        # when the current breach started (None if not breached)
            "state": sig.state, "dir": sig.dir, "strength": round(sig.strength, 3),
            "max_oi_ce": pin.max_oi_ce, "max_oi_pe": pin.max_oi_pe,
            "vol_ratio": round(vr, 3), "vix": snap.vix, "weight": round(weight, 4),
            "is_index": st.is_index,                       # NIFTY/sisters real; stocks N/A in replay
            "freshness_age_secs": round(fd.freshness_age_secs, 0),
        }

        if inst == "NIFTY":
            nifty_band = band
            if need and not did_recompute:
                self._levels_stale = True         # re-anchor was DUE but the band failed -> genuinely stale
                self._reanchor_owed = True        # 2.0.1 (T01): stays stale until a re-anchor SUCCEEDS
            elif band is not None:
                # fresh re-anchor OR a valid HELD band -> trustworthy. Clearing on a valid held band
                # is what un-sticks the stale flag after a transient NIFTY blink set it (a gap sets
                # _levels_stale=True but the recovery round usually has no recompute due) — council F9.
                self._levels_stale = self._reanchor_owed
            if did_recompute and band is not None:
                self._record_anchor(round_ts, band, fd.recalc_trigger)
            ce_q = snap.quote(br.atm_strike, "CE")
            pe_q = snap.quote(br.atm_strike, "PE")
            # the freshness bar must show 0 on the very round we just re-anchored (mark_recomputed
            # zeroed the age THIS round; fd.freshness_age_secs was sampled before that) — don't lie.
            held_age = 0.0 if did_recompute else round(fd.freshness_age_secs, 0)
            self._nifty_head = {
                "spot": snap.spot, "delta": round(delta, 2), "future": br.future,
                "spot_source": "CLOSING_PROXY" if "NIFTY" in self._index_spot else "INDEX",
                "index_spot": self._index_spot.get("NIFTY", snap.spot),
                "basis": round(basis, 2), "atm": br.atm_strike, "vix": snap.vix,
                "future_mode": "ACTUAL" if mode == "ACTUAL" else "IMPLIED",
                "lower": lower, "upper": upper, "dist_up": dist_up, "dist_dn": dist_dn,
                "max_oi_ce": pin.max_oi_ce, "max_oi_pe": pin.max_oi_pe,
                "k_ce": band.k_ce if band else None, "k_pe": band.k_pe if band else None,
                "atm_ce": round(ce_q.ltp, 2) if ce_q else None,
                "atm_pe": round(pe_q.ltp, 2) if pe_q else None,
                "calc": dict(self._level_anchor) if self._level_anchor else None,
                "calc_prev": dict(self._level_anchor_prev) if self._level_anchor_prev else None,
                "held_age_secs": held_age,                           # how old the held levels are (0 on re-anchor)
                "hold_secs": st.freshness.hold_min_secs,             # the fixed re-anchor interval (20 min)
            }
            self._nifty_chain = self._build_chain(snap, br.atm_strike, step)
            self._nifty_band_inputs = (round_ts, snap, br.atm_strike, basis, mode, step)
            return {"band": nifty_band, "abrupt": bm.abrupt_flag, "signal_weight": bm.signal_weight,
                    "d_basis": bm.d_basis, "abrupt_dir": bm.abrupt_dir}
        return {}

    # ---- 2.1.1: the published state carries a sequence number and wakes the dashboard ---------------
    # The dashboard used to poll `latest_state` every 20 ms and key its cache on id(dict) (pre-open frames
    # never went out: round_ts stays None; freed-dict address reuse could collide). Every assignment now
    # bumps `state_seq` and calls `state_wake` (set by the dashboard: loop.call_soon_threadsafe).
    @property
    def latest_state(self) -> dict:
        return self.__dict__.get("_latest_state", {})

    @latest_state.setter
    def latest_state(self, value: dict) -> None:
        self.__dict__["_latest_state"] = value
        self.__dict__["state_seq"] = self.__dict__.get("state_seq", 0) + 1
        wake = self.__dict__.get("state_wake")
        if wake is not None:
            try:
                wake()
            except Exception:
                pass

    @property
    def state_seq(self) -> int:
        return self.__dict__.get("state_seq", 0)

    def _gc_hygiene(self, round_ts: int) -> None:
        """2.1.1: `gc.freeze()` at startup makes CPython's long-lived total ~0, so a full collection is
        promoted as soon as survivors pile up -- and the survivors ARE the tape buffers. Re-freeze every
        ~10 min (cheap) so the standing buffers leave the scanned set; the last collection's cost is
        published in `round.gc` (app.py registers the gc callback that fills gc_info)."""
        n = self.__dict__.get("_gc_round", 0) + 1
        self.__dict__["_gc_round"] = n
        if n % 2400 == 0:
            try:
                import gc
                gc.freeze()
            except Exception:
                pass

    def process_round(self, round_ts: int, snapshots: dict[str, MarketSnapshot]) -> dict:
        # Real calculation + tape recording start at calc_start (09:20); all pre-open
        # frames (~08:19 onward) are skipped entirely — nothing computed or recorded before it.
        if not hhmm_reached(round_ts, self.calc_start):
            # pre-open: nothing is computed or recorded, but show the LIVE spot so a blank screen
            # can't be mistaken for a dead token (T28). round_ts stays None -> the PRE-OPEN banner.
            ns = snapshots.get("NIFTY") if snapshots else None
            if ns is not None:
                head = dict(self.latest_state.get("nifty") or {})
                head.update({"spot": ns.spot, "vix": ns.vix, "preopen": True})
                if head.get("upper") is not None:
                    head["dist_up"] = round(head["upper"] - ns.spot, 2)
                if head.get("lower") is not None:
                    head["dist_dn"] = round(ns.spot - head["lower"], 2)
                self.latest_state = {**self.latest_state, "nifty": head, "preopen_ts": round_ts}
            return self.latest_state
        self._round_t0 = time.perf_counter()
        if not self._regime_logged:
            self._regime_logged = True
            # 2.0.2: one short "armed" chime at the first calculated round -- proof every morning
            # that sound reaches the operator's speakers (sound council 10).
            self.sounds.emit("armed", round_ts, "VAJRA armed -- sound check")
            self.tape.event("REGIME", {**self.regime.as_dict(), "last_entry": self.entry_cutoff, "closing": self.closing.as_dict(),
                                       "mtm": self._mtm_kw if self.regime.band_method == "mtm" else None},
                            ts_ms=round_ts)
        # NIFTY is the sole execution instrument — a round without it is a feed gap, not a tradeable
        # round. Record it and skip (downstream is None-guarded, but a NIFTY-less round must not be
        # silently half-processed). Keeps the cockpit clock advancing so a gap != a hang.
        if snapshots.get("NIFTY") is None:
            # NIFTY feed gap: the held levels/cockpit are now untrustworthy -> flag stale so the
            # dashboard dims them, and EDGE-LATCH the alert so it fires once per gap episode (not
            # 4x/sec). The latch clears on the next round that DOES carry NIFTY (below).
            self._levels_stale = True
            if not self._nifty_gap_on:
                self._emit_event("FEED_GAP", {"instrument": "NIFTY", "stage": "round_missing_nifty"}, ts_ms=round_ts)
                self._nifty_gap_on = True
            self.latest_state = {**self.latest_state, "round_ts": round_ts,
                                 "events": list(self._recent_events), "levels_stale": True}
            return self.latest_state
        self._nifty_gap_on = False                  # NIFTY present again -> a future gap re-alerts
        self._index_spot = {}
        if self.closing.spot_proxy and hhmm_reached(round_ts, self.closing.index_frozen_from):
            snapshots = dict(snapshots)
            snapshots["NIFTY"] = self._closing_proxy(round_ts, snapshots["NIFTY"])
        signals: dict[str, ContinuationSignal] = {}
        nifty_band: TriggerBand | None = None
        nifty_snap = snapshots.get("NIFTY")
        instruments_view: dict = {}

        prev_bias = self.decision._bias_ewma.value or 0.0
        nifty_abrupt = False
        nifty_signal_weight = 1.0
        nifty_d_basis = 0.0
        nifty_abrupt_dir = 0

        # Deterministic instrument order so raw_bias float-sum order is identical
        # between a live record and any replay (both iterate sorted) — makes the
        # recorded and replayed decision_tape byte-identical, not just replay-replay.
        for inst in sorted(snapshots):
            snap = snapshots[inst]
            st = self.states.get(inst)
            if st is None:
                continue
            # CRITICAL ISOLATION: one bad tick on ONE instrument must NEVER end the trading day.
            # The per-instrument work runs inside try/except; a throw degrades that instrument to a
            # NEUTRAL signal + an ANOMALY and the round continues (council: int(round_to(NaN)) crash).
            try:
                res = self._process_instrument(inst, snap, st, round_ts, signals, instruments_view)
            except Exception as _exc:
                # 2.0: own latch — 1.x stored it in _basis_gap, which the basis-ok path pops every
                # round, so a persistent post-basis error re-fired 4x/sec (team finding 25).
                if not self._err_on.get(inst):
                    self._emit_event("ANOMALY", {"kind": "INSTRUMENT_ERROR", "instrument": inst,
                                                "error": type(_exc).__name__,
                                                "detail": str(_exc)[:160]}, ts_ms=round_ts)
                    self._err_on[inst] = True
                signals[inst] = ContinuationSignal("NEUTRAL", 0, 0.0, False, False, False, 0.0, None, 0.0)
                continue
            self._err_on.pop(inst, None)
            if res:                                  # NIFTY accumulators for the decision section
                nifty_band = res["band"]
                nifty_abrupt = res["abrupt"]
                nifty_signal_weight = res["signal_weight"]
                nifty_d_basis = res["d_basis"]
                nifty_abrupt_dir = res["abrupt_dir"]

        # ---- universe completeness (operator: never silently drop an instrument) ----------
        # A configured instrument whose snapshot/ATM-quote gapped this round must NOT vanish from
        # the cockpit — it carries its last-known view flagged `stale`; one never yet seen renders
        # an explicit N/A row (the dashboard's null-level path). Fixes the 21-of-24 silent drop
        # (KOTAKBANK / BANKBARODA / FEDERALBNK) the council flagged CRITICAL.
        for inst in self.states:
            if inst in instruments_view:
                self._inst_last[inst] = instruments_view[inst]
            elif inst in self._inst_last:
                instruments_view[inst] = {**self._inst_last[inst], "stale": True}
            else:
                st0 = self.states.get(inst)
                instruments_view[inst] = {
                    "spot": None, "delta": 0.0, "atm": None, "future": None, "basis": None,
                    "basis_mode": None, "upper": None, "lower": None, "dist_up": None,
                    "dist_dn": None, "breached": None, "breach_ts": None, "heat": 0.0,
                    "state": "N/A", "dir": 0,
                    "strength": 0.0, "max_oi_ce": None, "max_oi_pe": None, "vol_ratio": 0.0,
                    "vix": None, "weight": 0.0, "is_index": bool(st0.is_index) if st0 else False,
                    "freshness_age_secs": None, "stale": True,
                }

        # ---- §5.6 decision (the gate) -------------------------------------
        # An ABRUPT basis move warns the prevailing intraday direction may be
        # turning (§5.6), so it freezes new entries WHILE a bias exists — but only
        # while the basis signal still carries weight (§5.5 time-weighting): after
        # ~13:15 the decaying basis_signal_weight drops below the floor and a late,
        # convergence-driven abrupt no longer freezes.
        # §5.6: a reversal warning is an abrupt NIFTY basis move that CONTRADICTS the prevailing
        # bias — not any abrupt move while a bias merely exists. The direction test (abrupt_dir
        # opposes sign(bias)) is what makes the name true; without it a WITH-bias continuation move
        # wrongly froze entries and mislabeled the decision_tape/cockpit badge (council HIGH).
        abrupt_against_bias = self._is_reversal(
            nifty_abrupt, nifty_abrupt_dir, prev_bias,
            nifty_signal_weight, self.reversal_weight_floor)
        # 2.0: a "prevailing bias" must be a real one (1.x accepted |bias| ~0.03 -> 1,351 events/day).
        if abs(prev_bias) < self.reversal_min_bias:
            abrupt_against_bias = False
        # REVERSAL_WATCH (cockpit strip): edge-triggered AND at most once per refractory window.
        if abrupt_against_bias:
            if not self._reversal_on.get("NIFTY"):
                if (self._last_reversal_ms is None
                        or round_ts - self._last_reversal_ms >= self.reversal_refractory_ms):
                    self._emit_event("REVERSAL_WATCH", {"instrument": "NIFTY", "d_basis": nifty_d_basis,
                                                        "dir": nifty_abrupt_dir,
                                                        "bias": round(prev_bias, 4)}, ts_ms=round_ts)
                    self._last_reversal_ms = round_ts
                self._reversal_on["NIFTY"] = True
        else:
            self._reversal_on["NIFTY"] = False
        expiry_gate_ok = self._expiry_gate_ok()
        is_caution = hhmm_reached(round_ts, self.caution_after)
        if is_caution and not self._caution_emitted:
            self._emit_event("ANOMALY", {"kind": "EXECUTION_CAUTION_WINDOW",
                                        "after": self.caution_after,
                                        "note": "post-2PM: size down + tighten exits (§8.3)"},
                            ts_ms=round_ts)
            self._caution_emitted = True
        # Cross-instrument CONTEXT (bias / per-instrument states) — ADVISORY only (never a gate).
        dr = self.decision.evaluate(
            round_ts, signals, abrupt_against_bias=abrupt_against_bias,
            expiry_gate_ok=expiry_gate_ok, routing_mode=self.routing_mode,
            bias_threshold=self.bias_threshold, bias_threshold_lead=self.bias_threshold_lead,
            position_active=self.executor.position_active,
        )

        # ---- cooldown edges FIRST (2.0): when a cooldown ends, re-anchor BEFORE evaluating the
        # entry, so this round can never enter on the stale pre-cooldown band (1.x did).
        secs_since_stop = round_ts - self._last_stop_ms
        in_stop_cooldown = secs_since_stop < self.post_stop_cooldown_ms
        if self._stop_cooldown_active and not in_stop_cooldown:
            self._recompute_nifty_band(round_ts, nifty_band, trigger="post_cooldown")
        self._stop_cooldown_active = in_stop_cooldown
        in_profit_cooldown = (round_ts - self._last_profit_ms) < self.post_profit_cooldown_ms
        if self._profit_cooldown_active and not in_profit_cooldown:
            self._recompute_nifty_band(round_ts, nifty_band, trigger="post_cooldown")
        self._profit_cooldown_active = in_profit_cooldown
        if nifty_band is not None and "NIFTY" in self.states:   # pick up a cooldown-end re-anchor
            nifty_band = self.states["NIFTY"].band

        # ---- ENTRY: ANTICIPATE the breach (operator rule) -----------------
        # Fire when NIFTY comes within proximity_pts of a HELD level (or breaks through it).
        # The cross-instrument confluence is SCREAMED + LOGGED, never used to block.
        in_session = (hhmm_reached(round_ts, self.trade_start)
                      and not hhmm_reached(round_ts, self.entry_cutoff))
        entry = self._approach_entry(nifty_band, nifty_snap, signals, dr) if in_session else None
        health_ok = self._health is None or self._health.ok
        if self._proxy_on and self._basis_ref_src != "median_clean_basis":
            health_ok = False        # 2.0.4: no pre-freeze basis -> the closing-session spot is a guess

        # ---- the ordered entry gates (first closed gate = the "why no trade" reason) ----
        gate = None
        if self.executor.position_active:
            gate = Reason.POSITION_ACTIVE
        elif not hhmm_reached(round_ts, self.trade_start):
            gate = Reason.BEFORE_TRADE_START
        elif hhmm_reached(round_ts, self.entry_cutoff):
            gate = Reason.AFTER_LAST_ENTRY
        elif nifty_band is None:
            gate = Reason.NO_LEVELS
        elif not health_ok:
            gate = Reason.FEED_UNHEALTHY
        elif self._levels_stale:
            gate = Reason.LEVELS_STALE
        elif not expiry_gate_ok:
            gate = Reason.EXPIRY_GATE_BLOCKED
        elif in_stop_cooldown:
            gate = Reason.POST_STOP_COOLDOWN
        elif in_profit_cooldown:
            gate = Reason.POST_PROFIT_COOLDOWN
        elif entry is None:
            gate = Reason.NO_APPROACH
        self._entry_gate = gate
        fire = gate is None and entry is not None and nifty_snap is not None

        # NIFTY approach cue (2.0.1, teardown T04/T13): an EARLY warning at sounds.approach_warn_pts
        # (default 15, wider than the 7-pt entry) so it rings BEFORE the trade, not in the same
        # millisecond as the entry cue. Once per approach episode; silent when no trade is possible
        # (in a position, outside 09:21-15:25, bad feed, stale levels, either cooldown), and silent on
        # the round the entry itself fires (the entry cue says it).
        if nifty_band is not None and nifty_snap is not None:
            du = (nifty_band.upper_level - nifty_snap.spot) if nifty_band.upper_level is not None else None
            dl = (nifty_snap.spot - nifty_band.lower_level) if nifty_band.lower_level is not None else None
            if du is not None and dl is not None:
                near_up = du <= dl
                d, lvl = (du, nifty_band.upper_level) if near_up else (dl, nifty_band.lower_level)
                started = self._approach_ep.update(max(0.0, d), lvl)
                possible = gate in (None, Reason.NO_APPROACH)
                rang = False
                if started and possible and not fire:
                    rang = self.sounds.emit("approach", round_ts,
                                            f"NIFTY {'upper' if near_up else 'lower'} level {lvl:.0f} "
                                            f"({max(0.0, d):.0f} pts)")
                if started and not rang and not fire:
                    # 2.0.2 (sound council 01/05/08): an episode that could not ring (cooldown, gate,
                    # gap) stays armed and rings as soon as it can -- it was being used up silently.
                    self._approach_ep.rearm()
                # 2.0.2: ENTRY-IMMINENT warning. 30 of Monday's 40 entries fired the instant a
                # cooldown ended with NIFTY already at the level, so no approach could ever ring
                # first. Ring the approach cue `imminent_secs` before the cooldown ends when NIFTY
                # is inside the warning zone -- once per cooldown.
                if gate in (Reason.POST_STOP_COOLDOWN, Reason.POST_PROFIT_COOLDOWN) and not fire:
                    if gate == Reason.POST_STOP_COOLDOWN:
                        key, end_ms = ("stop", self._last_stop_ms), self._last_stop_ms + self.post_stop_cooldown_ms
                    else:
                        key, end_ms = ("profit", self._last_profit_ms), self._last_profit_ms + self.post_profit_cooldown_ms
                    left = end_ms - round_ts
                    if (0 < left <= self._imminent_ms and key != self._imminent_key
                            and d <= self._approach_ep.prox):
                        self._imminent_key = key
                        self.sounds.emit("approach", round_ts,
                                         f"NIFTY {'upper' if near_up else 'lower'} level {lvl:.0f} "
                                         f"({max(0.0, d):.0f} pts) -- cooldown ends in {left / 1000:.0f}s",
                                         force=True)

        # decision_tape (a BINDING audit artifact, §9) is written AFTER the action outcome is
        # known, so it can never show FIRE while order_tape shows REJECT for the same round.
        self._entry_block = None          # this round's entry outcome (for the scream): None|ENTERED|reject reason
        if fire:
            decision_word, reason = ("FIRE_UP" if entry["direction"] == "UP" else "FIRE_DOWN"), Reason.APPROACH_ENTRY
            conf = entry["confluence"]
            cur_atm = ((self._nifty_head.get("atm") or nifty_band.atm_strike)
                       if self.strike_from == "current_atm" else nifty_band.atm_strike)
            self._emit_alert_event("APPROACH_ALERT", entry["direction"], {
                "direction": entry["direction"], "level_side": entry["side"],
                "level": round(entry["level"], 2), "spot": round(nifty_snap.spot, 2),
                "dist_pts": round(entry["dist"], 2), "verdict": conf["verdict"],
                "confidence": conf["confidence"], "same_side": conf["same_side"],
                "opp_side": conf["opp_side"], "conflicted": conf["conflicted"],
                "regime": self.regime.name,
                "buy": f"NIFTY {int(cur_atm)} {'CE' if entry['direction']=='UP' else 'PE'}",
            }, round_ts)
            # signal_tape is written AFTER enter() returns, gated on a real ENTRY (below).
            order_rows = self.executor.enter(
                round_ts, entry["direction"], nifty_snap, nifty_band,
                conviction=abs(conf["confidence"]), threshold=self.bias_threshold,
                channels=conf["same_side"], is_expiry_day=self.expiry.is_expiry_day,
                expiry_iso=self.expiry.expiry_date.isoformat(),
                step=self.states["NIFTY"].step,
                # 2.0: buy the CURRENT ATM (execution.strike_from: current_atm). "anchor_atm" = 1.0 behaviour
                atm_strike=(int(cur_atm) if self.strike_from == "current_atm" else None),
            )
            self._emit_orders(order_rows, confluence=conf)
            _r0 = order_rows[0] if order_rows else None
            self._entry_block = ("ENTERED" if (_r0 and _r0["action"] == "ENTRY")
                                 else (_r0.get("exit_reason") if _r0 else None))
            if _r0 and _r0["action"] == "ENTRY":
                self.tape.append("signal_tape", self._approach_signal_row(round_ts, entry, conf))
                self._position_dir = entry["direction"]
                self._last_confluence = conf
                self._entries_today += 1
                self.sounds.emit("entry", round_ts,
                                 f"BUY NIFTY {_r0['strike']} {_r0['right']} @ {_r0['fill_price']:.2f}")
                # operator rule (d): levels are NOT recomputed at entry.
            elif _r0 and _r0["action"] == "REJECT":        # entry FIRED but the order was rejected
                decision_word, reason = "HOLD", (_r0.get("exit_reason") or Reason.NO_APPROACH)
                self._emit_alert_event("ENTRY_REJECTED", _r0.get("exit_reason"), {
                    "reason": _r0.get("exit_reason"), "direction": entry["direction"],
                    "level_side": entry["side"], "level": round(entry["level"], 2),
                    "spot": round(nifty_snap.spot, 2), "dist_pts": round(entry["dist"], 2),
                    "verdict": conf.get("verdict"),
                }, round_ts)
            else:                                          # no rows at all (defensive)
                decision_word, reason = "HOLD", Reason.NO_APPROACH
        else:
            decision_word, reason = "HOLD", (gate or Reason.NO_APPROACH)
            if gate == Reason.POST_STOP_COOLDOWN and entry is not None:
                _left = max(0, round((self.post_stop_cooldown_ms - secs_since_stop) / 1000))
                self._entry_block = f"POST-STOP COOLDOWN {_left}s"

        # ---- manage an open position EVERY round NIFTY is present (2.0: even when this round's
        # band/basis failed — 1.x skipped stops and the 15:25 flatten on such rounds).
        if self.executor.position_active and nifty_snap is not None and not fire:
            _pos = self.executor.position
            if _pos is not None and nifty_snap.quote(_pos.strike, _pos.right) is None:
                if not self._manage_gap_on:
                    self._emit_event("FEED_GAP", {"instrument": "NIFTY", "strike": _pos.strike,
                                                 "right": _pos.right, "stage": "manage_quote"}, ts_ms=round_ts)
                    self._manage_gap_on = True
            else:
                self._manage_gap_on = False
            order_rows = self.executor.manage(round_ts, nifty_snap)
            self._emit_orders(order_rows)
            for _xr in order_rows:                 # audit a LIVE exit that the broker rejected
                if _xr["action"] == "REJECT" and _xr.get("exit_reason") == "ORDER_PLACEMENT_FAILED":
                    self.sounds.emit("feed_alarm", round_ts, "EXIT REJECTED by broker -- position still open", force=True)
                    self._emit_alert_event("EXIT_REJECTED", _xr["exit_reason"], {
                        "strike": _xr["strike"], "right": _xr["right"],
                        "reason": _xr["exit_reason"]}, round_ts)
                if _xr["action"] == "EXIT":
                    pnl = _xr.get("pnl_realized", 0.0)
                    self.sounds.emit("exit_profit" if pnl > 0 else "exit_loss", round_ts,
                                     f"EXIT {_xr['exit_reason']} {pnl:+,.0f}")
                    if self.executor.last_exit_quote_age_ms > 30_000:     # priced off a frozen quote (T15)
                        self._emit_event("ANOMALY", {"kind": "STALE_QUOTE_AT_EXIT",
                                                     "quote_age_secs": self.executor.last_exit_quote_age_ms // 1000,
                                                     "strike": _xr["strike"], "right": _xr["right"],
                                                     "fill": _xr["fill_price"],
                                                     "note": "paper exit priced off an option quote that had not "
                                                             "changed for this long -- check the real fill"},
                                         ts_ms=round_ts)
                if _xr["action"] == "EXIT" and _xr.get("exit_reason") == "SL_HIT":
                    self._last_stop_ms = round_ts
                    self._emit_event("ANOMALY", {"kind": "POST_STOP_COOLDOWN",
                                                 "cooldown_secs": self.post_stop_cooldown_ms // 1000,
                                                 "strike": _xr["strike"], "right": _xr["right"]},
                                     ts_ms=round_ts)
                elif _xr["action"] == "EXIT" and _xr.get("pnl_realized", 0.0) > 0:
                    self._last_profit_ms = round_ts
            if not self.executor.position_active:
                self._position_dir = None
                self._last_confluence = None
                if not hhmm_reached(round_ts, self.flatten_at):   # no pointless re-anchor after the flatten
                    self._recompute_nifty_band(round_ts, nifty_band)

        self._check_power(round_ts)
        self._gc_hygiene(round_ts)
        self._nearest_dist = None
        if nifty_band is not None and nifty_snap is not None and nifty_band.upper_level is not None:
            self._nearest_dist = round(min(nifty_band.upper_level - nifty_snap.spot,
                                           nifty_snap.spot - nifty_band.lower_level), 2)
        self.tape.append("decision_tape", self._decision_row(round_ts, dr, decision_word, reason))
        # The authoritative NIFTY decision (what actually fired/was rejected) — surfaced to the
        # cockpit badge so the dashboard matches decision_tape, not the dead DecisionMaker HOLD.
        self._last_decision = {"decision": decision_word, "reason": reason}

        # ---- dashboard view ------------------------------------------------
        alarm = self._compute_alarm(round_ts, nifty_band, nifty_snap, signals, dr)
        self._update_view(round_ts, instruments_view, dr, alarm)
        # 2.0.1 round_tape: one compact line per round for EOD forensics (timing, health, what the
        # operator was shown). Cheap: ~150 bytes/round, ~13 MB/day.
        _rt = getattr(self.tape, "round", None)
        if callable(_rt):
            try:
                _wall = int(time.time() * 1000)
                _rt({"ts": round_ts, "wall_ms": _wall, "lag_ms": _wall - round_ts,
                     "proc_ms": round((time.perf_counter() - self._round_t0) * 1000, 2),
                     "n_instr": len(snapshots), "spot": (nifty_snap.spot if nifty_snap is not None else None),
                     "health": (self._health.reason if self._health is not None and not self._health.ok else "ok"),
                     "gate": self._entry_gate, "arming": alarm.get("state"), "block": self._entry_block,
                     "pos": (self.executor.position.strike if self.executor.position_active else None),
                     "snd_seq": self.sounds._seq, "levels_stale": self._levels_stale,
                     "upper": (nifty_band.upper_level if nifty_band is not None else None),
                     "lower": (nifty_band.lower_level if nifty_band is not None else None),
                     "ticks": getattr(self._breeze, "ticks_received", None),
                     "ws": self._ws_brief()})
            except Exception:
                pass
        _er = getattr(self.tape, "end_round", None)       # 2.0: one jsonl flush per round, not per row
        if callable(_er):
            _er()
        return self.latest_state

    def _check_power(self, round_ts: int) -> None:
        """Sample the machine's power state every ~30 s (120 rounds); shout once per edge."""
        fn = self.power_status_fn
        if fn is None:
            return
        self._power_round += 1
        if self._power_round % 120 != 1:
            return
        try:
            st = dict(fn() or {})
        except Exception:
            return
        self._power = st
        on_batt = bool(st.get("on_battery"))
        if on_batt and not self._power_on_battery:
            self._emit_event("ANOMALY", {"kind": "POWER_ON_BATTERY", "battery_pct": st.get("battery_pct"),
                                         "note": "laptop unplugged -- a power-off mid-trade cannot be flattened by VAJRA"},
                             ts_ms=round_ts)
            self.sounds.emit("feed_alarm", round_ts, f"RUNNING ON BATTERY {st.get('battery_pct')}% -- plug in", force=True)
            from .console import warn
            warn(f"  !! RUNNING ON BATTERY ({st.get('battery_pct')}%) -- plug the laptop in NOW.")
        elif not on_batt and self._power_on_battery:
            self._emit_event("ANOMALY", {"kind": "POWER_AC_RESTORED", "battery_pct": st.get("battery_pct")}, ts_ms=round_ts)
        self._power_on_battery = on_batt

    def _feed_health_view(self) -> dict | None:
        """2.1: broker socket / subscription health (live mode only; None in replay)."""
        src = getattr(self, "_source", None)
        fn = getattr(src, "health", None) or getattr(self._breeze, "health", None)
        if not callable(fn):
            return None
        try:
            return dict(fn())
        except Exception:
            return None

    def _ws_brief(self):
        h = self._feed_health_view()
        if not h:
            return None
        return {k: h.get(k) for k in ("connected", "tick_age_s", "reconnects_ok", "reconnects_failed",
                                      "drops", "sub_errors", "silent") if k in h}

    def _record_anchor(self, ts, band, trigger):
        """Snapshot the NIFTY level calc (time + ITM inputs + levels) for the dashboard,
        shifting the current anchor into 'previous'."""
        self.sounds.reset("approach")                 # new levels = a new setup (sound council 01)
        if band is None or band.upper_level is None or band.lower_level is None:
            return
        cur = {
            "ts": int(ts), "trigger": trigger,
            "upper": round(band.upper_level, 2), "lower": round(band.lower_level, 2),
            "atm": band.atm_strike,
            "k_ce": band.k_ce, "vwap_ce": round(band.vwap_ce, 2) if band.vwap_ce is not None else None,
            "k_pe": band.k_pe, "vwap_pe": round(band.vwap_pe, 2) if band.vwap_pe is not None else None,
            "basis": round(band.basis, 2), "basis_mode": band.basis_mode,
            "band_method": getattr(band, "band_method", "legacy"),
            "narrowed": bool(getattr(band, "narrowed", False)),
            "legacy_upper": round(band.legacy_upper, 2) if getattr(band, "legacy_upper", None) is not None else None,
            "legacy_lower": round(band.legacy_lower, 2) if getattr(band, "legacy_lower", None) is not None else None,
            "anchor_spot": round(band.anchor_spot, 2) if getattr(band, "anchor_spot", None) is not None else None,
        }
        self._level_anchor_prev = self._level_anchor
        self._level_anchor = cur
        self._level_history.append(cur)              # full log of every recompute (time + inputs)

    def seed_from_prev_close(self, snap, ts_ms):
        """(operator rule a) Seed PRE-OPEN U/L levels from the previous day's close — its spot
        + nearest-ITM CE/PE — so levels exist pre-open. Held until the 09:20 intraday calc."""
        if snap is None:
            return
        st = self.states.get("NIFTY")
        if st is None:
            return
        step = self.cfg.strike_step("NIFTY")
        br = compute_basis(snap, step, self._basis_mode_for("NIFTY", st.is_index))
        if not br.ok:
            return
        band = self._build_band("NIFTY", snap, br.atm_strike, br.basis, br.basis_mode, step)
        if not band.ok:                     # NEVER write a fabricated 0.0/0.0 level to level_tape —
            self._emit_event("FEED_GAP", {"instrument": "NIFTY", "stage": "prev_close_seed_band",
                                         "reason": band.reason}, ts_ms=ts_ms)
            return                           # unchanged from the "no seed" path: 09:20 "open" calc anchors normally
        st.band = band
        st.freshness.mark_recomputed(ts_ms)        # so the first intraday round (09:20) re-anchors
        self._nifty_band_inputs = (ts_ms, snap, br.atm_strike, br.basis, br.basis_mode, step)
        self._record_anchor(ts_ms, band, "prev_close_seed")
        self.tape.append("level_tape", self._level_row(
            ts_ms, "NIFTY", band, locate_pin(snap, br.future, step), "prev_close_seed", 0.0))
        # 2.0.1 (T28): show the seeded levels pre-open instead of a blank cockpit
        self._nifty_head = {"spot": snap.spot, "lower": band.lower_level, "upper": band.upper_level,
                            "atm": br.atm_strike, "basis": round(br.basis, 2), "future": br.future,
                            "future_mode": br.basis_mode, "vix": snap.vix, "preopen": True,
                            "calc": dict(self._level_anchor) if self._level_anchor else None}
        self.latest_state = {**self.latest_state, "nifty": dict(self._nifty_head),
                             "level_history": list(self._level_history)[-20:]}

    def _recompute_nifty_band(self, ts, fallback_band, trigger="post_exit"):
        """Recompute NIFTY's U/L levels NOW from the nearest ITM VWAPs — re-anchor immediately
        AFTER booking out, and again when a post-profit cooldown ends (operator rule)."""
        if self._nifty_band_inputs is None:
            return
        stamp_ts, snap, atm, basis, mode, step = self._nifty_band_inputs
        if stamp_ts != ts:                          # inputs are from a prior round (NIFTY fed-gapped
            self._levels_stale = True               # this round) -> recomputing would anchor STALE data
            self._reanchor_owed = True
            self.states["NIFTY"].freshness.mark_attempt(ts)   # engage the 30s short-retry, don't wait the full interval
            self._emit_event("FEED_GAP", {"instrument": "NIFTY", "stage": "post_exit_band",
                                         "reason": "STALE_INPUTS"}, ts_ms=ts)
            return
        if self._health is not None and not self._health.ok:   # 2.0: never re-anchor on a bad feed
            self._levels_stale = True
            self._reanchor_owed = True
            self.states["NIFTY"].freshness.mark_attempt(ts)
            self._emit_event("FEED_GAP", {"instrument": "NIFTY", "stage": f"{trigger}_band",
                                         "reason": f"FEED_{self._health.reason}"}, ts_ms=ts)
            return
        band = self._build_band("NIFTY", snap, atm, basis, mode, step)
        if not band.ok:                             # failed band -> keep the held one, do NOT arm
            self._levels_stale = True               # held U/L levels are now stale -> flag the cockpit
            self._reanchor_owed = True
            self.states["NIFTY"].freshness.mark_attempt(ts)   # engage the 30s short-retry, don't wait the full interval
            self._emit_event("FEED_GAP", {"instrument": "NIFTY", "stage": "post_exit_band",
                                         "reason": band.reason}, ts_ms=ts)
            return
        st = self.states["NIFTY"]
        st.band = band
        st.freshness.mark_recomputed(ts)            # hold; next interval re-anchor is 20 min out
        self._reanchor_owed = False
        self._levels_stale = False                  # fresh re-anchor -> levels trustworthy again
        self._record_anchor(ts, band, trigger)
        self.tape.append("level_tape", self._level_row(
            ts, "NIFTY", band, locate_pin(snap, basis + snap.spot, step), trigger, 0.0))

    def _round_error(self, round_ts, snapshots, exc):
        """A round raised. 1.x/2.0 ended the whole session here (SESSION_ERROR -> flatten -> exit).
        Now: log it (once per distinct error per minute), still MANAGE the open position from this
        round's NIFTY quote (stops / 15:25 flatten must never be skipped), and keep running."""
        import traceback
        key = repr(exc)[:200]
        last = self._round_err_seen.get(key)
        if last is None or round_ts - last >= 60_000:
            self._round_err_seen[key] = round_ts
            tb = traceback.format_exc(limit=6)
            print(f"  vajra · ROUND ERROR at {round_ts}: {key}\n{tb}", file=sys.stderr, flush=True)
            try:
                self._emit_event("SESSION_ERROR", {"stage": "process_round", "severity": "ROUND_SKIPPED",
                                                  "reason": key, "trace": tb[-1500:]}, ts_ms=round_ts)
            except Exception:
                pass
        try:
            nifty = snapshots.get("NIFTY") if snapshots else None
            if nifty is not None and self.executor.position_active:
                rows = self.executor.manage(round_ts, nifty)
                self._emit_orders(rows)
                for _xr in rows:
                    if _xr["action"] == "EXIT":
                        pnl = _xr.get("pnl_realized", 0.0)
                        self.sounds.emit("exit_profit" if pnl > 0 else "exit_loss", round_ts,
                                         f"EXIT {_xr['exit_reason']} {pnl:+,.0f}")
                        if _xr.get("exit_reason") == "SL_HIT":
                            self._last_stop_ms = round_ts
                        elif pnl > 0:
                            self._last_profit_ms = round_ts
                self._push_sounds()
        except Exception:
            pass
        try:
            self.tape.end_round()
        except Exception:
            pass
        try:                                             # 2.1.1: the cockpit clock must not stop during an
            self.latest_state = {**(self.latest_state or {}), "round_ts": round_ts,   # error episode (false
                                 "events": list(self._recent_events)}                 # DATA FROZEN)
        except Exception:
            pass

    # ---- same-day restart (2.0.1, teardown T07/T09/T27) ---------------------
    def restore_basis_history(self, partition) -> int:
        """2.0.4: a restart inside / just before the closing session would have no pre-freeze basis
        (basis_ref). Rebuild it from the tail of today's basis_tape.jsonl. Returns rows restored."""
        import json
        from pathlib import Path
        path = Path(partition) / "basis_tape.jsonl"
        lim = float(self.cfg.get("feed_sanity", "max_abs_basis_pts", default=40))
        recs: list = []
        if path.exists():
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    fh.seek(max(0, size - 64 * 1024 * 1024))
                    tail = fh.read().decode("utf-8", errors="replace").splitlines()[1:]
            except OSError:
                tail = []
            for line in tail:
                if '"NIFTY"' not in line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
        else:
            # 2.1.1: the default config writes NO basis_tape.jsonl (tape.sidecar_jsonl: small), so every
            # 2.1 restart came up with an empty basis history -> no entries 15:15-15:29 and a proxy spot
            # off by the basis. Read the parquet (today's parts are merged/skipped safely) instead.
            try:
                from .tape import iter_parquet_rows
                for r in iter_parquet_rows(Path(partition), "basis_tape",
                                           columns=["ts", "instrument", "spot_source", "parity_basis"]):
                    if r.get("instrument") == "NIFTY":
                        recs.append(r)
            except Exception:
                recs = []
        rows = []
        for r in recs:
            if r.get("instrument") != "NIFTY" or r.get("spot_source") != "INDEX":
                continue
            b, ts = r.get("parity_basis"), r.get("ts")
            if b is None or ts is None or not math.isfinite(float(b)) or abs(float(b)) > lim:
                continue
            if hhmm_reached(int(ts), self.closing.index_frozen_from):
                continue
            rows.append((int(ts), float(b)))
        if not rows:
            return 0
        last = rows[-1][0]
        self._clean_basis = deque(r for r in rows if last - r[0] <= self._basis_ref_window_ms)
        return len(self._clean_basis)

    def restore_from_order_tape(self, partition) -> dict | None:
        """A same-day restart (token refresh / crash relaunch) used to reset capital to the start
        value, forget the open paper position and clear both cooldowns -- breaking the operator's
        compounding rule. Rebuild them from today's order_tape.jsonl (append-only, written before any
        restart). Returns a summary dict (also logged as a RESTORED event) or None if nothing to do."""
        import json
        from pathlib import Path
        from .execution import Position, SL_HIT
        path = Path(partition) / "order_tape.jsonl"
        if not path.exists():
            return None
        rows = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue                                   # a torn last line from a crash
        if not rows:
            return None
        ex = self.executor
        pnl = 0.0
        open_entry = None
        n_entries = 0
        self.journal = []                                    # 2.1: the cockpit journal / trade count survive too
        self._open_trade = None
        equity_high = ex.starting_capital
        for r in rows:
            act = r.get("action")
            if act == "ENTRY":
                open_entry = r
                n_entries += 1
                self._open_trade = {
                    "entry_ts": int(r["ts"]), "exit_ts": None,
                    "direction": "UP" if r.get("right") == "CE" else "DOWN", "right": r.get("right"),
                    "strike": r.get("strike"), "qty": int(r.get("qty") or 0),
                    "lots": int(r.get("qty") or 0) // max(1, ex.lot_size),
                    "entry_price": round(float(r.get("fill_price") or 0.0), 2), "exit_price": None,
                    "pnl": 0.0, "pnl_pct": 0.0, "exit_reason": None, "status": "OPEN",
                    "verdict": None, "same_side": [], "opp_side": [], "conflicted": False,
                }
            elif act == "EXIT":
                p_ = float(r.get("pnl_realized") or 0.0)
                if math.isfinite(p_):
                    pnl += p_
                equity_high = max(equity_high, ex.starting_capital + pnl)
                if (r.get("exit_reason") or "") == SL_HIT:
                    self._last_stop_ms = int(r["ts"])
                    self._stop_cooldown_active = True
                elif p_ > 0:
                    self._last_profit_ms = int(r["ts"])
                    self._profit_cooldown_active = True
                if self._open_trade is not None:
                    t = self._open_trade
                    t["exit_ts"] = int(r["ts"]); t["exit_price"] = round(float(r.get("fill_price") or 0.0), 2)
                    t["pnl"] = round(p_, 2)
                    t["pnl_pct"] = (round(p_ / (t["entry_price"] * t["qty"]) * 100, 1)
                                    if t["entry_price"] and t["qty"] else 0.0)
                    t["exit_reason"] = r.get("exit_reason"); t["status"] = "CLOSED"
                    self.journal.append(t)
                    self._open_trade = None
                open_entry = None
        self._equity_high = equity_high
        ex.capital = ex.starting_capital + pnl
        self._entries_today = n_entries
        seqs = [int(str(r.get("order_id"))[6:]) for r in rows
                if str(r.get("order_id") or "").startswith("PAPER-") and str(r.get("order_id"))[6:].isdigit()]
        if seqs and hasattr(ex.wrapper, "_paper_seq"):     # PAPER ids must not repeat after a restart (T27)
            ex.wrapper._paper_seq = max(seqs)
        restored_pos = None
        if open_entry is not None and int(open_entry.get("qty") or 0) > 0:
            fill = float(open_entry["fill_price"]); qty = int(open_entry["qty"])
            right = open_entry["right"]
            ex.position = Position(
                direction="UP" if right == "CE" else "DOWN", right=right,
                strike=int(open_entry["strike"]), entry_ts=int(open_entry["ts"]), entry_fill=fill,
                entry_spot=float("nan"), lots=qty // ex.lot_size, lot_size=ex.lot_size,
                qty_total=qty, qty_remaining=qty, cost=qty * fill,
                peak_premium=fill, last_premium=fill,
                order_ids=[str(open_entry.get("order_id") or "")])
            self._position_dir = ex.position.direction
            restored_pos = f"{right} {int(open_entry['strike'])} x{qty} @ {fill:.2f}"
        info = {"capital": round(ex.capital, 2), "realized_pnl": round(pnl, 2),
                "entries_today": n_entries, "open_position": restored_pos,
                "last_stop_ts": self._last_stop_ms or None, "last_profit_ts": self._last_profit_ms or None,
                "note": "restart: capital/position/cooldowns rebuilt from today's order_tape "
                        "(trail peak restarts at the entry fill)"}
        self._emit_event("RESTORED", info, ts_ms=int(rows[-1]["ts"]))
        return info

    # ---- run loop ----------------------------------------------------------
    def run(self, source, heartbeat=False):
        # (operator rule a) seed pre-open levels from the previous day's close — LOUD on failure/stale
        # so a silent no-seed (which causes a late 09:20 calc) can never hide again.
        # Wrapped: seed_from_prev_close runs compute_basis/compute_band on the seed, OUTSIDE the round
        # loop's try/except below. A bad seed (e.g. a non-finite value) must NOT crash startup before
        # the session begins — fall through and let the 09:20 calc anchor levels.
        try:
            if hasattr(source, "prev_close_snapshot"):
                seed = source.prev_close_snapshot()
                src_desc = getattr(source, "_seed_source", None)
                if seed is not None:
                    self.seed_from_prev_close(seed, seed.ts_ms)
                    import sys
                    print(f"  vajra · pre-open seed from {src_desc}", file=sys.stderr, flush=True)
                    if src_desc and "STALE" in src_desc:        # used old recorded data, not a real prev-day
                        self._emit_event("ANOMALY", {"kind": "STALE_PREV_CLOSE_SEED", "source": src_desc},
                                         ts_ms=seed.ts_ms)
                else:                                            # NO seed at all -> CRITICAL, levels unseeded
                    import sys
                    print("  vajra · CRITICAL: NO prev-close seed — pre-open levels UNSEEDED; first calc at 09:20",
                          file=sys.stderr, flush=True)
                    self._emit_event("SESSION_ERROR", {"stage": "prev_close_seed", "severity": "CRITICAL",
                                     "reason": "no prior-day data; pre-open levels unseeded"}, ts_ms=None)
        except Exception as _seed_exc:                       # a bad seed must never kill the run before it starts
            import sys
            print(f"  vajra · seed FAILED ({type(_seed_exc).__name__}: {str(_seed_exc)[:120]}) — "
                  f"continuing; levels anchor at 09:20", file=sys.stderr, flush=True)
            self._emit_event("SESSION_ERROR", {"stage": "prev_close_seed", "severity": "HIGH",
                             "reason": f"seed error: {type(_seed_exc).__name__}"}, ts_ms=None)
        last_round = None
        last_good = None        # last round that carried NIFTY — used to flatten cleanly at finalize
        _hb = 0
        self._source = source
        try:
            for round_ts, snapshots in source.rounds():
                if self._stop.is_set():
                    break
                try:
                    self.process_round(round_ts, snapshots)
                except Exception as _re:          # 2.0.1 (T01): one bad round must NOT end the trading day
                    self._round_error(round_ts, snapshots, _re)
                if hasattr(source, "pin_strikes"):   # keep the HELD strike streaming even if spot runs
                    pos = self.executor.position     # past the ATM±N window (else: no stop; T07)
                    source.pin_strikes({("NIFTY", int(pos.strike), pos.right)}
                                       if pos is not None and pos.open else set())
                last_round = (round_ts, snapshots)
                if snapshots.get("NIFTY") is not None:
                    last_good = (round_ts, snapshots)
                _hb += 1
                if heartbeat and _hb % 120 == 0:     # terminal liveness for an attended live run (~30 s)
                    from .console import warn        # 2.1: queued -- a blocked console can't stall the engine
                    nh = self.latest_state.get("nifty") or {}
                    am = self.latest_state.get("arming") or {}
                    ws = self.latest_state.get("ws") or {}
                    warn(f"  vajra · {len(snapshots)} instr · NIFTY {nh.get('spot')} · {am.get('state')}"
                         f" · trades {((self.latest_state.get('trades_summary') or {}).get('closed'))}"
                         f" · events {len(self._recent_events)}"
                         f" · ws {'up' if ws.get('connected', True) else 'DOWN'} tick-age {ws.get('tick_age_s', '?')}s")
        except Exception as e:                  # a mid-session feed/engine crash must STILL flatten + shut down
            self._emit_event("SESSION_ERROR", {"stage": "run_loop", "reason": repr(e)},
                             ts_ms=(last_round[0] if last_round else None))
        finally:
            fin = last_good or last_round        # prefer a NIFTY-bearing round so the square-off can fill
            if (fin is not None and last_good is not None and last_round is not None
                    and last_round[0] - last_good[0] > 60_000 and self.executor.position_active):
                # NIFTY quotes stopped > 60 s before the end: booking the flatten at that old price
                # (and stamping it at that old time) would be fiction (T15). Leave it OPEN + say so.
                pos = self.executor.position
                self._emit_event("SESSION_ERROR", {
                    "stage": "finalize", "severity": "CRITICAL",
                    "reason": "no NIFTY quote for the last %ds — paper position left OPEN; flatten manually"
                              % ((last_round[0] - last_good[0]) // 1000),
                    "strike": pos.strike, "right": pos.right, "qty": pos.qty_remaining}, ts_ms=last_round[0])
                self.sounds.emit("feed_alarm", last_round[0], "POSITION LEFT OPEN -- flatten manually", force=True)
                self._push_sounds()
                print(f"  !! NO NIFTY QUOTE at the close — paper position {pos.right} {pos.strike} x"
                      f"{pos.qty_remaining} left OPEN. Flatten your ICICI position manually.",
                      file=__import__("sys").stderr, flush=True)
                self._emit_event("SHUTDOWN", {"reason": "session_end_position_open"}, ts_ms=last_round[0])
                self.tape.flush()
            elif fin is not None:
                self.finalize(*fin)
            else:                                        # 2.1.1: a Ctrl+C before the first round is a CLEAN
                try:                                     # stop, not an "ENDED ABNORMALLY" at the next start
                    self._emit_event("SHUTDOWN", {"reason": "stopped_before_first_round"})
                    self.tape.flush()
                except Exception:
                    pass

    def stop(self):
        self._stop.set()

    def _push_sounds(self):
        """Publish the sound ring outside the normal state build (error / shutdown paths)."""
        try:
            self.latest_state = {**(self.latest_state or {}), "sound_events": self.sounds.snapshot()}
        except Exception:
            pass

    def finalize(self, round_ts: int, snapshots: dict[str, MarketSnapshot]):
        nifty_snap = snapshots.get("NIFTY")
        if nifty_snap is not None:
            _rows = self.executor.force_flatten(round_ts, nifty_snap)
            self._emit_orders(_rows)
            for _xr in _rows:                       # a shutdown flatten is a real exit -> its cue too (T04)
                if _xr["action"] == "EXIT":
                    pnl = _xr.get("pnl_realized", 0.0)
                    self.sounds.emit("exit_profit" if pnl > 0 else "exit_loss", round_ts,
                                     f"EXIT {_xr['exit_reason']} {pnl:+,.0f}")
        self._push_sounds()                          # the last cue must reach the page (council 07)
        try:                                         # 2.1.1: the flatten's EXIT row reaches the disk BEFORE
            self.tape.end_round()                    # SHUTDOWN (a window-close kills us ~4 s later; a
        except Exception:                            # SHUTDOWN without the EXIT restored a phantom position)
            pass
        self._emit_event("SHUTDOWN", {"reason": "session_end"}, ts_ms=round_ts)
        self.tape.flush()

    # ---- row builders ------------------------------------------------------
    def _closing_proxy(self, round_ts: int, snap: MarketSnapshot) -> MarketSnapshot:
        """2.0.4: from index_frozen_from the NIFTY index is frozen / the indicative auction close.
        Replace the spot with FORWARD - BASIS_REF (vajra/closing.py). FORWARD is option put-call
        parity (median of 3 strikes around the last spot) in both basis modes. BASIS_REF = median of
        (parity forward - index) over the clean window before the freeze. If no forward is
        available this round the previous proxy is held (the feed-sanity frozen check then trips)."""
        if self._basis_ref is None:
            vals = sorted(b for _, b in self._clean_basis)
            if vals:
                n = len(vals)
                self._basis_ref = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
                src = "median_clean_basis"
            else:
                self._basis_ref = 0.0
                src = "none_available_zero"
            self._proxy_on = True
            self._basis_ref_src = src
            self._emit_event("ANOMALY", {"kind": "CLOSING_SESSION_SPOT", "instrument": "NIFTY",
                                         "basis_ref": round(self._basis_ref, 2), "basis_ref_source": src,
                                         "samples": len(vals), "index_spot": snap.spot,
                                         "from": self.closing.index_frozen_from,
                                         "last_entry": self.entry_cutoff, "flatten_before": self.flatten_at,
                                         "note": "NSE closing auction: NIFTY spot now = forward - basis_ref"},
                             ts_ms=round_ts)
        step = self.cfg.strike_step("NIFTY", snap.inferred_step())
        center = self._proxy_spot if self._proxy_spot is not None else snap.spot
        fwd = None
        # Option put-call parity in BOTH basis modes: on 21 Oct the traded future's LTP sat unchanged
        # for 25 s at 15:18 (false SPOT_FROZEN) while the options ticked every round.
        for _ in range(2):                   # re-centre once if the forward moved the ATM
            fwd = parity_forward(snap, step, center)
            if fwd is None:
                break
            new_center = fwd - self._basis_ref
            if round(new_center / step) == round(center / step):
                break
            center = new_center
        if fwd is not None and math.isfinite(fwd):
            self._proxy_spot = round(fwd - self._basis_ref, 2)
        if self._proxy_spot is None:
            return snap                      # nothing to derive yet -> raw (health gate decides)
        self._index_spot["NIFTY"] = snap.spot
        return _dc_replace(snap, spot=self._proxy_spot)

    def _tick_row(self, ts, snap: MarketSnapshot, br) -> dict:
        strikes = [{
            "strike": q.strike, "right": q.right, "ltp": q.ltp, "vwap": q.vwap,
            "vwap_source": q.vwap_source, "volume": q.volume, "oi": q.oi,
            "bid": q.bid, "ask": q.ask,
        } for q in snap.all_strikes_sorted()]
        return {           # 2.0.4: tick_tape keeps the RAW index print (replays re-derive the proxy)
            "ts": ts, "instrument": snap.instrument, "spot": self._index_spot.get(snap.instrument, snap.spot),
            "atm_strike": br.atm_strike, "future": br.future, "basis": br.basis,
            "basis_mode": br.basis_mode, "vix": snap.vix, "strikes": strikes,
        }

    def _level_row(self, ts, inst, band: TriggerBand, pin, trigger, age) -> dict:
        return {
            "ts": ts, "instrument": inst, "atm_strike": band.atm_strike,
            "max_oi_ce": pin.max_oi_ce if pin.max_oi_ce is not None else 0,
            "max_oi_pe": pin.max_oi_pe if pin.max_oi_pe is not None else 0,
            "k_ce": band.k_ce, "vwap_ce": band.vwap_ce or 0.0,
            "raw_upper": band.raw_upper or 0.0, "upper_level": band.upper_level or 0.0,
            "k_pe": band.k_pe, "vwap_pe": band.vwap_pe or 0.0,
            "raw_lower": band.raw_lower or 0.0, "lower_level": band.lower_level or 0.0,
            "basis": band.basis, "basis_mode": band.basis_mode, "pad": band.pad,
            "itm_depth": band.itm_depth, "round_to": band.round_to_step,
            "recalc_trigger": trigger, "freshness_age_secs": age,
            "regime": self.regime.name, "band_method": getattr(band, "band_method", "legacy"),
            "legacy_upper": getattr(band, "legacy_upper", None) if getattr(band, "legacy_upper", None) is not None else band.upper_level,
            "legacy_lower": getattr(band, "legacy_lower", None) if getattr(band, "legacy_lower", None) is not None else band.lower_level,
            "narrowed": bool(getattr(band, "narrowed", False)),
        }

    def _decision_row(self, ts, dr, decision=None, reason=None) -> dict:
        return {
            "ts": ts, "instruments": dr.instruments_vector, "raw_bias": dr.raw_bias,
            "bias_ewma": dr.bias_ewma, "bias_threshold": dr.bias_threshold,
            "routing_mode": self.routing_mode, "divergence_flag": dr.divergence_flag,
            "reversal_freeze_flag": dr.reversal_freeze_flag,
            "expiry_gate_flag": dr.expiry_gate_flag,
            "decision": decision if decision is not None else dr.decision,
            "reason_code": reason if reason is not None else dr.reason_code,
            "regime": self.regime.name,
            "feed_ok": bool(self._health is None or self._health.ok),
            "nearest_level_dist": self._nearest_dist if self._nearest_dist is not None else float("nan"),
        }

    def _emit_orders(self, rows, confluence=None):
        """Write order rows to order_tape AND append to the dashboard blotter (with the
        cross-instrument confluence that armed the trade)."""
        for row in rows:
            if row.get("action") == "REJECT":
                # an unaffordable / too-thin entry re-REJECTs every round (900 rows in 4 min, T27):
                # write each distinct reject to the binding order_tape once a minute at most
                k = (row.get("exit_reason"), row.get("strike"), row.get("right"))
                last = self._reject_seen.get(k)
                if last is not None and row["ts"] - last < 60_000:
                    continue
                self._reject_seen[k] = row["ts"]
            self.tape.append("order_tape", row)
            rec = {
                "ts": row["ts"], "action": row["action"], "right": row["right"],
                "strike": row["strike"], "qty": row["qty"], "fill": row["fill_price"],
                "slippage": row["slippage"], "pnl": row["pnl_realized"],
                "reason": row["exit_reason"], "live": row["live_orders_flag"],
            }
            src = confluence if (row["action"] == "ENTRY" and confluence) else self._last_confluence
            if src:
                rec["verdict"] = src.get("verdict")
                rec["same_side"] = src.get("same_side", [])
                rec["opp_side"] = src.get("opp_side", [])
                rec["conflicted"] = src.get("conflicted", False)
            self.trades_log.append(rec)

            # ROUND-TRIP journal (one row per trade): entry + exit together (#3).
            if row["action"] == "ENTRY":
                c = confluence or {}
                self._open_trade = {
                    "entry_ts": row["ts"], "exit_ts": None,
                    "direction": "UP" if row["right"] == "CE" else "DOWN", "right": row["right"],
                    "strike": row["strike"], "qty": row["qty"], "lots": row["qty"] // self.executor.lot_size,
                    "entry_price": round(row["fill_price"], 2), "exit_price": None,
                    "pnl": 0.0, "pnl_pct": 0.0, "exit_reason": None, "status": "OPEN",
                    "verdict": c.get("verdict"), "same_side": c.get("same_side", []),
                    "opp_side": c.get("opp_side", []), "conflicted": c.get("conflicted", False),
                }
            elif row["action"] == "EXIT" and self._open_trade is not None:
                t = self._open_trade
                t["exit_ts"] = row["ts"]; t["exit_price"] = round(row["fill_price"], 2)
                t["pnl"] = round(row["pnl_realized"], 2)
                # % derived from the canonical realized P&L (slippage/qty-aware) so it can never
                # disagree with the rupee column shown beside it on the blotter.
                t["pnl_pct"] = (round(row["pnl_realized"] / (t["entry_price"] * t["qty"]) * 100, 1)
                                if t["entry_price"] and t["qty"] else 0.0)
                t["exit_reason"] = row["exit_reason"]; t["status"] = "CLOSED"
                self.journal.append(t)
                self._open_trade = None

    # ---- ANTICIPATORY ENTRY (operator rule) --------------------------------
    def _approach_entry(self, band, snap, signals, dr):
        """Fire when NIFTY reaches within proximity_pts of a HELD level OR breaks through it.
        dist = level - spot for UP (>=0 approaching, <0 already breached); a clean break that
        gaps past the level in one frame is caught here too. NO arming latch — the one-position
        rule is what prevents re-entry, so a held breach is never silent."""
        if band is None or snap is None or band.upper_level is None or band.lower_level is None:
            return None
        spot, prox = snap.spot, self.entry_proximity_pts
        ul, ll = band.upper_level, band.lower_level
        # NO arming latch. While we are FLAT, a live approach/breach ALWAYS yields an entry
        # signal — the one-position rule (not position_active) is what prevents re-entry, so a
        # held breach is NEVER silent: enter, hold, exit, and on the next flat breached round
        # enter again. (operator: "enter on EVERY breach".)
        dist_up = ul - spot                      # >=0: below the upper (approaching) ; <0: BREACHED up
        dist_dn = spot - ll                      # >=0: above the lower (approaching) ; <0: BREACHED down
        up_zone = spot >= ul - prox              # within prox below, AT, or ABOVE the upper level
        dn_zone = spot <= ll + prox              # within prox above, AT, or BELOW the lower level
        if up_zone and (not dn_zone or dist_up <= dist_dn):
            d, side, dist, level = "UP", "UPPER", dist_up, ul
        elif dn_zone:
            d, side, dist, level = "DOWN", "LOWER", dist_dn, ll
        else:
            return None
        return {"direction": d, "side": side, "dist": dist, "level": level,
                "breached": dist < 0, "confluence": self._confluence(signals, d, dr)}

    def _confluence(self, signals, direction, dr):
        """The advance cross-instrument read: which instruments are breaching/approaching
        the SAME side as our trade vs the OPPOSITE side, weighted by importance."""
        want = 1 if direction == "UP" else -1
        same, opp = [], []
        same_pull = opp_pull = 0.0
        absolute_net_pull = 0.0                       # +ve = bloc pulling UP, -ve = DOWN (direction-INDEPENDENT)
        pulls_by_instrument: dict = {}
        for inst, sig in sorted(signals.items()):
            if inst == "NIFTY" or sig.dir == 0 or sig.state == "NEUTRAL":
                continue
            pull = self.decision.applied_weight(inst) * sig.strength
            pulls_by_instrument[inst] = round(pull, 4)   # the weighted voice of each instrument
            absolute_net_pull += pull * sig.dir          # signed by the instrument's OWN direction
            if sig.dir == want:
                same.append(inst); same_pull += pull
            else:
                opp.append(inst); opp_pull += pull
        same.sort(); opp.sort()
        confidence = abs(dr.bias_ewma) if _sign(dr.bias_ewma) == want else -abs(dr.bias_ewma)
        conflicted = bool(same) and bool(opp)
        # WEIGHT-AWARE verdict (spec 2.7/5.6 — a weighted chorus, not a head-count): a single
        # heavyweight bloc (e.g. BANKNIFTY) reads STRONG, while several featherweight stocks do not.
        net_pull = same_pull - opp_pull
        verdict = ("CONFLICTED" if conflicted else
                   "STRONG" if (same_pull >= self.strong_pull or len(same) >= 3) else
                   "MILD" if same else "LONE")
        return {"same_side": same, "opp_side": opp, "confidence": round(confidence, 3),
                "conflicted": conflicted, "verdict": verdict,
                "same_pull": round(same_pull, 4), "opp_pull": round(opp_pull, 4),
                "net_pull": round(net_pull, 4),                       # RELATIVE to our trade direction (gating only)
                "absolute_net_pull": round(absolute_net_pull, 4),    # ABSOLUTE UP(+)/DOWN(-) bloc lean (panel label)
                "pulls_by_instrument": pulls_by_instrument}

    def _approach_signal_row(self, ts, entry, conf) -> dict:
        return {
            "ts": int(ts), "direction": entry["direction"], "conviction": float(conf["confidence"]),
            "nifty_level_crossed": float(entry["level"]),
            "continuation_confirmed": False,         # anticipatory — we do NOT wait for the breach
            "confirm_secs_elapsed": 0.0,
            "channels_in_agreement": list(conf["same_side"]),
            "routing_mode": self.routing_mode, "lead_mode_flag": False,
        }

    def _compute_alarm(self, ts, band, snap, signals, dr):
        """The advance SCREAM: NIFTY about to breach which level, and the cross-instrument
        confluence telling you whether it likely will or won't."""
        pos = self.executor.position
        in_trade = self.executor.position_active
        prox = self.entry_proximity_pts
        direction = side = level = None
        dist = None
        triggered = False
        if band is not None and snap is not None and band.upper_level is not None and band.lower_level is not None:
            du, dl = band.upper_level - snap.spot, snap.spot - band.lower_level   # <0 == BREACHED that side
            up_zone = snap.spot >= band.upper_level - prox    # within prox below, AT, or ABOVE the upper
            dn_zone = snap.spot <= band.lower_level + prox     # within prox above, AT, or BELOW the lower
            triggered = up_zone or dn_zone
            if up_zone and (not dn_zone or abs(du) <= abs(dl)):
                direction, side, dist, level = "UP", "UPPER", du, band.upper_level
            elif dn_zone:
                direction, side, dist, level = "DOWN", "LOWER", dl, band.lower_level
            else:                                              # between the levels — show the nearer one
                direction, side, dist, level = (("UP", "UPPER", du, band.upper_level) if du <= dl
                                                else ("DOWN", "LOWER", dl, band.lower_level))
        atm = self._nifty_head.get("atm")
        # While IN A TRADE, read the confluence RELATIVE TO THE HELD POSITION's direction — NOT the
        # nearest band level. Otherwise, when spot drifts toward the OPPOSITE level, same_side/opp_side
        # /net_pull invert against the trade and the panel says the bloc supports a position it opposes
        # (council: fix #6 was wrong-direction). Flat: use the approach/nearest-level direction.
        conf_dir = pos.direction if (in_trade and pos is not None) else direction
        conf = self._confluence(signals, conf_dir, dr) if conf_dir else {}
        target = None
        if in_trade:
            state = "IN_TRADE"
            # Show the LIVE cross-instrument read each round (computed above off current signals),
            # not the frozen entry-time snapshot — the entry-time confluence is still logged to the
            # tapes via self._last_confluence; the panel must reflect what the bloc is doing NOW.
            if not conf and self._last_confluence:
                conf = self._last_confluence            # only fall back when there is no live direction
            msg = f"IN TRADE {pos.direction} {pos.right} {pos.strike} — trailing the stop to book the move"
        elif triggered and atm is not None:
            right = "CE" if direction == "UP" else "PE"
            target = {"action": "BUY", "instrument": "NIFTY", "right": right,
                      "strike": int(atm), "label": f"BUY NIFTY {int(atm)} {right}"}
            ss = ", ".join(conf["same_side"]) or "-"
            opp = f"  vs OPPOSITE [{', '.join(conf['opp_side'])}]" if conf.get("opp_side") else ""
            # If we tried to enter this round but COULDN'T, say WHY out loud (never a silent breach).
            eb = self._entry_block
            if eb == "INSUFFICIENT_CAPITAL":
                why = f"  [NO TRADE: out of capital, Rs {self.executor.capital:,.0f} left]"
            elif eb == "PREMIUM_TOO_THIN":
                fl = self.executor.min_premium_expiry if self.expiry.is_expiry_day else self.executor.min_premium_nonexpiry
                why = f"  [NO TRADE: {right} premium below your Rs {fl:.0f} floor]"
            elif self._entry_gate not in (None, Reason.NO_APPROACH, Reason.POSITION_ACTIVE):
                # 2.0.1 (T29): never show a bare "BUY" while a gate blocks the entry
                why = f"  [NO TRADE: {self._GATE_TEXT.get(self._entry_gate, self._entry_gate)}]"
                target = None
            else:
                why = ""
            if why:
                target = None            # 2.1: never publish a BUY target the entry itself rejected (thin premium /
            #                              out of capital) -- the cockpit showed a green BUY on exactly those rounds
            act = target['label'] if (target and not why) else f"{right} (blocked)" if why else f"BUY {right}"
            pulls = f" (pull {conf.get('net_pull', 0.0):+.2f}: +{conf.get('same_pull', 0.0):.2f}/-{conf.get('opp_pull', 0.0):.2f})"
            if dist < 0:                                       # already through the level
                state = "BREACHED"
                msg = (f"BREACHED {side} {level:.0f} by {abs(dist):.1f} pts -> {act}{why}  |  "
                       f"{conf['verdict']}{pulls}: same-side [{ss}]{opp}")
            else:
                state = "ABOUT_TO_BREACH"
                msg = (f"ABOUT TO BREACH {side} {level:.0f} ({dist:.1f} pts away) -> {act}{why}  |  "
                       f"{conf['verdict']}{pulls}: same-side [{ss}]{opp}")
        elif dist is not None and dist <= prox * 2.5:
            state = "WATCHING"
            msg = f"WATCHING — NIFTY {dist:.1f} pts from its {side} pin-risk level"
        else:
            state, msg = "STAND_BY", "STAND-BY — no NIFTY level in range; tracking the universe"
        return {"state": state, "lean": direction, "in_trade": in_trade,
                "message": msg, "target": target,
                "entry_block": self._entry_block,          # ENTERED | reject reason | None (for the cockpit badge)
                "dist_to_level": round(dist, 2) if dist is not None else None,
                "side": side, "proximity_pts": prox,
                "confluence": {"verdict": conf.get("verdict"), "same_side": conf.get("same_side", []),
                               "opp_side": conf.get("opp_side", []), "confidence": conf.get("confidence"),
                               "conflicted": conf.get("conflicted", False),
                               "same_pull": conf.get("same_pull", 0.0), "opp_pull": conf.get("opp_pull", 0.0),
                               "net_pull": conf.get("net_pull", 0.0),
                               # drives the NIFTY-LEAN label; None (not 0.0) when there is no read at all (2.1)
                               "absolute_net_pull": conf.get("absolute_net_pull") if conf else None,
                               "pulls_by_instrument": conf.get("pulls_by_instrument", {})}}

    _GATE_TEXT = {
        "POSITION_ACTIVE": "in a trade",
        "BEFORE_TRADE_START": "before trade start",
        "AFTER_LAST_ENTRY": "past the entry window (flatten time) — managing exits only",
        "NO_LEVELS": "no NIFTY levels yet",
        "FEED_UNHEALTHY": "NIFTY feed unhealthy — entries + re-anchors paused",
        "LEVELS_STALE": "held levels are stale (feed gap) — waiting for a fresh re-anchor",
        "EXPIRY_GATE_BLOCKED": "non-expiry trading is switched off (routing.nonexpiry_mode)",
        "POST_STOP_COOLDOWN": "post-stop cooldown",
        "POST_PROFIT_COOLDOWN": "short pause after a profit",
        "NO_APPROACH": "waiting for NIFTY to reach a level",
    }

    def _idle_diagnostics(self, ts) -> dict:
        """WHY NO TRADE (2.0): the first closed gate, how far the levels are, and when they move."""
        st = self.states.get("NIFTY")
        band = st.band if st else None
        spot = (self._nifty_head or {}).get("spot")
        out = {"gate": self._entry_gate, "text": self._GATE_TEXT.get(self._entry_gate or "", self._entry_gate),
               "nearest_side": None, "nearest_dist": None, "nearest_pct": None,
               "half_width": None, "legacy_half_width": None, "band_method": None,
               "next_reanchor_secs": None, "proximity_pts": self.entry_proximity_pts}
        if band is not None and spot is not None and band.upper_level is not None and band.lower_level is not None:
            du, dl = band.upper_level - spot, spot - band.lower_level
            side, d = ("UPPER", du) if du <= dl else ("LOWER", dl)
            out.update({"nearest_side": side, "nearest_dist": round(d, 1),
                        "nearest_pct": round(100.0 * d / spot, 3) if spot else None,
                        "half_width": round((band.upper_level - band.lower_level) / 2, 1),
                        "band_method": getattr(band, "band_method", "legacy")})
            lu, ll = getattr(band, "legacy_upper", None), getattr(band, "legacy_lower", None)
            if lu is not None and ll is not None:
                out["legacy_half_width"] = round((lu - ll) / 2, 1)
        if st is not None:
            age = st.freshness.age_secs(ts)
            out["next_reanchor_secs"] = max(0, round(st.freshness.hold_min_secs - age))
        return out

    def _capital_block(self, pos) -> dict:
        ex = self.executor
        cap = ex.capital                                   # account capital (self.capital += pnl on each close)
        deployed = pos.cost if (pos and pos.open) else 0.0
        unreal = (pos.last_premium - pos.entry_fill) * pos.qty_remaining if (pos and pos.open) else 0.0
        # self.capital is NOT decremented on entry (the all-in/one-position model adds pnl on close),
        # so the truly-LIQUID cash while a trade is open is capital - deployed. Reporting `cap` raw
        # double-counted the deployed amount (cash 100k AND deployed 100k). equity is unchanged:
        # (cap - deployed) + deployed + unrealized == cap + unrealized.
        liquid_cash = cap - deployed
        equity = cap + unreal
        start = ex.starting_capital
        # session equity high-watermark + drawdown from it — display-only (the operator was doing
        # this arithmetic in their head from the journal); never feeds any trading decision.
        self._equity_high = max(getattr(self, "_equity_high", 0.0), start, equity)
        dd_pct = (100.0 * (self._equity_high - equity) / self._equity_high) if self._equity_high else 0.0
        return {
            "starting": round(start, 2), "cash": round(liquid_cash, 2), "unrealized": round(unreal, 2),
            "equity": round(equity, 2), "pnl": round(equity - start, 2),
            "pnl_pct": round(100.0 * (equity - start) / start, 2) if start else 0.0,
            "deployed": round(deployed, 2),
            "equity_high": round(self._equity_high, 2), "dd_pct": round(dd_pct, 2),
        }

    def _build_chain(self, snap, atm, step, n=6) -> list:
        """NIFTY option-chain ladder (ATM +/- n) for the dashboard."""
        rows = []
        for k in range(-n, n + 1):
            K = int(atm + k * step)
            ce, pe = snap.ce(K), snap.pe(K)
            rows.append({
                "strike": K, "is_atm": K == atm, "off": k,
                "ce_ltp": round(ce.ltp, 2) if ce else None, "ce_vwap": round(ce.vwap, 2) if ce else None,
                "ce_oi": ce.oi if ce else None, "ce_vol": ce.volume if ce else None,
                "ce_bid": round(ce.bid, 2) if ce else None, "ce_ask": round(ce.ask, 2) if ce else None,
                "pe_ltp": round(pe.ltp, 2) if pe else None, "pe_vwap": round(pe.vwap, 2) if pe else None,
                "pe_oi": pe.oi if pe else None, "pe_vol": pe.volume if pe else None,
                "pe_bid": round(pe.bid, 2) if pe else None, "pe_ask": round(pe.ask, 2) if pe else None,
            })
        return rows

    def _update_view(self, ts, instruments_view, dr, arming):
        pos = self.executor.position
        # round-trip journal: closed trades + the live open trade
        journal = list(self.journal)
        if self._open_trade is not None and pos is not None and pos.open:
            ot = dict(self._open_trade)
            ot["exit_price"] = round(pos.last_premium, 2)
            ot["pnl"] = round((pos.last_premium - ot["entry_price"]) * pos.qty_remaining, 2)
            # live % derived from the same unrealized rupee figure above (one consistent story).
            ot["pnl_pct"] = (round(ot["pnl"] / (ot["entry_price"] * pos.qty_remaining) * 100, 1)
                             if ot["entry_price"] and pos.qty_remaining else 0.0)
            journal.append(ot)
        closed = [t for t in self.journal]
        realized = round(sum(t["pnl"] for t in closed), 2)
        wins = sum(1 for t in closed if t["pnl"] > 0)
        losses = sum(1 for t in closed if t["pnl"] <= 0)     # 2.1: a 0 P&L is a loss everywhere (as the sound says)
        # session-health: win-rate, current W/L streak (+N wins / -N losses), avg win/avg loss
        win_rate = round(100.0 * wins / len(closed), 1) if closed else 0.0
        _w = [t["pnl"] for t in closed if t["pnl"] > 0]
        _l = [t["pnl"] for t in closed if t["pnl"] < 0]
        avg_win = round(sum(_w) / len(_w), 0) if _w else 0.0
        avg_loss = round(sum(_l) / len(_l), 0) if _l else 0.0
        streak = 0
        for t in reversed(closed):
            s = 1 if t["pnl"] > 0 else -1
            if streak != 0 and (s > 0) != (streak > 0):
                break
            streak += s

        # Burst board: every instrument ranked by how close it is to bursting
        # through a pin-risk level (breached first, then approaching, then by proximity).
        board = [{"instrument": k, "spot": v["spot"], "state": v["state"], "dir": v["dir"],
                  "dist_up": v["dist_up"], "dist_dn": v["dist_dn"], "heat": v["heat"],
                  "breached": v["breached"], "breach_ts": v.get("breach_ts"),
                  "weight": v["weight"], "strength": v["strength"],
                  "stale": bool(v.get("stale"))}
                 for k, v in instruments_view.items()]
        # stale (carried/N-A) instruments sink below every live one — a stale last-known heat must
        # not outrank a live instrument; they stay visible at the bottom flagged STALE.
        board.sort(key=lambda x: (x["stale"], -x["heat"], -(x["weight"] or 0), x["instrument"]))
        # Breach alerts: instruments that have ALREADY breached a level — LIVE only (never alarm on
        # a carried-forward stale breach).
        breach_alerts = [b for b in board if b["breached"] and not b["stale"]]
        # ordered by the MOST RECENT breach first (latest breach_ts at the top), heaviest as tiebreak.
        breach_alerts.sort(key=lambda x: (-(x.get("breach_ts") or 0), -(x["weight"] or 0), x["instrument"]))

        # Post-stop cooldown, STANDING for the whole window — not contingent on price being near a
        # level this round (that was the bug: the old cockpit badge only rendered on the coincidental
        # round an approach fired, so the screen looked idle for most of the 11 minutes). Post-profit
        # stays deliberately absent here — operator rule: SILENT, no banner, ever.
        _stop_left = max(0, round((self.post_stop_cooldown_ms - (ts - self._last_stop_ms)) / 1000))
        cooldown = {"stop_cooldown_active": bool(self._stop_cooldown_active),
                    "stop_cooldown_remaining_secs": _stop_left if self._stop_cooldown_active else 0}

        self.latest_state = {
            "round_ts": ts,
            "nifty": dict(self._nifty_head),
            "nifty_chain": list(self._nifty_chain),
            "arming": arming,
            "cooldown": cooldown,
            "burst_board": board,
            "breach_alerts": breach_alerts,
            "events": list(self._recent_events),       # recent FEED_GAP / rejections / anomalies strip
            "levels_stale": self._levels_stale,        # held NIFTY U/L levels failed to re-anchor
            "level_history": self._level_history[-80:],  # full log of pin-risk recomputes (time + inputs)
            "instruments": instruments_view,
            "decision": {
                "decision": (self._last_decision or {}).get("decision", dr.decision),
                "reason": (self._last_decision or {}).get("reason", dr.reason_code),
                "bias_ewma": round(dr.bias_ewma, 4), "raw_bias": round(dr.raw_bias, 4),
                "conviction": round(dr.conviction, 4), "threshold": dr.bias_threshold,
                "routing_mode": self.routing_mode, "divergence": dr.divergence_flag,
                "reversal_freeze": dr.reversal_freeze_flag, "expiry_gate": dr.expiry_gate_flag,
                "channels": dr.channels_in_agreement, "lead": dr.lead_mode_flag,
            },
            "position": None if (pos is None or not pos.open) else {   # a CLOSED position is flat (T28)
                "open": pos.open, "direction": pos.direction, "right": pos.right,
                "strike": pos.strike, "lots": pos.lots, "qty_remaining": pos.qty_remaining,
                "entry_ts": pos.entry_ts,                                   # 2.1: time-in-trade source
                "qty_total": pos.qty_total, "entry_fill": round(pos.entry_fill, 2),
                "last_premium": round(pos.last_premium, 2), "peak_premium": round(pos.peak_premium, 2),
                "trail_floor": round(pos.trail_floor, 2), "trail_armed": pos.trail_armed,
                "cost": round(pos.cost, 2),
                "unrealized_pnl": round((pos.last_premium - pos.entry_fill) * pos.qty_remaining, 2),
                "realized_pnl": round(pos.realized_pnl, 2),
                # DISPLAY of the locked exit set (not a change to it): the threshold prices the
                # trader is being stopped against, so the cockpit can show distance-to-next-stop.
                "exit_plan": {
                    "hard_sl": round(pos.entry_fill * (1 - self.executor.hard_sl_pct), 2),
                    "trail_activate": round(pos.entry_fill * (1 + self.executor.trail_activate_pct), 2),
                    "min_profit": round(pos.entry_fill * (1 + self.executor.min_profit_pct), 2),
                },
            },
            "capital": self._capital_block(pos),
            "journal": journal[-80:],
            "trades_summary": {"trades_today": len(closed) + (1 if (self._open_trade and pos and pos.open) else 0),
                               "closed": len(closed), "wins": wins, "losses": losses, "realized_pnl": realized,
                               "win_rate_pct": win_rate, "streak": streak,
                               "avg_win": avg_win, "avg_loss": avg_loss},
            "session_date": self.expiry.session_date.isoformat(),
            "expiry": {
                "date": self.expiry.expiry_date.isoformat(),
                "basis_mode": self.expiry.basis_mode,
                "is_expiry_day": self.expiry.is_expiry_day,
                "is_monthly": self.expiry.is_monthly,
                "shifted_from_holiday": _shift_label(self.expiry),
            },
            "live_orders": self.cfg.live_orders,
            # ---- VAJRA 2.0 ----
            "version": "2.0",
            "boot_id": self._boot_id,
            "regime": {**self.regime.as_dict(), "last_entry": self.entry_cutoff, "closing": self.closing.as_dict(),
                       "entries_today": self._entries_today},
            "feed_health": (self._health.as_dict() if self._health is not None
                            else {"ok": True, "reason": None, "spot_unchanged_secs": 0.0, "basis": None}),
            "idle": self._idle_diagnostics(ts),
            "sound_events": self.sounds.snapshot(),
            "tape_errors": (lambda v: v if isinstance(v, int) else 0)(getattr(self.tape, "disk_errors", 0)),
            # ---- 2.1 ----
            "ws": self._feed_health_view(),              # broker socket + subscription health (live only)
            "power": (dict(self._power) if self._power else None),   # {on_battery, battery_pct, ac} (live, Windows)
            "round": {"proc_ms": round((time.perf_counter() - self._round_t0) * 1000, 1),
                      "published_ms": int(time.time() * 1000), "gc": self.__dict__.get("gc_info")},
        }


def _shift_label(expiry):
    """Label for a holiday-shifted expiry: the NOMINAL weekday it moved from (2.1: it is a bool in
    ExpiryResolution; the old code always printed "Tue" regardless of the configured weekday)."""
    v = getattr(expiry, "shifted_from_holiday", None)
    if not v:
        return None
    try:
        return v.strftime("%a %d %b")
    except AttributeError:
        wd = str(getattr(expiry, "nominal_weekday", "") or "").upper()
        names = {"MON": "Mon", "TUE": "Tue", "WED": "Wed", "THU": "Thu", "FRI": "Fri"}
        return names.get(wd, wd.title() if wd else "holiday")


__all__ = ["Orchestrator", "InstrumentState"]
