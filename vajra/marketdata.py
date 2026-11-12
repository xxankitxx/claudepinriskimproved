"""Lightweight market-data value objects shared by feed + engine.

Kept dependency-free (no pyarrow / breeze imports) so every engine module can
import it without pulling heavy deps — important for isolated unit testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrikeQuote:
    strike: int
    right: str                 # "CE" | "PE"
    ltp: float
    vwap: float
    vwap_source: str           # "feed" | "computed"
    volume: int
    oi: int
    bid: float
    ask: float


@dataclass
class MarketSnapshot:
    """One instrument's full state at one instant (already VIX-corrected)."""
    ts_ms: int
    instrument: str
    spot: float
    vix: float
    future_traded: float | None                       # near-month future LTP, if available
    strikes: dict[tuple[int, str], StrikeQuote] = field(default_factory=dict)

    # ---- convenience accessors ---------------------------------------------
    def quote(self, strike: int, right: str) -> StrikeQuote | None:
        return self.strikes.get((int(strike), right))

    def ce(self, strike: int) -> StrikeQuote | None:
        return self.quote(strike, "CE")

    def pe(self, strike: int) -> StrikeQuote | None:
        return self.quote(strike, "PE")

    def strikes_for(self, right: str) -> list[StrikeQuote]:
        return sorted(
            (q for (s, r), q in self.strikes.items() if r == right),
            key=lambda q: q.strike,
        )

    def all_strikes_sorted(self) -> list[StrikeQuote]:
        # 2.3: the dict keys ARE (strike, right) and unique, so sorting the items needs no key function
        return [q for _k, q in sorted(self.strikes.items())]

    def inferred_step(self) -> float | None:
        """Infer strike spacing from the chain (spec §5.1 'resolve per-instrument step')."""
        ks = sorted({q.strike for q in self.strikes.values()})
        if len(ks) < 2:
            return None
        diffs = sorted({ks[i + 1] - ks[i] for i in range(len(ks) - 1)})
        return float(diffs[0]) if diffs else None
