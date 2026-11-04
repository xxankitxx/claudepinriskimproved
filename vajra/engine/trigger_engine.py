"""§5.3 Gamma-Trigger Level Engine — the panic band, from ITM VWAPs.

    PAD       = max(0.10, pad_floor)
    ROUND_TO  = level_round_step              # default 5
    ITM_DEPTH = itm_depth_strikes             # default 1

    K_CE       = ATM - ITM_DEPTH*step                       # ITM for a call (below spot)
    RAW_UPPER  = K_CE + roundto(VWAP(K_CE)*(1+PAD), ROUND_TO)
    UPPER_LEVEL= RAW_UPPER - BASIS                          # forward -> spot

    K_PE       = ATM + ITM_DEPTH*step                       # ITM for a put (above spot)
    RAW_LOWER  = K_PE - roundto(VWAP(K_PE)*(1+PAD), ROUND_TO)
    LOWER_LEVEL= RAW_LOWER - BASIS

Unified basis rule: LEVEL = RAW - BASIS for BOTH levels (premium>0 subtracts,
discount<0 adds).

Golden (premium 10, ATM 24100, step 50, VWAP_CE 75, VWAP_PE 95):
    K_CE=24050, RAW_UPPER=24050+round(82.5,5)=24135, UPPER=24135-10=24125
    K_PE=24150, RAW_LOWER=24150-round(104.5,5)=24045, LOWER=24045-10=24035

Why ITM VWAPs (keep in code, §5.3): deep-ITM price ~= intrinsic + thin extrinsic
=> a stable ~1:1 tracker of spot (high delta) => a clean breakeven line.  The thin
extrinsic bleeds via theta through the day, so the band compresses toward spot —
which is why §5.4 must recompute it.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, replace

from ..mathx import round_to
from ..marketdata import MarketSnapshot
from ..schema import Reason


@dataclass(frozen=True)
class TriggerBand:
    instrument: str
    atm_strike: int
    k_ce: int
    vwap_ce: float | None
    raw_upper: float | None
    upper_level: float | None
    k_pe: int
    vwap_pe: float | None
    raw_lower: float | None
    lower_level: float | None
    basis: float
    basis_mode: str
    pad: float
    itm_depth: int
    round_to_step: float
    ok: bool
    reason: str | None
    # ---- VAJRA 2.0 additions (defaults keep 1.x construction sites valid) ----
    band_method: str = "legacy"          # legacy | mtm
    legacy_upper: float | None = None    # what the 1.x formula gave (for the narrow-only audit)
    legacy_lower: float | None = None
    e_ce: float | None = None            # extrinsic of the ITM call (same-strike OTM put LTP)
    e_pe: float | None = None
    delta_ce: float | None = None
    delta_pe: float | None = None
    anchor_spot: float | None = None     # spot at the instant the band was computed
    narrowed: bool = False               # True if the 2.0 rules pulled a level closer than 1.x


def compute_band(
    snapshot: MarketSnapshot,
    atm_strike: int,
    basis: float,
    basis_mode: str,
    step: float,
    *,
    pad_floor: float = 0.10,
    level_round_step: float = 5,
    itm_depth_strikes: int = 1,
) -> TriggerBand:
    # 2.0: pad is tunable in both directions (1.x silently clamped it to >= 0.10, so the
    # documented `--set trigger.pad_floor=0.05` was a no-op). Default stays 0.10 -> golden unchanged.
    pad = float(pad_floor)
    if not math.isfinite(pad) or pad < 0.0 or pad > 1.0:     # a typo'd pad must not build absurd levels
        pad = 0.10
    depth = int(itm_depth_strikes)

    k_ce = int(atm_strike - depth * step)        # ITM call (strike below spot)
    k_pe = int(atm_strike + depth * step)        # ITM put  (strike above spot)

    ce = snapshot.ce(k_ce)
    pe = snapshot.pe(k_pe)

    def _okv(q):
        return q is not None and q.vwap is not None and math.isfinite(q.vwap) and q.vwap > 0
    if not (_okv(ce) and _okv(pe)):      # 2.0.1: a NaN / zero VWAP is "missing", never a NaN level (T02)
        return TriggerBand(
            instrument=snapshot.instrument, atm_strike=atm_strike,
            k_ce=k_ce, vwap_ce=(ce.vwap if ce else None), raw_upper=None, upper_level=None,
            k_pe=k_pe, vwap_pe=(pe.vwap if pe else None), raw_lower=None, lower_level=None,
            basis=basis, basis_mode=basis_mode, pad=pad, itm_depth=depth,
            round_to_step=level_round_step, ok=False, reason=Reason.MISSING_VWAP,
        )

    vwap_ce = ce.vwap
    vwap_pe = pe.vwap

    raw_upper = k_ce + round_to(vwap_ce * (1.0 + pad), level_round_step)
    upper_level = raw_upper - basis

    raw_lower = k_pe - round_to(vwap_pe * (1.0 + pad), level_round_step)
    lower_level = raw_lower - basis

    return TriggerBand(
        instrument=snapshot.instrument, atm_strike=atm_strike,
        k_ce=k_ce, vwap_ce=vwap_ce, raw_upper=raw_upper, upper_level=upper_level,
        k_pe=k_pe, vwap_pe=vwap_pe, raw_lower=raw_lower, lower_level=lower_level,
        basis=basis, basis_mode=basis_mode, pad=pad, itm_depth=depth,
        round_to_step=level_round_step, ok=True, reason=None,
    )


def _delta(snapshot, k, step, right, fallback):
    """Finite-difference |delta| of the option at strike k from its neighbours' LTPs."""
    a = snapshot.quote(int(k - step), right)
    b = snapshot.quote(int(k + step), right)
    if a is None or b is None or a.ltp <= 0 or b.ltp <= 0:
        return fallback, "fallback"
    d = abs(a.ltp - b.ltp) / (2.0 * step)
    return min(0.98, max(0.30, d)), "chain"


