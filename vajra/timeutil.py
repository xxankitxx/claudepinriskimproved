"""Session-time helpers (IST).  Deterministic: pure functions of the tape ts.

Used by the basis monitor's time-weighting (§5.5 / §8.1) and by expiry-close
execution gating (§7 / §8).  IST = UTC+05:30; the NSE has no DST.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
SECONDS_PER_DAY = 86_400


def ist_datetime(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=IST)


_IST_OFFSET_S = 19_800


def ist_seconds_into_day(ts_ms: int) -> int:
    # 2.2: plain arithmetic (IST has a fixed offset); building a datetime here cost ~4% of a round
    return (int(ts_ms) // 1000 + _IST_OFFSET_S) % SECONDS_PER_DAY


_HHMM_CACHE: dict = {}


def parse_hhmm(s: str) -> int:
    v = _HHMM_CACHE.get(s)
    if v is None:
        h, m = str(s).split(":")
        v = int(h) * 3600 + int(m) * 60
        if len(_HHMM_CACHE) < 256:
            _HHMM_CACHE[s] = v
    return v


def basis_signal_weight(
    ts_ms: int,
    full_until: str = "12:30",
    near_zero_after: str = "14:00",
    floor: float = 0.05,
) -> float:
    """Decay basis-signal weight through the session (§5.5 'Time weighting').

    weight = 1.0           for t <= full_until
           = linear decay  between full_until and near_zero_after
           = floor (~0)     for t >= near_zero_after
    """
    sec = ist_seconds_into_day(ts_ms)
    full = parse_hhmm(full_until)
    near = parse_hhmm(near_zero_after)
    if near <= full:
        return 1.0 if sec <= full else floor
    if sec <= full:
        return 1.0
    if sec >= near:
        return floor
    frac = (sec - full) / (near - full)
    return 1.0 - frac * (1.0 - floor)


def hhmm_reached(ts_ms: int, hhmm: str) -> bool:
    return ist_seconds_into_day(ts_ms) >= parse_hhmm(hhmm)


__all__ = [
    "IST", "ist_datetime", "ist_seconds_into_day", "parse_hhmm",
    "basis_signal_weight", "hhmm_reached",
]
