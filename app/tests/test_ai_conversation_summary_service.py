from __future__ import annotations

from app.services.ai_conversation_summary_service import (
    format_confidence,
    format_extracted_fields,
    format_missing_fields,
    get_ai_summary_from_context,
    get_confidence_badge_level,
)


def _context() -> dict:
    return {
        "ai": {
            "last_intent": "book_virtual_appointment",
            "last_confidence": 0.88,
            "last_source": "ai",
            "raw_response": {"secret": "no mostrar"},
            "missing_fields": ["is_first_time"],
            "extracted": {
                "appointment_type": "virtual",
                "appointment_for": "other",
                "other_patient_name": "Juan Perez",
                "other_patient_dni": "40111222",
                "preferred_day": "martes",
                "preferred_time_range": "tarde",
                "urgency_level": "low",
                "needs_human": False,
                "dni": None,
            },
        }
    }


def test_empty_context_returns_hidden_summary() -> None:
    assert get_ai_summary_from_context({})["show"] is False


def test_context_with_intent_returns_summary() -> None:
    summary = get_ai_summary_from_context(_context())

    assert summary["show"] is True
    assert summary["intent_label"] == "Turno virtual"
    assert summary["confidence_label"] == "88%"
    assert summary["confidence_level"] == "high"
    assert summary["source"] == "ai"
    assert "raw_response" not in summary


def test_confidence_formatting_levels() -> None:
    assert format_confidence(0.88) == "88%"
    assert get_confidence_badge_level(0.88) == "high"
    assert get_confidence_badge_level(0.55) == "medium"
    assert get_confidence_badge_level(0.2) == "low"


def test_extracted_fields_omit_nulls_and_can_mask_dni() -> None:
    fields = format_extracted_fields(_context()["ai"]["extracted"], mask_sensitive=True)
    values = {item["label"]: item["value"] for item in fields}

    assert "DNI" in values
    assert values["DNI"] == "***222"
    assert "Preferencia" in values
    assert "martes" in values["Preferencia"]
    assert all(item["value"] not in {"", None} for item in fields)


def test_missing_fields_are_formatted() -> None:
    assert format_missing_fields(["is_first_time", "medical_reason"]) == ["primera vez", "detalle"]


def test_urgency_high_is_exposed() -> None:
    context = _context()
    context["ai"]["extracted"]["urgency_level"] = "high"
    context["ai"]["extracted"]["needs_human"] = True

    summary = get_ai_summary_from_context(context)

    assert summary["urgency_level"] == "high"
    assert summary["needs_human"] is True