def compute_band_v2(
    snapshot: MarketSnapshot,
    atm_strike: int,
    basis: float,
    basis_mode: str,
    step: float,
    *,
    method: str = "legacy",
    pad_floor: float = 0.10,
    level_round_step: float = 5,
    itm_depth_strikes: int = 1,
    mtm_pad: float = 0.20,
    mtm_floor_pts: float = 25.0,
    mtm_delta_fallback: float = 0.65,
    max_half_width_pts: float = 0.0,
) -> TriggerBand:
    """VAJRA 2.0 band.

    method="legacy": the locked 1.x §5.3 formula (expiry day ALWAYS uses this).
    method="mtm"   : NON-EXPIRY mark-to-market pain level (team finding 01) — the spot at which
                     the 1-ITM option would trade `mtm_pad` above its session VWAP *now*:
          UPPER = S + max(((1+mtm_pad)*VWAP_CE - LTP_CE) / delta_CE, floor)
          LOWER = S - max(((1+mtm_pad)*VWAP_PE - LTP_PE) / delta_PE, floor)
        (It does NOT reduce exactly to the legacy formula: legacy is strike-anchored and subtracts
        basis; this is spot-anchored. Expiry day therefore never uses it.)
    Narrow-only rule (operator: "narrow the levels if they are too far"): a 2.0 level is never
    FURTHER from spot than the 1.x level would have been. Optional hard cap max_half_width_pts.
    """
    base = compute_band(snapshot, atm_strike, basis, basis_mode, step, pad_floor=pad_floor,
                        level_round_step=level_round_step, itm_depth_strikes=itm_depth_strikes)
    if not base.ok:
        return base
    S = snapshot.spot
    ce, pe = snapshot.ce(base.k_ce), snapshot.pe(base.k_pe)
    e_ce_q, e_pe_q = snapshot.pe(base.k_ce), snapshot.ce(base.k_pe)
    e_ce = e_ce_q.ltp if e_ce_q else None
    e_pe = e_pe_q.ltp if e_pe_q else None
    if method != "mtm" and max_half_width_pts <= 0:
        return replace(base, band_method="legacy", legacy_upper=base.upper_level,
                       legacy_lower=base.lower_level, e_ce=e_ce, e_pe=e_pe, anchor_spot=S)

    up_leg = base.upper_level - S                      # legacy distances from the anchor spot
    dn_leg = S - base.lower_level
    up, dn = up_leg, dn_leg
    d_ce = d_pe = None
    if method == "mtm":
        d_ce, _ = _delta(snapshot, base.k_ce, step, "CE", mtm_delta_fallback)
        d_pe, _ = _delta(snapshot, base.k_pe, step, "PE", mtm_delta_fallback)
        up_mtm = max(((1.0 + mtm_pad) * ce.vwap - ce.ltp) / d_ce, mtm_floor_pts)
        dn_mtm = max(((1.0 + mtm_pad) * pe.vwap - pe.ltp) / d_pe, mtm_floor_pts)
        up, dn = min(up, up_mtm), min(dn, dn_mtm)      # narrow only — never widen vs 1.x
    if max_half_width_pts > 0:
        up, dn = min(up, max_half_width_pts), min(dn, max_half_width_pts)
    upper = round_to(S + up, level_round_step)
    lower = round_to(S - dn, level_round_step)
    if method == "mtm" and level_round_step > 0:      # rounding must not break the 25-pt floor (T24)
        rs = float(level_round_step)
        upper = max(upper, math.ceil((S + mtm_floor_pts) / rs) * rs)
        lower = min(lower, math.floor((S - mtm_floor_pts) / rs) * rs)
    # rounding must never push a level BEYOND the legacy one (narrow-only, teardown T02: up to 2.45 pts)
    upper = min(upper, base.upper_level)
    lower = max(lower, base.lower_level)
    import math as _m
    if not (_m.isfinite(upper) and _m.isfinite(lower)) or upper <= lower:
        return replace(base, band_method="legacy", legacy_upper=base.upper_level,
                       legacy_lower=base.lower_level, e_ce=e_ce, e_pe=e_pe, anchor_spot=S)
    narrowed = (upper < base.upper_level - 1e-9) or (lower > base.lower_level + 1e-9)
    return replace(base, upper_level=upper, lower_level=lower, band_method=method,
                   legacy_upper=base.upper_level, legacy_lower=base.lower_level,
                   e_ce=e_ce, e_pe=e_pe, delta_ce=d_ce, delta_pe=d_pe,
                   anchor_spot=S, narrowed=narrowed)


__all__ = ["TriggerBand", "compute_band", "compute_band_v2"]
