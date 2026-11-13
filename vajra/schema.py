"""Tape schemas (spec §9) — the single definition of every tape's columns/dtypes.

Each tape is defined ONCE here as a pyarrow schema so that:
  * the tape writer (vajra/tape/writer.py) serialises with correct dtypes,
  * verify_vajra.py (§11.2) asserts the exact specified columns + ms timestamps
    + schema_version against THIS module rather than a hand-copied list.

`ts` is epoch-milliseconds (int64) on every tape (spec §9: "millisecond
-timestamped").  Every row also carries `schema_version` (spec §9).

Each schema contains EXACTLY the columns named in the spec section plus the
two universal columns `ts` and `schema_version`.  No silent extra columns.
"""

from __future__ import annotations

import pyarrow as pa

from . import SCHEMA_VERSION

# --- shared enums / reason codes --------------------------------------------

BASIS_MODES = ("IMPLIED", "ACTUAL")
RIGHTS = ("CE", "PE")
RECALC_TRIGGERS = ("prev_close_seed", "open", "interval", "post_exit", "post_cooldown", "held")
BAND_METHODS = ("legacy", "mtm")
REGIMES = ("EXPIRY", "NON_EXPIRY")
DEC_STATES = (
    "NEUTRAL", "APPROACH_UP", "APPROACH_DOWN",
    "BREACH_UP", "BREACH_DOWN", "CONFIRM_UP", "CONFIRM_DOWN",
)
DECISIONS = ("FIRE_UP", "FIRE_DOWN", "HOLD")
ORDER_ACTIONS = ("ENTRY", "EXIT", "REJECT")
ROUTING_MODES = ("CONFIRM", "LEAD")

# Typed reason codes (§0.4 — never fabricate; log a typed reason instead).
class Reason:
    # decision_tape
    NO_FRESH_BREACH = "NO_FRESH_BREACH"
    NO_CONTINUATION = "NO_CONTINUATION"
    BIAS_BELOW_THRESHOLD = "BIAS_BELOW_THRESHOLD"
    BIAS_SIGN_MISMATCH = "BIAS_SIGN_MISMATCH"
    REVERSAL_FREEZE = "REVERSAL_FREEZE"
    EXPIRY_GATE_BLOCKED = "EXPIRY_GATE_BLOCKED"
    DIVERGENCE_DAMPED = "DIVERGENCE_DAMPED"
    FIRE_CONFIRM = "FIRE_CONFIRM"
    FIRE_LEAD = "FIRE_LEAD"
    APPROACH_ENTRY = "APPROACH_ENTRY"      # anticipatory entry: fired on a 5-7pt approach
    NO_APPROACH = "NO_APPROACH"
    POSITION_ACTIVE = "POSITION_ACTIVE"
    POST_STOP_COOLDOWN = "POST_STOP_COOLDOWN"   # no new trade for N s after a hard-stop (SL_HIT) loss
    POST_PROFIT_COOLDOWN = "POST_PROFIT_COOLDOWN"  # silent no-trade window after a profitable exit
    # 2.0 entry gates (the ordered "why no trade" reasons)
    ENTRY_RETREATING = "ENTRY_RETREATING"       # 2.2: NIFTY is moving AWAY from the level -> wait
    REBUY_WAIT = "REBUY_WAIT"                   # 2.2: after a profit, same-side re-buy waits for a new extreme
    BREACH_TOO_DEEP = "BREACH_TOO_DEEP"         # 2.2: spot already far through the level (optional guard)
    GAMMA_CONFIRM_WAIT = "GAMMA_CONFIRM_WAIT"   # 2.4: low gamma -> enter only once the level has actually broken
    GAMMA_TOO_TIGHT = "GAMMA_TOO_TIGHT"         # 2.4: the stop is only a few index points away (noise would hit it)
    RETRY_NEEDS_BREAK = "RETRY_NEEDS_BREAK"     # 2.4: last same-side attempt failed -> the level must break first
    BEFORE_TRADE_START = "BEFORE_TRADE_START"
    AFTER_LAST_ENTRY = "AFTER_LAST_ENTRY"
    NO_LEVELS = "NO_LEVELS"
    FEED_UNHEALTHY = "FEED_UNHEALTHY"
    LEVELS_STALE = "LEVELS_STALE"
    # executor reject reasons that can reach decision_tape.reason_code
    NO_QUOTE = "NO_QUOTE"
    PREMIUM_TOO_THIN = "PREMIUM_TOO_THIN"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    ORDER_PLACEMENT_FAILED = "ORDER_PLACEMENT_FAILED"
    # feed/event gaps
    STALE_CHAIN = "STALE_CHAIN"
    MISSING_TICK = "MISSING_TICK"
    MISSING_VWAP = "MISSING_VWAP"
    MISSING_ATM_QUOTE = "MISSING_ATM_QUOTE"
    NO_FUTURE_PRICE = "NO_FUTURE_PRICE"
    SESSION_ERROR = "SESSION_ERROR"


