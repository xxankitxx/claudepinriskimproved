"""§5.6 Cross-Instrument Decision-Maker — the gate.

Stateful, EWMA-decaying directional-bias engine (consistent with the MYTHOS R5
Decision-Maker), re-implemented here independently.

    w(NIFTY)         = w_self                       (default 1.0)
    w(financial bloc)= w_bloc                        (BANKNIFTY & FINNIFTY SHARE this
                                                      single weight, scaled by agreement,
                                                      NOT summed at full weight)
    w(heavy stock)   = NIFTY weight% / 100

    raw_bias  = Σ w(i) * dir(i) * strength(i)
    BIAS_EWMA = ewma(raw_bias, halflife = bias_ewma_hl)     # default 3 min, stateful

  * Divergence suppression: NIFTY breaking against the bloc -> conviction *= (1-penalty).
  * Reversal-watch coupling: an ABRUPT basis move against BIAS_EWMA freezes new
    entries for reversal_cooldown (default 2 min).

Emits the full per-instrument state vector, weights, raw_bias, BIAS_EWMA, every
gate flag and the fire/no-fire decision with a typed reason -> decision_tape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..mathx import TimeEWMA
from ..schema import Reason
from .continuation_validator import ContinuationSignal

EPS = 1e-9


def _sign(x: float) -> int:
    if x > EPS:
        return 1
    if x < -EPS:
        return -1
    return 0


@dataclass
class DecisionResult:
    decision: str                      # FIRE_UP | FIRE_DOWN | HOLD
    reason_code: str
    direction: str | None              # UP | DOWN | None
    raw_bias: float
    bias_ewma: float
    conviction: float                  # post-divergence magnitude used for the gate
    bias_threshold: float              # active threshold (lead threshold in LEAD fires)
    divergence_flag: bool
    reversal_freeze_flag: bool
    expiry_gate_flag: bool             # True when expiry gating PERMITS firing
    lead_mode_flag: bool
    channels_in_agreement: list[str] = field(default_factory=list)
    instruments_vector: list[dict] = field(default_factory=list)


class DecisionMaker:
    def __init__(
        self,
        weight_table: dict[str, float],        # instrument -> decision weight (stocks)
        w_self: float = 1.0,
        w_bloc: float = 0.35,
        bias_ewma_halflife_min: float = 3.0,
        divergence_penalty: float = 0.5,
        reversal_cooldown_min: float = 2.0,
        bloc_instruments: tuple[str, ...] = ("BANKNIFTY", "FINNIFTY"),
        self_instrument: str = "NIFTY",
    ):
        self.weight_table = dict(weight_table)
        self.w_self = float(w_self)
        self.w_bloc = float(w_bloc)
        self.divergence_penalty = float(divergence_penalty)
        self.reversal_cooldown_secs = float(reversal_cooldown_min) * 60.0
        self.bloc_instruments = tuple(bloc_instruments)
        self.self_instrument = self_instrument

        self._bias_ewma = TimeEWMA(halflife_seconds=bias_ewma_halflife_min * 60.0)
        self._freeze_until_ms: int | None = None

    # ---- weight resolution --------------------------------------------------
    def applied_weight(self, instrument: str, agreement_scale: float = 1.0) -> float:
        if instrument == self.self_instrument:
            return self.w_self
        if instrument in self.bloc_instruments:
            # Split the single shared bloc weight across the two bloc members,
            # scaled by their agreement (NOT summed at full weight).
            return self.w_bloc * 0.5 * agreement_scale
        return float(self.weight_table.get(instrument, 0.0))

    # ---- main evaluation ----------------------------------------------------
    def evaluate(
        self,
        ts_ms: int,
        signals: dict[str, ContinuationSignal],
        *,
        abrupt_against_bias: bool,
        expiry_gate_ok: bool,
        routing_mode: str,
        bias_threshold: float,
        bias_threshold_lead: float,
        position_active: bool,
    ) -> DecisionResult:
        # --- bloc aggregation + agreement ----------------------------------
        bloc_vals = []
        for b in self.bloc_instruments:
            s = signals.get(b)
            if s is not None:
                bloc_vals.append(s.dir * s.strength)
        bank = bloc_vals[0] if len(bloc_vals) > 0 else 0.0
        fin = bloc_vals[1] if len(bloc_vals) > 1 else 0.0
        if _sign(bank) != 0 and _sign(fin) != 0:
            agreement_scale = 1.0 if _sign(bank) == _sign(fin) else 0.3
        elif _sign(bank) == 0 and _sign(fin) == 0:
            agreement_scale = 1.0
        else:
            agreement_scale = 0.6
        bloc_signal = (bank + fin) / 2.0
        bloc_dir = _sign(bloc_signal)

        # --- raw bias --------------------------------------------------------
        raw_bias = 0.0
        vector: list[dict] = []
        for inst, sig in sorted(signals.items()):
            w = self.applied_weight(inst, agreement_scale)
            contrib = w * sig.dir * sig.strength
            raw_bias += contrib
            vector.append({
                "instrument": inst,
                "state": sig.state,
                "dir": int(sig.dir),
                "strength": float(sig.strength),
                "weight": float(w),
            })
        vector.sort(key=lambda d: d["instrument"])   # deterministic ordering

        bias_ewma = self._bias_ewma.update(ts_ms / 1000.0, raw_bias)

        # --- divergence suppression -----------------------------------------
        nifty_sig = signals.get(self.self_instrument)
        nifty_dir = nifty_sig.dir if nifty_sig else 0
        divergence = (_sign(nifty_dir) != 0 and bloc_dir != 0 and _sign(nifty_dir) != bloc_dir)
        conviction = abs(bias_ewma)
        if divergence:
            conviction *= (1.0 - self.divergence_penalty)

        # --- reversal-watch freeze ------------------------------------------
        if abrupt_against_bias:
            self._freeze_until_ms = int(ts_ms + self.reversal_cooldown_secs * 1000)
        reversal_freeze = self._freeze_until_ms is not None and ts_ms < self._freeze_until_ms

        # --- direction candidates -------------------------------------------
        bias_sign = _sign(bias_ewma)

        def _channels(direction_sign: int) -> list[str]:
            out = [v["instrument"] for v in vector
                   if v["dir"] == direction_sign and v["strength"] > 0.0]
            return sorted(out)

        # Default HOLD result builder.
        def _hold(reason: str, lead_flag: bool = False, active_threshold: float | None = None) -> DecisionResult:
            return DecisionResult(
                decision="HOLD", reason_code=reason, direction=None,
                raw_bias=raw_bias, bias_ewma=bias_ewma, conviction=conviction,
                bias_threshold=(active_threshold if active_threshold is not None else bias_threshold),
                divergence_flag=divergence, reversal_freeze_flag=reversal_freeze,
                expiry_gate_flag=expiry_gate_ok, lead_mode_flag=lead_flag,
                channels_in_agreement=_channels(bias_sign), instruments_vector=vector,
            )

        # One regime at a time — no new entry while a position is live (§10).
        if position_active:
            return _hold(Reason.POSITION_ACTIVE)

        # --- CONFIRM path (mandatory NIFTY own fresh break + continuation) ---
        nifty_confirmed_up = nifty_sig is not None and nifty_sig.state == "CONFIRM_UP"
        nifty_confirmed_down = nifty_sig is not None and nifty_sig.state == "CONFIRM_DOWN"

        if nifty_confirmed_up or nifty_confirmed_down:
            direction = 1 if nifty_confirmed_up else -1
            dir_word = "UP" if direction == 1 else "DOWN"
            # (3) bias gate
            if bias_sign != direction:
                return _hold(Reason.BIAS_SIGN_MISMATCH)
            if conviction < bias_threshold:
                return _hold(Reason.BIAS_BELOW_THRESHOLD)
            # (4) reversal freeze
            if reversal_freeze:
                return _hold(Reason.REVERSAL_FREEZE)
            # (5) expiry gate
            if not expiry_gate_ok:
                return _hold(Reason.EXPIRY_GATE_BLOCKED)
            return DecisionResult(
                decision="FIRE_UP" if direction == 1 else "FIRE_DOWN",
                reason_code=(Reason.DIVERGENCE_DAMPED if divergence else Reason.FIRE_CONFIRM),
                direction=dir_word, raw_bias=raw_bias, bias_ewma=bias_ewma,
                conviction=conviction, bias_threshold=bias_threshold,
                divergence_flag=divergence, reversal_freeze_flag=reversal_freeze,
                expiry_gate_flag=expiry_gate_ok, lead_mode_flag=False,
                channels_in_agreement=_channels(direction), instruments_vector=vector,
            )

        # --- LEAD path (opt-in): overwhelming chorus may fire pre-break ------
        if routing_mode == "LEAD" and bias_sign != 0 and conviction >= bias_threshold_lead:
            direction = bias_sign
            dir_word = "UP" if direction == 1 else "DOWN"
            if reversal_freeze:
                return _hold(Reason.REVERSAL_FREEZE, lead_flag=True, active_threshold=bias_threshold_lead)
            if not expiry_gate_ok:
                return _hold(Reason.EXPIRY_GATE_BLOCKED, lead_flag=True, active_threshold=bias_threshold_lead)
            return DecisionResult(
                decision="FIRE_UP" if direction == 1 else "FIRE_DOWN",
                reason_code=Reason.FIRE_LEAD, direction=dir_word,
                raw_bias=raw_bias, bias_ewma=bias_ewma, conviction=conviction,
                bias_threshold=bias_threshold_lead, divergence_flag=divergence,
                reversal_freeze_flag=reversal_freeze, expiry_gate_flag=expiry_gate_ok,
                lead_mode_flag=True, channels_in_agreement=_channels(direction),
                instruments_vector=vector,
            )

        # --- no fire: pick the most informative reason ----------------------
        if nifty_sig is not None and nifty_sig.breached and not nifty_sig.confirmed:
            return _hold(Reason.NO_CONTINUATION)
        return _hold(Reason.NO_FRESH_BREACH)


__all__ = ["DecisionMaker", "DecisionResult"]
