"""
app_platform/timeutil.py
------------------------
The database stores naive UTC. People think in Asia/Kolkata. These
helpers are the only place the two are converted.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app_platform.config import get_settings

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(tz())


def local_now(now_utc: datetime) -> datetime:
    return to_local(now_utc)


def local_to_utc(d: date, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    local = datetime.combine(d, time(h, m), tzinfo=tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(value)
    return h, m


def to_minutes(value: str) -> int:
    h, m = parse_hhmm(value)
    return h * 60 + m


def from_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def label_12h(hhmm: str) -> str:
    h, m = parse_hhmm(hhmm)
    return f"{(h % 12) or 12:02d}:{m:02d} {'AM' if h < 12 else 'PM'}"


def fmt_local(dt_utc: datetime) -> str:
    d = to_local(dt_utc)
    return d.strftime("%d %b %Y, ") + label_12h(d.strftime("%H:%M"))


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def add_months_dt(dt: datetime, months: int) -> datetime:
    nd = add_months(dt.date(), months)
    return datetime.combine(nd, dt.time())


def booking_window(today_local: date, months: int) -> tuple[date, date]:
    """Rolling window: today through the same day `months` later (clamped to month end)."""
    return today_local, add_months(today_local, months)


def is_valid_hhmm(value: str) -> bool:
    try:
        parse_hhmm(value)
        return len(value) == 5
    except (ValueError, AttributeError):
        return False


def days_between(a: date, b: date) -> int:
    return (b - a).days


def hours_until(start_utc: datetime, now_utc: datetime) -> float:
    return (start_utc - now_utc) / timedelta(hours=1)
