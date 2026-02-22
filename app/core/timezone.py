from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BA_TIMEZONE_NAME = "America/Argentina/Buenos_Aires"


def get_ba_tz() -> tzinfo:
    try:
        return ZoneInfo(BA_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-3))


def now_ba() -> datetime:
    return datetime.now(get_ba_tz())


def to_ba(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(get_ba_tz())


def format_ba(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    converted = to_ba(value)
    if converted is None:
        return ""
    return converted.strftime(fmt)
