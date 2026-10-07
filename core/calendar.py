"""Slovenian working days; no locale, external service, or observed-day shifts.

Recurring work-free days from https://www.gov.si/teme/drzavni-prazniki-in-dela-prosti-dnevi/
"""

from datetime import date, timedelta

_FIXED_NONWORKING_DAYS = frozenset({
    (1, 1), (1, 2), (2, 8), (4, 27), (5, 1), (5, 2), (6, 25),
    (8, 15), (10, 31), (11, 1), (12, 25), (12, 26),
})


def _easter_sunday(year):
    """Gregorian Easter, using the Meeus/Jones/Butcher computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, remainder = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, remainder + 1)


def is_slovenian_working_day(day: date) -> bool:
    # Easter Sunday and Pentecost Sunday are already excluded as weekends.
    return (
        day.weekday() < 5
        and (day.month, day.day) not in _FIXED_NONWORKING_DAYS
        and day != _easter_sunday(day.year) + timedelta(days=1)
    )


def next_slovenian_working_day(day: date) -> date:
    """Return day itself if working, otherwise advance to the first working day."""
    while not is_slovenian_working_day(day):
        day += timedelta(days=1)
    return day
