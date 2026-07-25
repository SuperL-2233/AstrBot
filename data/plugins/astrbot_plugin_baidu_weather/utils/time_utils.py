from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def today_local(tz_name: str) -> date:
    """Return the current calendar date in the configured timezone."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(tz).date()


def parse_date(raw: str) -> date | None:
    """Parse a strict YYYY-MM-DD date, returning None on invalid input."""
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def parse_hhmm(raw: str) -> str | None:
    """Normalize an HH:MM time string, returning None on invalid input."""
    try:
        parsed = datetime.strptime(raw.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return None
    return parsed.strftime("%H:%M")


def date_range(start: date, days: int) -> list[date]:
    """Return the consecutive dates starting at start (inclusive)."""
    return [start + timedelta(days=offset) for offset in range(days)]
