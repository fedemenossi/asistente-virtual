from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.timezone import get_ba_tz
from app.services.holiday_service import HolidayService


WEEKDAY_KEYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

WEEKDAY_LABELS = {
    "monday": "Lunes",
    "tuesday": "Martes",
    "wednesday": "Miercoles",
    "thursday": "Jueves",
    "friday": "Viernes",
    "saturday": "Sabado",
    "sunday": "Domingo",
}

DEFAULT_AVAILABLE_TAG = "[TURNO DISPONIBLE]"
DEFAULT_RESERVED_TAG_TEMPLATE = "TURNO [{patient_full_name}]"
DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"


@dataclass(frozen=True)
class CalculatedSlot:
    start_at: datetime
    end_at: datetime
    weekday: str


def default_google_calendar_config() -> dict[str, Any]:
    return {
        "calendar_id": "",
        "available_tag": DEFAULT_AVAILABLE_TAG,
        "reserved_tag_template": DEFAULT_RESERVED_TAG_TEMPLATE,
        "timezone": DEFAULT_TIMEZONE,
        "require_google_meet": True,
        "schedule_ranges": [],
        "schedule": {
            key: {
                "enabled": False,
                "start": "09:00",
                "end": "17:00",
                "slot_minutes": 30,
                "buffer_minutes": 0,
            }
            for key in WEEKDAY_KEYS
        },
    }


