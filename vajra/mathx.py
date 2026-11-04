"""Deterministic math helpers shared across VAJRA modules.

Two things here are load-bearing for the completion gate (spec §11):

  * ``round_to`` uses Decimal ROUND_HALF_UP.  The §5.3 golden test requires
    round(82.5, 5) == 85 and round(104.5, 5) == 105.  Python's built-in
    round() uses banker's rounding (round-half-to-even) and would return 80
    and 105 respectively, which BREAKS the golden upper=24125 result.  Do not
    "simplify" this to round().

  * ``TimeEWMA`` decays on the *tape timestamp* delta, never wall-clock, so
    that replay.py reproduces byte-identical state across runs (§11.5
    determinism test).
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP


def round_to(value: float, step: float) -> float:
    """Round ``value`` to the nearest multiple of ``step`` using HALF-UP.

    round_to(82.5, 5)  -> 85.0   (NOT 80.0 — banker's rounding would give 80)
    round_to(104.5, 5) -> 105.0
    round_to(24110, 50) -> 24100.0
    """
    if step <= 0:
        raise ValueError(f"round_to step must be > 0, got {step!r}")
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    quotient = (d_value / d_step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(quotient * d_step)


def round_half_up(value: float, ndigits: int = 0) -> float:
    """Round a scalar to ``ndigits`` decimal places, HALF-UP."""
    q = Decimal("1") if ndigits <= 0 else Decimal("1").scaleb(-ndigits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def halflife_to_tau(halflife_seconds: float) -> float:
    """Convert a halflife (seconds) to the exponential time-constant tau."""
    if halflife_seconds <= 0:
        raise ValueError("halflife must be > 0")
    return halflife_seconds / math.log(2.0)


class TimeEWMA:
    """Stateful, irregular-interval EWMA decayed by elapsed *tape* time.

    alpha = 1 - exp(-dt / tau),  tau = halflife / ln 2

    The decay uses the timestamp deltas supplied to ``update``; given the same
    sequence of (ts, value) it is fully deterministic — the property §11.5
    relies on.
    """

    def __init__(self, halflife_seconds: float):
        self.tau = halflife_to_tau(halflife_seconds)
        self.value: float | None = None
        self._last_ts: float | None = None

    def update(self, ts_seconds: float, x: float) -> float:
        if self.value is None or self._last_ts is None:
            self.value = float(x)
            self._last_ts = float(ts_seconds)
            return self.value
        dt = float(ts_seconds) - self._last_ts
        if dt < 0:
            # Out-of-order tick: treat as zero elapsed (no negative decay).
            dt = 0.0
        alpha = 1.0 - math.exp(-dt / self.tau) if self.tau > 0 else 1.0
        self.value = alpha * float(x) + (1.0 - alpha) * self.value
        self._last_ts = float(ts_seconds)
        return self.value


class RollingStats:
    """Fixed-window rolling mean/std over the most recent ``window`` samples.

    Used by the basis monitor's ABRUPT test (§5.5) and continuation volume
    averaging (§5.7).  Deterministic: pure function of the inserted sequence.
    """

    def __init__(self, window: int):
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window = window
        self._buf: list[float] = []

    def push(self, x: float) -> None:
        self._buf.append(float(x))
        if len(self._buf) > self.window:
            self._buf.pop(0)

    def __len__(self) -> int:
        return len(self._buf)

    def mean(self) -> float:
        return sum(self._buf) / len(self._buf) if self._buf else 0.0

    def std(self) -> float:
        n = len(self._buf)
        if n < 2:
            return 0.0
        m = self.mean()
        var = sum((v - m) ** 2 for v in self._buf) / (n - 1)  # sample std
        return math.sqrt(var)
