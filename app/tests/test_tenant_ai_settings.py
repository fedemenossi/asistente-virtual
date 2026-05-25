from __future__ import annotations

import asyncio
import re

import pytest

from app.core.security import hash_password
from app.models.user import UserRole
from app.services.ai_intent_classifier import AIIntent
from app.services.tenant_ai_settings_service import (
    get_effective_ai_settings,
    mask_api_key,
    validate_ai_settings,
)
from app.tests.conftest import create_tenant, create_user, get_tenant, login


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token no encontrado"
    return match.group(1)


def _base_tenant_form(csrf: str, **extra) -> dict:
    data = {
        "csrf_token": csrf,
        "nombre": "Tenant IA",
        "whatsapp_number": "whatsapp:+5491100011111",
        "activo": "1",
        "ai_provider": "openai",
        "ai_model": "gpt-4o-mini",
        "ai_min_confidence": "0.75",
        "ai_timeout_seconds": "8",
        "ai_agent_name": "Asistente virtual",
        "ai_personality": "cordial, clara, profesional y breve",
        "ai_system_prompt": "",
        "ai_temperature": "0",
        "ai_max_tokens": "400",
        "ai_handoff_on_low_confidence": "1",
        "ai_max_offered_slots": "5",
        "ai_require_confirmation_before_booking": "1",
        "ai_allowed_intents": sorted(
            [
                AIIntent.BOOK_PRESENTIAL_APPOINTMENT,
                AIIntent.BOOK_VIRTUAL_APPOINTMENT,
                AIIntent.RECIPE_OR_ORDER,
                AIIntent.OTHER_MEDICAL_QUERY,
                AIIntent.HUMAN_HANDOFF,
                AIIntent.CANCEL_APPOINTMENT,
                AIIntent.RESCHEDULE_APPOINTMENT,
                AIIntent.GREETING,
                AIIntent.EXIT,
                AIIntent.UNKNOWN,
            ]
        ),
    }
    data.update(extra)
    return data


def test_tenant_can_store_ai_settings(db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Store IA",
            "whatsapp:+910",
            ai_settings={"enabled": True, "api_key": "sk-testabcd", "model": "gpt-4o-mini"},
        )
    )

    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    assert tenant.ai_settings["enabled"] is True
    assert tenant.ai_settings["api_key"] == "sk-testabcd"


def test_effective_ai_settings_defaults_when_empty(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Defaults IA", "whatsapp:+911"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    settings = get_effective_ai_settings(tenant)

    assert settings["enabled"] is False
    assert settings["provider"] == "openai"
    assert settings["model"] == "gpt-4o-mini"
    assert settings["min_confidence"] == 0.75
    assert settings["tools_enabled"] is False
    assert settings["availability_lookup_enabled"] is False
    assert settings["max_offered_slots"] == 5
    assert settings["require_confirmation_before_booking"] is True


def test_validate_ai_settings_rejects_invalid_confidence():
    with pytest.raises(ValueError):
        validate_ai_settings({"min_confidence": 1.5})


def test_validate_ai_settings_rejects_invalid_timeout():
    with pytest.raises(ValueError):
        validate_ai_settings({"timeout_seconds": 0})


def test_validate_ai_settings_rejects_invalid_max_offered_slots():
    with pytest.raises(ValueError):
        validate_ai_settings({"max_offered_slots": 11})


def test_validate_ai_settings_preserves_existing_api_key_when_blank():
    settings = validate_ai_settings(
        {"enabled": True, "api_key": "", "allowed_intents": [AIIntent.UNKNOWN]},
        existing_settings={"api_key": "sk-existingabcd"},
    )

    assert settings["api_key"] == "sk-existingabcd"


def test_mask_api_key():
    assert mask_api_key("sk-1234567890abcd") == "sk-****abcd"


def test_super_admin_sees_ai_fields_in_tenant_create(client, db_session):
    asyncio.run(
        create_user(
            db_session,
            "admin-ai@example.com",
            hash_password("change_me"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-ai@example.com", "change_me")

    response = client.get("/admin/tenants/new")

    assert response.status_code == 200
    assert "Agente de IA" in response.text
    assert "ai_api_key" in response.text
    assert "ai_tools_enabled" in response.text
    assert "ai_availability_lookup_enabled" in response.text


def test_super_admin_saves_tenant_ai_settings_and_masks_key(client, db_session):
    asyncio.run(
        create_user(
            db_session,
            "admin-ai-save@example.com",
            hash_password("change_me"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-ai-save@example.com", "change_me")
    page = client.get("/admin/tenants/new")
    csrf = _extract_csrf(page.text)

    response = client.post(
        "/admin/tenants/new",
        data=_base_tenant_form(
            csrf,
            ai_enabled="1",
            ai_api_key="sk-test1234abcd",
            ai_min_confidence="0.8",
            ai_tools_enabled="1",
            ai_availability_lookup_enabled="1",
            ai_max_offered_slots="4",
        ),
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)

    tenant = asyncio.run(get_tenant(db_session, 1))
    assert tenant.ai_settings["enabled"] is True
    assert tenant.ai_settings["api_key"] == "sk-test1234abcd"
    assert tenant.ai_settings["min_confidence"] == 0.8
    assert tenant.ai_settings["tools_enabled"] is True
    assert tenant.ai_settings["availability_lookup_enabled"] is True
    assert tenant.ai_settings["max_offered_slots"] == 4

    edit = client.get(f"/admin/tenants/{tenant.id}/edit")
    assert "sk-****abcd" in edit.text
    assert "sk-test1234abcd" not in edit.text


def test_edit_tenant_ai_settings_keeps_api_key_when_blank(client, db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Keep Key",
            "whatsapp:+912",
            ai_settings={"enabled": True, "api_key": "sk-keepabcd", "model": "gpt-4o-mini"},
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "admin-ai-edit@example.com",
            hash_password("change_me"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-ai-edit@example.com", "change_me")
    edit = client.get(f"/admin/tenants/{tenant_id}/edit")
    csrf = _extract_csrf(edit.text)

    response = client.post(
        f"/admin/tenants/{tenant_id}/edit",
        data=_base_tenant_form(
            csrf,
            nombre="Tenant Keep Key",
            whatsapp_number="whatsapp:+912",
            ai_enabled="1",
            ai_api_key="",
            ai_model="gpt-4o",
        ),
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    assert tenant.ai_settings["api_key"] == "sk-keepabcd"
    assert tenant.ai_settings["model"] == "gpt-4o"


def test_tenant_admin_cannot_edit_admin_ai_settings(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant No AI Edit", "whatsapp:+913"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-ai@example.com",
            hash_password("secret"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-ai@example.com", "secret")

    response = client.get(f"/admin/tenants/{tenant_id}/edit")

    assert response.status_code == 403
