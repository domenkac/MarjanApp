from datetime import date

import pytest

from core.calendar import is_slovenian_working_day, next_slovenian_working_day


@pytest.mark.parametrize("month,day", [
    (1, 1), (1, 2), (2, 8), (4, 27), (5, 1), (5, 2), (6, 25),
    (8, 15), (10, 31), (11, 1), (12, 25), (12, 26),
])
def test_fixed_nonworking_holidays(month, day):
    assert not is_slovenian_working_day(date(2026, month, day))


@pytest.mark.parametrize("holiday", [date(2026, 4, 6), date(2027, 3, 29), date(2024, 4, 1)])
def test_easter_monday(holiday):
    assert not is_slovenian_working_day(holiday)


@pytest.mark.parametrize("day", [date(2026, 6, 8), date(2026, 9, 15), date(2026, 11, 10)])
def test_public_holidays_that_are_working_days(day):
    assert is_slovenian_working_day(day)


def test_consecutive_days_and_year_rollover():
    assert next_slovenian_working_day(date(2026, 1, 1)) == date(2026, 1, 5)
    assert next_slovenian_working_day(date(2025, 12, 31)) == date(2025, 12, 31)
    assert next_slovenian_working_day(date(2026, 12, 31)) == date(2026, 12, 31)
