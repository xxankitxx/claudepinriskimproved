"""§11.4 expiry-mapping tests (calendar-driven; injected holiday sets)."""

from datetime import date, timedelta
from vajra.expiry import ExpiryCalendar


def _first_tuesday(year, month):
    d = date(year, month, 1)
    while d.weekday() != 1:
        d += timedelta(days=1)
    return d


def test_weekly_holiday_shifts_to_monday():
    tue = _first_tuesday(2026, 9)
    cal = ExpiryCalendar(holidays={tue}, weekly_weekday="TUE")
    monday = tue - timedelta(days=1)
    res = cal.resolve(monday)
    assert res.expiry_date == monday
    assert res.shifted_from_holiday is True
    assert res.basis_mode == "IMPLIED"     # non-monthly week
    assert res.is_expiry_day is True


def test_monthly_week_is_actual():
    monthly_tue, _ = ExpiryCalendar(weekly_weekday="TUE").monthly_expiry(2026, 9)
    res = ExpiryCalendar(weekly_weekday="TUE").resolve(monthly_tue)
    assert res.is_monthly is True
    assert res.basis_mode == "ACTUAL"


def test_monthly_holiday_shifts_and_stays_actual():
    monthly_tue, _ = ExpiryCalendar(weekly_weekday="TUE").monthly_expiry(2026, 9)
    cal = ExpiryCalendar(holidays={monthly_tue}, weekly_weekday="TUE")
    resolved, shifted = cal.monthly_expiry(2026, 9)
    assert shifted is True
    assert resolved == monthly_tue - timedelta(days=1)
    res = cal.resolve(resolved)
    assert res.is_monthly is True
    assert res.basis_mode == "ACTUAL"


def test_wednesday_points_to_next_week():
    cal = ExpiryCalendar(weekly_weekday="TUE")
    tue = _first_tuesday(2026, 9)
    wed = tue + timedelta(days=1)
    res = cal.resolve(wed)
    assert res.expiry_date == tue + timedelta(days=7)
    assert res.is_expiry_day is False
