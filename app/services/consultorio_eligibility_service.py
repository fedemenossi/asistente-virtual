from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.consultorio import Consultorio


COVERAGE_TYPES = ("particular", "obra_social", "prepaga")
WEEKDAYS = (
    ("monday", "Lunes"),
    ("tuesday", "Martes"),
    ("wednesday", "Miercoles"),
    ("thursday", "Jueves"),
    ("friday", "Viernes"),
    ("saturday", "Sabado"),
    ("sunday", "Domingo"),
)
WEEKDAY_INDEX = {key: index for index, (key, _label) in enumerate(WEEKDAYS)}


def default_eligibility_config() -> dict[str, Any]:
    return {
        "accepts_first_consultation": True,
        "coverage_types": list(COVERAGE_TYPES),
        "coverage_entities": [],
        "weekly_ranges": [],
    }


def get_eligibility_config(consultorio: Consultorio | None) -> dict[str, Any]:
    defaults = default_eligibility_config()
    raw = ((consultorio.configuracion_externa or {}).get("eligibility") or {}) if consultorio else {}
    return {
        "accepts_first_consultation": bool(
            raw.get("accepts_first_consultation", defaults["accepts_first_consultation"])
        ),
        "coverage_types": [
            value for value in raw.get("coverage_types", defaults["coverage_types"]) if value in COVERAGE_TYPES
        ],
        "coverage_entities": [str(value) for value in raw.get("coverage_entities", []) if str(value).strip()],
        "weekly_ranges": [
            item for item in raw.get("weekly_ranges", []) if isinstance(item, dict)
        ],
    }


def build_eligibility_config_from_form(form: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    if not any(str(key).startswith("eligibility_") for key in form.keys()):
        return default_eligibility_config(), {}
    getlist = getattr(form, "getlist", lambda key: [form[key]] if key in form else [])
    coverage_types = [value for value in getlist("eligibility_coverage_types") if value in COVERAGE_TYPES]
    entities = [
        item.strip()
        for item in str(form.get("eligibility_coverage_entities") or "").splitlines()
        if item.strip()
    ]
    days = getlist("eligibility_range_day")
    starts = getlist("eligibility_range_start")
    ends = getlist("eligibility_range_end")
    ranges: list[dict[str, str]] = []
    errors: dict[str, str] = {}
    if form.get("eligibility_ranges_enabled"):
        for index in range(max(len(days), len(starts), len(ends), 0)):
            day = str(days[index] if index < len(days) else "").strip()
            start = str(starts[index] if index < len(starts) else "").strip()
            end = str(ends[index] if index < len(ends) else "").strip()
            if not day and not start and not end:
                continue
            if day not in WEEKDAY_INDEX or not _valid_time(start) or not _valid_time(end) or start >= end:
                errors["eligibility_ranges"] = "Cada franja debe tener dia y un horario de inicio anterior al fin."
                continue
            ranges.append({"day": day, "start": start, "end": end})
        if not ranges and "eligibility_ranges" not in errors:
            errors["eligibility_ranges"] = "Agrega al menos una franja o desactiva la restriccion horaria."
    if not coverage_types:
        errors["eligibility_coverage_types"] = "Selecciona al menos un tipo de cobertura."
    return (
        {
            "accepts_first_consultation": bool(form.get("eligibility_accepts_first_consultation")),
            "coverage_types": coverage_types,
            "coverage_entities": entities,
            "weekly_ranges": ranges,
        },
        errors,
    )


def _valid_time(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return False
    return True


def is_consultorio_eligible(
    consultorio: Consultorio,
    *,
    first_time: bool,
    coverage_type: str,
    coverage_entity: str,
) -> bool:
    config = get_eligibility_config(consultorio)
    if first_time and not config["accepts_first_consultation"]:
        return False
    if coverage_type not in config["coverage_types"]:
        return False
    allowed_entities = {_normalize_text(value) for value in config["coverage_entities"]}
    return not allowed_entities or _normalize_text(coverage_entity) in allowed_entities


def has_specific_coverage_match(consultorio: Consultorio, coverage_entity: str) -> bool:
    allowed_entities = {
        _normalize_text(value) for value in get_eligibility_config(consultorio)["coverage_entities"]
    }
    return bool(allowed_entities) and _normalize_text(coverage_entity) in allowed_entities


def slot_is_in_weekly_ranges(
    consultorio: Consultorio,
    start_at: datetime,
    end_at: datetime,
) -> bool:
    ranges = get_eligibility_config(consultorio)["weekly_ranges"]
    if not ranges:
        return True
    timezone_name = str(
        ((consultorio.configuracion_externa or {}).get("google_calendar") or {}).get("timezone") or ""
    )
    local_start = start_at
    local_end = end_at
    if timezone_name and start_at.tzinfo is not None:
        try:
            timezone = ZoneInfo(timezone_name)
            local_start = start_at.astimezone(timezone)
            local_end = end_at.astimezone(timezone) if end_at.tzinfo is not None else end_at
        except ZoneInfoNotFoundError:
            pass
    slot_time = local_start.strftime("%H:%M")
    slot_end_time = local_end.strftime("%H:%M")
    for item in ranges:
        if WEEKDAY_INDEX.get(str(item.get("day") or "")) != local_start.weekday():
            continue
        if (
            str(item.get("start") or "") <= slot_time
            and slot_end_time <= str(item.get("end") or "")
        ):
            return True
    return False


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))
