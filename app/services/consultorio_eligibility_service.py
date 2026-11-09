from __future__ import annotations

from typing import Any, Mapping
import unicodedata

from app.models.consultorio import Consultorio


COVERAGE_TYPES = ("particular", "obra_social", "prepaga")
def default_eligibility_config() -> dict[str, Any]:
    return {
        "accepts_first_consultation": True,
        "coverage_types": list(COVERAGE_TYPES),
        "coverage_entities": [],
        "prepaga_entities": [],
    }


def get_eligibility_config(consultorio: Consultorio | None) -> dict[str, Any]:
    defaults = default_eligibility_config()
    raw = ((consultorio.configuracion_externa or {}).get("eligibility") or {}) if consultorio else {}
    coverage_types = [
        value for value in raw.get("coverage_types", defaults["coverage_types"]) if value in COVERAGE_TYPES
    ]
    coverage_entities = [
        str(value) for value in raw.get("coverage_entities", []) if str(value).strip()
    ]
    raw_prepaga_entities = raw.get("prepaga_entities")
    prepaga_entities = [
        str(value)
        for value in (
            raw_prepaga_entities
            if isinstance(raw_prepaga_entities, list)
            else (
                coverage_entities
                if "prepaga" in coverage_types and "obra_social" not in coverage_types
                else []
            )
        )
        if str(value).strip()
    ]
    return {
        "accepts_first_consultation": bool(
            raw.get("accepts_first_consultation", defaults["accepts_first_consultation"])
        ),
        "coverage_types": coverage_types,
        "coverage_entities": coverage_entities,
        "prepaga_entities": prepaga_entities,
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
    prepaga_entities = [
        item.strip()
        for item in str(form.get("eligibility_prepaga_entities") or "").splitlines()
        if item.strip()
    ]
    errors: dict[str, str] = {}
    if not coverage_types:
        errors["eligibility_coverage_types"] = "Selecciona al menos un tipo de cobertura."
    if "prepaga" in coverage_types and not prepaga_entities:
        errors["eligibility_prepaga_entities"] = (
            "Indica al menos una prepaga admitida para los turnos de este consultorio."
        )
    return (
        {
            "accepts_first_consultation": bool(form.get("eligibility_accepts_first_consultation")),
            "coverage_types": coverage_types,
            "coverage_entities": entities,
            "prepaga_entities": prepaga_entities,
        },
        errors,
    )


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
    entity_key = "prepaga_entities" if coverage_type == "prepaga" else "coverage_entities"
    allowed_entities = {_normalize_text(value) for value in config[entity_key]}
    return not allowed_entities or _normalize_text(coverage_entity) in allowed_entities


def has_specific_coverage_match(
    consultorio: Consultorio,
    coverage_type: str,
    coverage_entity: str,
) -> bool:
    config = get_eligibility_config(consultorio)
    entity_key = "prepaga_entities" if coverage_type == "prepaga" else "coverage_entities"
    allowed_entities = {
        _normalize_text(value) for value in config[entity_key]
    }
    return bool(allowed_entities) and _normalize_text(coverage_entity) in allowed_entities


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))
