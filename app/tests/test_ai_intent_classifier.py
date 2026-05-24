from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.services.ai_intent_classifier import (
    AIIntent,
    AIIntentResult,
    AIIntentClassifier,
    classify_by_rules,
)


def test_rules_classify_presential_appointment() -> None:
    result = classify_by_rules("necesito turno en consultorio")

    assert result.intent == AIIntent.BOOK_PRESENTIAL_APPOINTMENT
    assert result.confidence >= 0.75
    assert result.extracted["appointment_type"] == "presential"
    assert result.source == "rules"


def test_rules_classify_virtual_appointment() -> None:
    result = classify_by_rules("quiero una consulta virtual")

    assert result.intent == AIIntent.BOOK_VIRTUAL_APPOINTMENT
    assert result.confidence >= 0.75
    assert result.extracted["appointment_type"] == "virtual"


def test_rules_classify_recipe_or_order() -> None:
    result = classify_by_rules("necesito una orden médica")

    assert result.intent == AIIntent.RECIPE_OR_ORDER
    assert result.confidence >= 0.75


def test_rules_classify_human_handoff() -> None:
    result = classify_by_rules("me atiende alguien")

    assert result.intent == AIIntent.HUMAN_HANDOFF
    assert result.confidence >= 0.75


def test_rules_classify_exit() -> None:
    result = classify_by_rules("reiniciar")

    assert result.intent == AIIntent.EXIT
    assert result.confidence >= 0.75


def test_rules_classify_ambiguous_as_unknown_low_confidence() -> None:
    result = classify_by_rules("quiero un turno")

    assert result.intent == AIIntent.UNKNOWN
    assert result.confidence < 0.75


def test_disabled_ai_classifier_does_not_call_openai(monkeypatch) -> None:
    async def _fail(*args, **kwargs):
        raise AssertionError("OpenAI no deberia ser llamado")

    monkeypatch.setattr(
        "app.services.ai_intent_classifier.AIIntentClassifier.classify_with_ai",
        _fail,
    )

    class _Tenant:
        ai_settings = {"enabled": False}

    result = asyncio.run(AIIntentClassifier().classify("quiero un turno", tenant=_Tenant()))

    assert result.intent == AIIntent.UNKNOWN
    assert result.confidence < 0.75
    get_settings.cache_clear()


def test_enabled_classifier_uses_tenant_settings(monkeypatch) -> None:
    captured = {}

    async def _fake_ai(self, message, *, ai_settings, api_key, fallback=None):
        captured["message"] = message
        captured["model"] = ai_settings["model"]
        captured["timeout"] = ai_settings["timeout_seconds"]
        captured["min_confidence"] = ai_settings["min_confidence"]
        captured["api_key"] = api_key
        return AIIntentResult(
            intent=AIIntent.BOOK_VIRTUAL_APPOINTMENT,
            confidence=0.9,
            extracted={},
            raw_response=None,
            source="ai",
            error=None,
        )

    monkeypatch.setattr(
        "app.services.ai_intent_classifier.AIIntentClassifier.classify_with_ai",
        _fake_ai,
    )

    class _Tenant:
        ai_settings = {
            "enabled": True,
            "api_key": "sk-tenant",
            "model": "gpt-4o",
            "min_confidence": 0.6,
            "timeout_seconds": 3,
            "allowed_intents": [AIIntent.BOOK_VIRTUAL_APPOINTMENT, AIIntent.UNKNOWN],
        }

    result = asyncio.run(AIIntentClassifier().classify("necesito una videollamada", tenant=_Tenant()))

    assert result.intent == AIIntent.BOOK_VIRTUAL_APPOINTMENT
    assert captured == {
        "message": "necesito una videollamada",
        "model": "gpt-4o",
        "timeout": 3,
        "min_confidence": 0.6,
        "api_key": "sk-tenant",
    }


def test_enabled_classifier_falls_back_when_ai_fails(monkeypatch) -> None:
    async def _fail_ai(self, message, *, ai_settings, api_key, fallback=None):
        fallback.error = "boom"
        return fallback

    monkeypatch.setattr(
        "app.services.ai_intent_classifier.AIIntentClassifier.classify_with_ai",
        _fail_ai,
    )

    class _Tenant:
        ai_settings = {
            "enabled": True,
            "api_key": "sk-tenant",
            "allowed_intents": [AIIntent.RECIPE_OR_ORDER, AIIntent.UNKNOWN],
        }

    result = asyncio.run(AIIntentClassifier().classify("quiero receta", tenant=_Tenant()))

    assert result.intent == AIIntent.RECIPE_OR_ORDER
    assert result.source == "rules"
    assert result.error == "boom"


def test_allowed_intents_blocks_rule_fallback_when_ai_enabled(monkeypatch) -> None:
    async def _fake_ai(self, message, *, ai_settings, api_key, fallback=None):
        return fallback

    monkeypatch.setattr(
        "app.services.ai_intent_classifier.AIIntentClassifier.classify_with_ai",
        _fake_ai,
    )

    class _Tenant:
        ai_settings = {
            "enabled": True,
            "api_key": "sk-tenant",
            "allowed_intents": [AIIntent.RECIPE_OR_ORDER, AIIntent.UNKNOWN],
        }

    result = asyncio.run(AIIntentClassifier().classify("necesito turno virtual", tenant=_Tenant()))

    assert result.intent == AIIntent.UNKNOWN
    assert result.confidence < 0.75


def test_rules_classify_virtual_free_text() -> None:
    result = classify_by_rules("necesito turno virtual")

    assert result.intent == AIIntent.BOOK_VIRTUAL_APPOINTMENT


def test_rules_classify_recipe_free_text() -> None:
    result = classify_by_rules("quiero receta")

    assert result.intent == AIIntent.RECIPE_OR_ORDER