def normalize_provider(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if normalized == "google_calendar":
        return "google"
    return normalized or None


def get_google_calendar_config(consultorio) -> dict[str, Any]:
    configured = ((getattr(consultorio, "configuracion_externa", None) or {}).get("google_calendar") or {})
    payload = {
        key: value
        for key, value in configured.items()
        if key in default_google_calendar_config() and value is not None
    }
    if "schedule_ranges" not in configured:
        legacy_eligibility_ranges = (
            ((getattr(consultorio, "configuracion_externa", None) or {}).get("eligibility") or {}).get(
                "weekly_ranges"
            )
            or []
        )
        if legacy_eligibility_ranges:
            legacy_schedule = configured.get("schedule") if isinstance(configured.get("schedule"), dict) else {}
            payload["schedule_ranges"] = [
                {
                    "day": item.get("day"),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "slot_minutes": (legacy_schedule.get(item.get("day")) or {}).get("slot_minutes", 30),
                    "buffer_minutes": (legacy_schedule.get(item.get("day")) or {}).get("buffer_minutes", 0),
                }
                for item in legacy_eligibility_ranges
                if isinstance(item, dict)
            ]
    return validate_google_calendar_config(payload)


def validate_google_calendar_config(data: dict[str, Any]) -> dict[str, Any]:
    config = default_google_calendar_config()
    config.update(
        {
            key: value
            for key, value in data.items()
            if key in config and key not in {"schedule", "schedule_ranges"}
        }
    )
    config["calendar_id"] = str(config.get("calendar_id") or "").strip()
    config["available_tag"] = str(config.get("available_tag") or DEFAULT_AVAILABLE_TAG).strip() or DEFAULT_AVAILABLE_TAG
    config["reserved_tag_template"] = (
        str(config.get("reserved_tag_template") or DEFAULT_RESERVED_TAG_TEMPLATE).strip()
        or DEFAULT_RESERVED_TAG_TEMPLATE
    )
    config["timezone"] = str(config.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    config["require_google_meet"] = _as_bool(config.get("require_google_meet"))
    _resolve_timezone(config["timezone"])

    if "schedule_ranges" in data:
        raw_ranges = data.get("schedule_ranges") if isinstance(data.get("schedule_ranges"), list) else []
    else:
        raw_ranges = _ranges_from_legacy_schedule(data.get("schedule"))
    schedule_ranges = [_validate_schedule_range(item) for item in raw_ranges if isinstance(item, dict)]
    schedule_ranges.sort(key=lambda item: (WEEKDAY_KEYS.index(item["day"]), item["start"]))
    _validate_non_overlapping_ranges(schedule_ranges)
    config["schedule_ranges"] = schedule_ranges
    config["schedule"] = _legacy_schedule_from_ranges(schedule_ranges)
    return config


def build_google_calendar_config_from_form(form) -> dict[str, Any]:
    getlist = getattr(form, "getlist", lambda key: [form[key]] if key in form else [])
    if form.get("gcal_schedule_mode") == "ranges":
        days = getlist("gcal_schedule_day")
        starts = getlist("gcal_schedule_start")
        ends = getlist("gcal_schedule_end")
        durations = getlist("gcal_schedule_slot_minutes")
        intervals = getlist("gcal_schedule_buffer_minutes")
        schedule_ranges = []
        for index in range(max(len(days), len(starts), len(ends), len(durations), len(intervals), 0)):
            schedule_ranges.append(
                {
                    "day": days[index] if index < len(days) else "",
                    "start": starts[index] if index < len(starts) else "",
                    "end": ends[index] if index < len(ends) else "",
                    "slot_minutes": durations[index] if index < len(durations) else "",
                    "buffer_minutes": intervals[index] if index < len(intervals) else "",
                }
            )
        schedule_payload = {"schedule_ranges": schedule_ranges}
    else:
        schedule_payload = {
            "schedule": {
                day_key: {
                    "enabled": form.get(f"gcal_{day_key}_enabled") == "1",
                    "start": form.get(f"gcal_{day_key}_start") or "09:00",
                    "end": form.get(f"gcal_{day_key}_end") or "17:00",
                    "slot_minutes": form.get(f"gcal_{day_key}_slot_minutes") or 30,
                    "buffer_minutes": form.get(f"gcal_{day_key}_buffer_minutes") or 0,
                }
                for day_key in WEEKDAY_KEYS
            }
        }
    return validate_google_calendar_config(
        {
            "calendar_id": form.get("gcal_calendar_id") or "",
            "available_tag": form.get("gcal_available_tag") or DEFAULT_AVAILABLE_TAG,
            "reserved_tag_template": form.get("gcal_reserved_tag_template") or DEFAULT_RESERVED_TAG_TEMPLATE,
            "timezone": form.get("gcal_timezone") or DEFAULT_TIMEZONE,
            "require_google_meet": form.get("gcal_require_google_meet") == "1",
            **schedule_payload,
        }
    )


def calculate_slots(
    config: dict[str, Any],
    start_date: date,
    end_date: date,
    *,
    exclude_argentina_holidays: bool = False,
    holiday_service: HolidayService | None = None,
) -> list[CalculatedSlot]:
    if end_date < start_date:
        raise ValueError("La fecha hasta debe ser mayor o igual a fecha desde.")
    config = validate_google_calendar_config(config)
    holiday_service = holiday_service or HolidayService()
    tz = _resolve_timezone(config["timezone"])
    slots: list[CalculatedSlot] = []
    day = start_date
    while day <= end_date:
        if exclude_argentina_holidays and holiday_service.is_argentina_holiday(day):
            day += timedelta(days=1)
            continue
        day_key = WEEKDAY_KEYS[day.weekday()]
        for day_config in (item for item in config["schedule_ranges"] if item["day"] == day_key):
            start_time = _parse_time(day_config["start"], "inicio")
            end_time = _parse_time(day_config["end"], "fin")
            slot_minutes = int(day_config["slot_minutes"])
            buffer_minutes = int(day_config["buffer_minutes"])
            cursor = datetime.combine(day, start_time, tzinfo=tz)
            day_end = datetime.combine(day, end_time, tzinfo=tz)
            while cursor + timedelta(minutes=slot_minutes) <= day_end:
                slot_end = cursor + timedelta(minutes=slot_minutes)
                slots.append(CalculatedSlot(start_at=cursor, end_at=slot_end, weekday=day_key))
                cursor = slot_end + timedelta(minutes=buffer_minutes)
        day += timedelta(days=1)
    return slots


def slot_is_in_schedule_ranges(consultorio, start_at: datetime, end_at: datetime) -> bool:
    config = get_google_calendar_config(consultorio)
    ranges = config["schedule_ranges"]
    if not ranges:
        return True
    timezone = _resolve_timezone(config["timezone"])
    local_start = start_at.astimezone(timezone) if start_at.tzinfo is not None else start_at
    local_end = end_at.astimezone(timezone) if end_at.tzinfo is not None else end_at
    day_key = WEEKDAY_KEYS[local_start.weekday()]
    slot_start = local_start.strftime("%H:%M")
    slot_end = local_end.strftime("%H:%M")
    return any(
        item["day"] == day_key and item["start"] <= slot_start and slot_end <= item["end"]
        for item in ranges
    )


def _ranges_from_legacy_schedule(value: Any) -> list[dict[str, Any]]:
    schedule = value if isinstance(value, dict) else {}
    ranges = []
    for day_key in WEEKDAY_KEYS:
        day = {**default_google_calendar_config()["schedule"][day_key], **(schedule.get(day_key) or {})}
        if _as_bool(day.get("enabled")):
            ranges.append({"day": day_key, **{key: day[key] for key in ("start", "end", "slot_minutes", "buffer_minutes")}})
    return ranges


def _validate_schedule_range(item: dict[str, Any]) -> dict[str, Any]:
    day_key = str(item.get("day") or "").strip()
    if day_key not in WEEKDAY_KEYS:
        raise ValueError("Cada franja debe tener un dia valido.")
    start = _parse_time(item.get("start"), f"{WEEKDAY_LABELS[day_key]} inicio")
    end = _parse_time(item.get("end"), f"{WEEKDAY_LABELS[day_key]} fin")
    if start >= end:
        raise ValueError(f"{WEEKDAY_LABELS[day_key]}: la hora de inicio debe ser menor a la hora de fin.")
    return {
        "day": day_key,
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "slot_minutes": _parse_int(item.get("slot_minutes"), f"{WEEKDAY_LABELS[day_key]} duracion", minimum=1),
        "buffer_minutes": _parse_int(item.get("buffer_minutes"), f"{WEEKDAY_LABELS[day_key]} intervalo", minimum=0),
    }


def _validate_non_overlapping_ranges(ranges: list[dict[str, Any]]) -> None:
    previous_by_day: dict[str, dict[str, Any]] = {}
    for item in ranges:
        previous = previous_by_day.get(item["day"])
        if previous and item["start"] < previous["end"]:
            raise ValueError(f"{WEEKDAY_LABELS[item['day']]}: las franjas de atencion no pueden superponerse.")
        previous_by_day[item["day"]] = item


def _legacy_schedule_from_ranges(ranges: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    schedule = default_google_calendar_config()["schedule"]
    for item in ranges:
        if schedule[item["day"]]["enabled"]:
            continue
        schedule[item["day"]] = {
            "enabled": True,
            "start": item["start"],
            "end": item["end"],
            "slot_minutes": item["slot_minutes"],
            "buffer_minutes": item["buffer_minutes"],
        }
    return schedule


def _parse_time(value: Any, label: str) -> time:
    try:
        return time.fromisoformat(str(value or ""))
    except ValueError:
        raise ValueError(f"{label}: formato de hora invalido.") from None


def _parse_int(value: Any, label: str, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: debe ser numerico.") from None
    if parsed < minimum:
        raise ValueError(f"{label}: debe ser mayor o igual a {minimum}.")
    return parsed


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "si"}
    return bool(value)


def _resolve_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == DEFAULT_TIMEZONE:
            return get_ba_tz()
        raise
