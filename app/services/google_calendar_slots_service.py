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
DEFAULT_RESERVED_TAG_TEMPLATE = "[TURNO {patient_full_name}]"
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
    base = default_google_calendar_config()
    for key, value in configured.items():
        if key == "schedule" and isinstance(value, dict):
            for day_key, day_value in value.items():
                if day_key in base["schedule"] and isinstance(day_value, dict):
                    base["schedule"][day_key].update(
                        {k: v for k, v in day_value.items() if k in base["schedule"][day_key]}
                    )
        elif key in base and value is not None:
            base[key] = value
    return validate_google_calendar_config(base)


def validate_google_calendar_config(data: dict[str, Any]) -> dict[str, Any]:
    config = default_google_calendar_config()
    config.update({key: value for key, value in data.items() if key in config and key != "schedule"})
    config["calendar_id"] = str(config.get("calendar_id") or "").strip()
    config["available_tag"] = str(config.get("available_tag") or DEFAULT_AVAILABLE_TAG).strip() or DEFAULT_AVAILABLE_TAG
    config["reserved_tag_template"] = (
        str(config.get("reserved_tag_template") or DEFAULT_RESERVED_TAG_TEMPLATE).strip()
        or DEFAULT_RESERVED_TAG_TEMPLATE
    )
    config["timezone"] = str(config.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    _resolve_timezone(config["timezone"])

    schedule_input = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    schedule: dict[str, dict[str, Any]] = {}
    for day_key in WEEKDAY_KEYS:
        day = {**default_google_calendar_config()["schedule"][day_key], **(schedule_input.get(day_key) or {})}
        start = _parse_time(day.get("start"), f"{WEEKDAY_LABELS[day_key]} inicio")
        end = _parse_time(day.get("end"), f"{WEEKDAY_LABELS[day_key]} fin")
        slot_minutes = _parse_int(day.get("slot_minutes"), f"{WEEKDAY_LABELS[day_key]} duracion", minimum=1)
        buffer_minutes = _parse_int(day.get("buffer_minutes"), f"{WEEKDAY_LABELS[day_key]} intervalo", minimum=0)
        enabled = _as_bool(day.get("enabled"))
        if enabled and start >= end:
            raise ValueError(f"{WEEKDAY_LABELS[day_key]}: la hora de inicio debe ser menor a la hora de fin.")
        schedule[day_key] = {
            "enabled": enabled,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "slot_minutes": slot_minutes,
            "buffer_minutes": buffer_minutes,
        }
    config["schedule"] = schedule
    return config


def build_google_calendar_config_from_form(form) -> dict[str, Any]:
    schedule = {}
    for day_key in WEEKDAY_KEYS:
        schedule[day_key] = {
            "enabled": form.get(f"gcal_{day_key}_enabled") == "1",
            "start": form.get(f"gcal_{day_key}_start") or "09:00",
            "end": form.get(f"gcal_{day_key}_end") or "17:00",
            "slot_minutes": form.get(f"gcal_{day_key}_slot_minutes") or 30,
            "buffer_minutes": form.get(f"gcal_{day_key}_buffer_minutes") or 0,
        }
    return validate_google_calendar_config(
        {
            "calendar_id": form.get("gcal_calendar_id") or "",
            "available_tag": form.get("gcal_available_tag") or DEFAULT_AVAILABLE_TAG,
            "reserved_tag_template": form.get("gcal_reserved_tag_template") or DEFAULT_RESERVED_TAG_TEMPLATE,
            "timezone": form.get("gcal_timezone") or DEFAULT_TIMEZONE,
            "schedule": schedule,
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
        day_config = config["schedule"][day_key]
        if day_config["enabled"]:
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
