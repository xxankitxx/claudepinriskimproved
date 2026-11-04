"""VAJRA 2.0 — session regime (EXPIRY vs NON_EXPIRY) computed once per session.

The regime decides ONLY the NIFTY band method (expiry: the locked 1.x formula; non-expiry: mtm)
and the dashboard label. Sizing is all-in, with no trade cap and no loss stop, in every regime
(operator-locked) -- there are deliberately no sizing/limit fields here.

  dte_trading = number of trading days from the session to the active weekly expiry
                (0 on expiry day; holidays/weekends excluded).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

EXPIRY = "EXPIRY"
NON_EXPIRY = "NON_EXPIRY"


@dataclass(frozen=True)
class Regime:
    name: str                 # EXPIRY | NON_EXPIRY
    dte_trading: int          # trading days to the active weekly expiry (0 on expiry day)
    is_expiry_day: bool
    band_method: str          # legacy | mtm  (the method used for the NIFTY band this session)

    def as_dict(self) -> dict:
        return {"name": self.name, "dte_trading": self.dte_trading,
                "is_expiry_day": self.is_expiry_day, "band_method": self.band_method}


def trading_days_between(cal, start: date, end: date) -> int:
    """Trading days in (start, end] — 0 when start == end."""
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if cal.is_trading_day(d):
            n += 1
    return n


def resolve_regime(cfg, expiry, cal=None) -> Regime:
    """Build the session regime from config + the resolved expiry."""
    if cal is not None:
        dte = trading_days_between(cal, expiry.session_date, expiry.expiry_date)
    else:
        dte = 0 if expiry.is_expiry_day else max(1, (expiry.expiry_date - expiry.session_date).days)
    if expiry.is_expiry_day:
        return Regime(EXPIRY, 0, True, "legacy")
    ne = cfg.get("nonexpiry", default={}) or {}
    return Regime(NON_EXPIRY, int(dte), False, band_method=str(ne.get("band_method", "mtm")))


__all__ = ["Regime", "EXPIRY", "NON_EXPIRY", "resolve_regime", "trading_days_between"]