# --- nested struct for tick_tape per-strike rows (§9.1) ---------------------

_STRIKE_STRUCT = pa.struct([
    pa.field("strike", pa.int64()),
    pa.field("right", pa.string()),          # CE | PE
    pa.field("ltp", pa.float64()),
    pa.field("vwap", pa.float64()),
    pa.field("vwap_source", pa.string()),    # feed | computed
    pa.field("volume", pa.int64()),
    pa.field("oi", pa.int64()),
    pa.field("bid", pa.float64()),
    pa.field("ask", pa.float64()),
])

# --- per-instrument struct for decision_tape (§9.4) -------------------------

_DEC_INSTR_STRUCT = pa.struct([
    pa.field("instrument", pa.string()),
    pa.field("state", pa.string()),
    pa.field("dir", pa.int64()),             # +1 / 0 / -1
    pa.field("strength", pa.float64()),      # [0,1]
    pa.field("weight", pa.float64()),
])


def _ts() -> pa.Field:
    return pa.field("ts", pa.int64())        # epoch milliseconds


def _sv() -> pa.Field:
    return pa.field("schema_version", pa.string())


# --- §9.1 tick_tape ----------------------------------------------------------
TICK_TAPE = pa.schema([
    _ts(),
    pa.field("instrument", pa.string()),
    pa.field("spot", pa.float64()),
    pa.field("atm_strike", pa.int64()),
    pa.field("future", pa.float64()),
    pa.field("basis", pa.float64()),
    pa.field("basis_mode", pa.string()),
    pa.field("vix", pa.float64()),
    pa.field("strikes", pa.list_(_STRIKE_STRUCT)),
    _sv(),
])

# --- §9.2 level_tape ---------------------------------------------------------
LEVEL_TAPE = pa.schema([
    _ts(),
    pa.field("instrument", pa.string()),
    pa.field("atm_strike", pa.int64()),
    pa.field("max_oi_ce", pa.int64()),
    pa.field("max_oi_pe", pa.int64()),
    pa.field("k_ce", pa.int64()),
    pa.field("vwap_ce", pa.float64()),
    pa.field("raw_upper", pa.float64()),
    pa.field("upper_level", pa.float64()),
    pa.field("k_pe", pa.int64()),
    pa.field("vwap_pe", pa.float64()),
    pa.field("raw_lower", pa.float64()),
    pa.field("lower_level", pa.float64()),
    pa.field("basis", pa.float64()),
    pa.field("basis_mode", pa.string()),
    pa.field("pad", pa.float64()),
    pa.field("itm_depth", pa.int64()),
    pa.field("round_to", pa.float64()),
    pa.field("recalc_trigger", pa.string()),     # prev_close_seed | open | interval | post_exit | post_cooldown | held
    pa.field("freshness_age_secs", pa.float64()),
    # ---- 2.0 ----
    pa.field("regime", pa.string()),             # EXPIRY | NON_EXPIRY
    pa.field("band_method", pa.string()),        # legacy | mtm
    pa.field("legacy_upper", pa.float64()),      # what the 1.x formula gave (narrow-only audit)
    pa.field("legacy_lower", pa.float64()),
    pa.field("narrowed", pa.bool_()),
    _sv(),
])

# --- §9.3 basis_tape ---------------------------------------------------------
BASIS_TAPE = pa.schema([
    _ts(),
    pa.field("instrument", pa.string()),
    pa.field("spot", pa.float64()),
    pa.field("future", pa.float64()),
    pa.field("basis", pa.float64()),
    pa.field("basis_mode", pa.string()),
    pa.field("d_basis", pa.float64()),
    pa.field("d_basis_ewma", pa.float64()),
    pa.field("abrupt_flag", pa.bool_()),
    pa.field("basis_signal_weight", pa.float64()),
    # ---- 2.0.4 ----
    pa.field("index_spot", pa.float64()),        # raw NIFTY index print (frozen / indicative after 15:15)
    pa.field("spot_source", pa.string()),        # INDEX | CLOSING_PROXY (spot = forward - basis_ref)
    pa.field("parity_basis", pa.float64()),      # NIFTY pre-freeze: parity forward - index (basis_ref source)
    _sv(),
])

# --- §9.4 decision_tape ------------------------------------------------------
DECISION_TAPE = pa.schema([
    _ts(),
    pa.field("instruments", pa.list_(_DEC_INSTR_STRUCT)),   # the per-instrument state vector
    pa.field("raw_bias", pa.float64()),
    pa.field("bias_ewma", pa.float64()),
    pa.field("bias_threshold", pa.float64()),
    pa.field("routing_mode", pa.string()),
    pa.field("divergence_flag", pa.bool_()),
    pa.field("reversal_freeze_flag", pa.bool_()),
    pa.field("expiry_gate_flag", pa.bool_()),
    pa.field("decision", pa.string()),           # FIRE_UP | FIRE_DOWN | HOLD
    pa.field("reason_code", pa.string()),
    # ---- 2.0 ----
    pa.field("regime", pa.string()),
    pa.field("feed_ok", pa.bool_()),
    pa.field("nearest_level_dist", pa.float64()),   # pts from NIFTY spot to the nearer held level (NaN if none)
    _sv(),
])

