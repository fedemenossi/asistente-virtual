from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.ai_intent_classifier import SUPPORTED_INTENTS


AI_SETTINGS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "provider": "openai",
    "api_key": "",
    "model": "gpt-4o-mini",
    "min_confidence": 0.75,
    "timeout_seconds": 8,
    "agent_name": "Asistente virtual",
    "system_prompt": "",
    "personality": "cordial, clara, profesional y breve",
    "allowed_intents": sorted(SUPPORTED_INTENTS),
    "handoff_on_low_confidence": True,
    "max_tokens": 400,
    "temperature": 0.0,
    "tools_enabled": False,
    "availability_lookup_enabled": False,
    "max_offered_slots": 5,
    "require_confirmation_before_booking": True,
}


def get_effective_ai_settings(tenant) -> dict[str, Any]:
    configured = getattr(tenant, "ai_settings", None) or {}
    effective = deepcopy(AI_SETTINGS_DEFAULTS)
    if isinstance(configured, dict):
        for key, value in configured.items():
            if key in effective and value is not None:
                effective[key] = value
    return validate_ai_settings(effective, allow_global_fallback=True)


def mask_api_key(api_key: str | None) -> str:
    value = (api_key or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}****{value[-4:]}" if value.startswith("sk-") else f"****{value[-4:]}"


def validate_ai_settings(
    data: dict[str, Any],
    *,
    existing_settings: dict[str, Any] | None = None,
    allow_global_fallback: bool = False,
) -> dict[str, Any]:
    existing_settings = existing_settings or {}
    cleaned = deepcopy(AI_SETTINGS_DEFAULTS)
    cleaned.update({key: value for key, value in data.items() if key in cleaned and value is not None})

    cleaned["enabled"] = _as_bool(cleaned.get("enabled"))
    cleaned["handoff_on_low_confidence"] = _as_bool(cleaned.get("handoff_on_low_confidence"))
    cleaned["tools_enabled"] = _as_bool(cleaned.get("tools_enabled"))
    cleaned["availability_lookup_enabled"] = _as_bool(cleaned.get("availability_lookup_enabled"))
    cleaned["require_confirmation_before_booking"] = _as_bool(
        cleaned.get("require_confirmation_before_booking", True)
    )

    provider = str(cleaned.get("provider") or "").strip().lower()
    if provider != "openai":
        raise ValueError("Proveedor IA no soportado.")
    cleaned["provider"] = provider

    cleaned["model"] = str(cleaned.get("model") or "").strip()
    if not cleaned["model"]:
        raise ValueError("El modelo IA es obligatorio.")

    cleaned["min_confidence"] = _float_range(
        cleaned.get("min_confidence"), "La confianza minima", 0.0, 1.0
    )
    cleaned["timeout_seconds"] = int(
        _float_range(cleaned.get("timeout_seconds"), "El timeout", 1, 30)
    )
    cleaned["temperature"] = _float_range(cleaned.get("temperature"), "La temperatura", 0.0, 1.0)
    cleaned["max_tokens"] = int(_float_range(cleaned.get("max_tokens"), "Max tokens", 50, 2000))
    cleaned["max_offered_slots"] = int(
        _float_range(cleaned.get("max_offered_slots"), "Maximo de turnos ofrecidos", 1, 10)
    )
    cleaned["agent_name"] = str(cleaned.get("agent_name") or AI_SETTINGS_DEFAULTS["agent_name"]).strip()
    cleaned["system_prompt"] = str(cleaned.get("system_prompt") or "").strip()
    cleaned["personality"] = str(cleaned.get("personality") or AI_SETTINGS_DEFAULTS["personality"]).strip()

    api_key = str(cleaned.get("api_key") or "").strip()
    if not api_key and existing_settings.get("api_key"):
        api_key = str(existing_settings.get("api_key") or "").strip()
    cleaned["api_key"] = api_key
    if cleaned["enabled"] and cleaned["provider"] == "openai" and not api_key and not allow_global_fallback:
        raise ValueError("Para habilitar IA con OpenAI debe cargar una API key o configurar fallback global.")

    raw_allowed = cleaned.get("allowed_intents")
    if not isinstance(raw_allowed, list):
        raise ValueError("Las intenciones permitidas deben ser una lista.")
    allowed = []
    for item in raw_allowed:
        value = str(item or "").strip()
        if value not in SUPPORTED_INTENTS:
            raise ValueError("Intencion IA no soportada.")
        if value not in allowed:
            allowed.append(value)
    cleaned["allowed_intents"] = allowed or list(AI_SETTINGS_DEFAULTS["allowed_intents"])
    return cleaned


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "si"}
    return bool(value)


def _float_range(value: Any, label: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} debe ser numerica.") from None
    if number < minimum or number > maximum:
        raise ValueError(f"{label} debe estar entre {minimum:g} y {maximum:g}.")
    return number
