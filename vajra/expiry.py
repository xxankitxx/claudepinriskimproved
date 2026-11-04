"""Expiry-day mapping + holiday calendar (spec §4.3).

Rules encoded:
  * Weekly options & futures expire on the configured weekday (default Tuesday).
  * If that day is a holiday, expiry shifts to the previous trading day
    (Tuesday -> Monday).  Generalised: step back over weekends/holidays.
  * Monthly expiry = the last <weekday> of the month (shifted the same way).
    The session whose active weekly expiry IS the monthly expiry is the
    monthly-expiry session -> basis mode ACTUAL; otherwise IMPLIED (§5.1).

Deterministic, calendar-driven.  Tests inject their own holiday set (§11.4).
"""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAY_INDEX = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
}


@dataclass(frozen=True)
class ExpiryResolution:
    session_date: date
    expiry_date: date
    basis_mode: str            # "IMPLIED" | "ACTUAL"
    is_expiry_day: bool
    is_monthly: bool
    shifted_from_holiday: bool   # True if expiry moved off the nominal weekday
    nominal_weekday: str
    # §4.1: the near-month FUTURE contract expiry (the monthly), which exists in
    # EVERY week as a sanity cross-check — distinct from the weekly options expiry.
    # In the monthly-expiry week it coincides with expiry_date.
    near_month_expiry: date | None = None


class ExpiryCalendar:
    def __init__(
        self,
        holidays: set[date] | None = None,
        weekly_weekday: str = "TUE",
    ):
        self.holidays = set(holidays or set())
        self.weekly_weekday = weekly_weekday.upper()
        if self.weekly_weekday not in WEEKDAY_INDEX:
            raise ValueError(f"unknown weekly_weekday {weekly_weekday!r}")
        self.wd = WEEKDAY_INDEX[self.weekly_weekday]

    # ---- day classification ------------------------------------------------
    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    def is_holiday(self, d: date) -> bool:
        return d in self.holidays

    def is_trading_day(self, d: date) -> bool:
        return not self.is_weekend(d) and not self.is_holiday(d)

    def prev_trading_day(self, d: date) -> date:
        c = d - timedelta(days=1)
        # Bounded walk-back (a year is far more than enough) to avoid infinite loop
        for _ in range(366):
            if self.is_trading_day(c):
                return c
            c -= timedelta(days=1)
        raise RuntimeError("no trading day found within 366 days — holiday set is broken")

    # ---- weekly / monthly resolution ---------------------------------------
    def _nominal_weekday_on_or_after(self, d: date) -> date:
        delta = (self.wd - d.weekday()) % 7
        return d + timedelta(days=delta)

    def resolve_weekly(self, nominal_tuesday: date) -> tuple[date, bool]:
        """Resolve a nominal weekly-expiry weekday to an actual trading date.

        Returns (resolved_date, shifted_from_holiday).
        """
        if self.is_trading_day(nominal_tuesday):
            return nominal_tuesday, False
        return self.prev_trading_day(nominal_tuesday), True

    def active_weekly_expiry(self, session_date: date) -> tuple[date, bool]:
        """The expiry the session is trading toward: next resolved weekly >= session_date."""
        nominal = self._nominal_weekday_on_or_after(session_date)
        resolved, shifted = self.resolve_weekly(nominal)
        if resolved < session_date:
            # This week's expiry already passed (e.g. today is the holiday Tuesday
            # and expiry was the prior Monday) -> jump to next week's weekday.
            nominal = nominal + timedelta(days=7)
            resolved, shifted = self.resolve_weekly(nominal)
        return resolved, shifted

    def monthly_expiry(self, year: int, month: int) -> tuple[date, bool]:
        """Last <weekday> of the month, shifted off holidays.  (resolved, shifted)."""
        last_day = _cal.monthrange(year, month)[1]
        d = date(year, month, last_day)
        # step back to the nominal weekday
        back = (d.weekday() - self.wd) % 7
        nominal_last = d - timedelta(days=back)
        return self.resolve_weekly(nominal_last)

    def near_month_future_expiry(self, session_date: date) -> date:
        """Nearest monthly future expiry on/after the session (the tradeable future)."""
        y, m = session_date.year, session_date.month
        nm, _ = self.monthly_expiry(y, m)
        if nm < session_date:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            nm, _ = self.monthly_expiry(y, m)
        return nm

    def resolve(self, session_date: date) -> ExpiryResolution:
        active, shifted = self.active_weekly_expiry(session_date)
        monthly, _m_shift = self.monthly_expiry(active.year, active.month)
        is_monthly = active == monthly
        return ExpiryResolution(
            session_date=session_date,
            expiry_date=active,
            basis_mode="ACTUAL" if is_monthly else "IMPLIED",
            is_expiry_day=(active == session_date),
            is_monthly=is_monthly,
            shifted_from_holiday=shifted,
            nominal_weekday=self.weekly_weekday,
            near_month_expiry=self.near_month_future_expiry(session_date),
        )


def parse_holidays(items) -> set[date]:
    """Accept ISO strings or date objects from config -> set[date]."""
    out: set[date] = set()
    for it in items or []:
        if isinstance(it, date):
            out.add(it)
        else:
            out.add(date.fromisoformat(str(it)))
    return out


__all__ = ["ExpiryCalendar", "ExpiryResolution", "parse_holidays", "WEEKDAY_INDEX"]
