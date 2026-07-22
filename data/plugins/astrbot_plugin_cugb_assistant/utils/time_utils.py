from __future__ import annotations

from datetime import date, datetime, time, timedelta

DATETIME_FORMAT = "%Y-%m-%d %H:%M"
TIME_FORMAT = "%H:%M"


def now_local() -> datetime:
    return datetime.now()


def today_local() -> date:
    return now_local().date()


def tomorrow_local() -> date:
    return today_local() + timedelta(days=1)


def parse_datetime(value: str) -> datetime:
    return datetime.strptime(value, DATETIME_FORMAT)


def parse_time_str(value: str) -> time:
    return datetime.strptime(value, TIME_FORMAT).time()


def combine_date_time(target_date: date, value: str) -> datetime:
    return datetime.combine(target_date, parse_time_str(value))


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime(DATETIME_FORMAT)


def format_time(value: datetime | time | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.time()
    return value.strftime(TIME_FORMAT)