# --- §9.5 signal_tape --------------------------------------------------------
SIGNAL_TAPE = pa.schema([
    _ts(),
    pa.field("direction", pa.string()),          # UP | DOWN
    pa.field("conviction", pa.float64()),
    pa.field("nifty_level_crossed", pa.float64()),
    pa.field("continuation_confirmed", pa.bool_()),
    pa.field("confirm_secs_elapsed", pa.float64()),
    pa.field("channels_in_agreement", pa.list_(pa.string())),
    pa.field("routing_mode", pa.string()),
    pa.field("lead_mode_flag", pa.bool_()),
    _sv(),
])

# --- §9.6 order_tape ---------------------------------------------------------
ORDER_TAPE = pa.schema([
    _ts(),
    pa.field("action", pa.string()),             # ENTRY | EXIT | REJECT
    pa.field("instrument", pa.string()),         # always NIFTY
    pa.field("strike", pa.int64()),
    pa.field("right", pa.string()),              # CE | PE
    pa.field("qty", pa.int64()),
    pa.field("intended_price", pa.float64()),
    pa.field("fill_price", pa.float64()),
    pa.field("slippage", pa.float64()),
    pa.field("order_id", pa.string()),
    pa.field("live_orders_flag", pa.bool_()),
    pa.field("exit_reason", pa.string()),
    pa.field("pnl_realized", pa.float64()),
    _sv(),
])

# Map tape name -> schema.  event_tape is jsonl (§9.7), handled separately.
TAPE_SCHEMAS = {
    "tick_tape": TICK_TAPE,
    "level_tape": LEVEL_TAPE,
    "basis_tape": BASIS_TAPE,
    "decision_tape": DECISION_TAPE,
    "signal_tape": SIGNAL_TAPE,
    "order_tape": ORDER_TAPE,
}

PARQUET_TAPES = tuple(TAPE_SCHEMAS.keys())
JSONL_TAPES = ("event_tape", "round_tape", "client_tape")   # 2.0.1: round + dashboard beacons

# event_tape (§9.7) row contract — jsonl, free-form payload but these keys are fixed.
EVENT_TAPE_KEYS = ("ts", "event_type", "schema_version", "payload")
EVENT_TYPES = (
    "STARTUP_CONFIG", "EXPIRY_RESOLVED", "HOLIDAY_DECISION", "FEED_GAP",
    "SESSION_ERROR", "ANOMALY", "REVERSAL_WATCH", "THRESHOLDS_PROVISIONAL", "HOLIDAYS_UNVERIFIED",
    "APPROACH_ALERT",                      # the advance "scream" (why/where an entry was anticipated)
    "ENTRY_REJECTED",                      # an anticipated entry was rejected (premium floor / capital / quote)
    "EXIT_REJECTED",                       # a LIVE exit order failed at the broker — position kept OPEN, retried
    "SHUTDOWN",
    # ---- 2.0 ----
    "RESTORED",
    "SOUND",                               # 2.0.1: every engine sound event (seq, kind, text)
    "CLIENT",                              # 2.0.1: dashboard beacon (what the browser painted / played)                            # 2.0.1: same-day restart rebuilt capital/position/cooldowns
    "REGIME",                              # session regime + band method (startup)
    "FEED_HEALTH",                         # NIFTY feed-sanity recovery (faults are FEED_GAP stage=feed_sanity)
    "TRADE",                               # 2.2: one row per closed round trip (entry context, peak/low, exit)
)


def required_columns(tape_name: str) -> list[str]:
    """Column names the tape MUST contain (used by verify_vajra.py §11.2)."""
    return [f.name for f in TAPE_SCHEMAS[tape_name]]


def expected_arrow_types(tape_name: str) -> dict[str, pa.DataType]:
    return {f.name: f.type for f in TAPE_SCHEMAS[tape_name]}


__all__ = [
    "SCHEMA_VERSION", "Reason", "TAPE_SCHEMAS", "PARQUET_TAPES", "JSONL_TAPES",
    "EVENT_TAPE_KEYS", "EVENT_TYPES", "required_columns", "expected_arrow_types",
    "BASIS_MODES", "RIGHTS", "RECALC_TRIGGERS", "DEC_STATES", "DECISIONS",
    "ORDER_ACTIONS", "ROUTING_MODES",
    "TICK_TAPE", "LEVEL_TAPE", "BASIS_TAPE", "DECISION_TAPE", "SIGNAL_TAPE", "ORDER_TAPE",
]
