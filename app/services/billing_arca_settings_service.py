from __future__ import annotations

import base64
import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


ARCA_ENVIRONMENTS = {"homo", "prod"}
ARCA_CURRENCIES = {"PES", "DOL", "EUR"}
ARCA_RECEIPT_TYPES = {11, 12, 13}
ARCA_CONCEPTS = {1, 2, 3}


def _fernet() -> Fernet:
    secret = get_settings().secret_key.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    return _fernet().encrypt(cleaned.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""


def normalize_decimal(value: str | Decimal | None) -> str:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return ""
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return ""
    if amount < 0:
        return ""
    return str(amount.quantize(Decimal("0.01")))


def validate_arca_settings(data: dict[str, str], existing: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    existing = existing or {}
    errors: dict[str, str] = {}

    represented_cuit = data.get("represented_cuit", "").strip()
    environment = data.get("environment", "homo").strip().lower()
    pto_vta = data.get("default_pto_vta", "").strip()
    cbte_tipo = data.get("default_cbte_tipo", "").strip()
    concepto = data.get("default_concepto", "").strip()
    currency = data.get("default_currency", "PES").strip().upper()
    mon_cotiz = data.get("default_mon_cotiz", "1").strip().replace(",", ".")
    fiscal_name = data.get("fiscal_name", "").strip()
    fiscal_address = data.get("fiscal_address", "").strip()
    gross_income = data.get("gross_income", "").strip()
    activity_start_date = data.get("activity_start_date", "").strip()
    tax_condition = data.get("tax_condition", "").strip()
    receiver_tax_condition = data.get("receiver_tax_condition", "").strip()
    activity_code = data.get("activity_code", "").strip()
    professional_legend = data.get("professional_legend", "").strip()
    email_subject_template = data.get("email_subject_template", "").strip()
    email_body_template = data.get("email_body_template", "").strip()

    if not re.fullmatch(r"\d{11}", represented_cuit):
        errors["represented_cuit"] = "El CUIT debe tener 11 digitos."
    if environment not in ARCA_ENVIRONMENTS:
        errors["environment"] = "Ambiente invalido."
    if not pto_vta.isdigit() or int(pto_vta) <= 0:
        errors["default_pto_vta"] = "El punto de venta debe ser numerico y mayor a cero."
    if not cbte_tipo.isdigit() or int(cbte_tipo) not in ARCA_RECEIPT_TYPES:
        errors["default_cbte_tipo"] = "Tipo de comprobante invalido para esta etapa."
    if not concepto.isdigit() or int(concepto) not in ARCA_CONCEPTS:
        errors["default_concepto"] = "Concepto invalido."
    if currency not in ARCA_CURRENCIES:
        errors["default_currency"] = "Moneda invalida."
    try:
        cotiz = Decimal(mon_cotiz)
        if cotiz <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errors["default_mon_cotiz"] = "La cotizacion debe ser mayor a cero."
        cotiz = Decimal("1")
    if not fiscal_name:
        errors["fiscal_name"] = "La razon social fiscal es obligatoria."
    if not fiscal_address:
        errors["fiscal_address"] = "El domicilio fiscal es obligatorio."

    certificate_pem = data.get("certificate_pem", "").strip()
    private_key_pem = data.get("private_key_pem", "").strip()
    key_passphrase = data.get("key_passphrase", "").strip()

    settings: dict[str, Any] = {
        "enabled": data.get("enabled") == "on",
        "environment": environment,
        "represented_cuit": represented_cuit,
        "service": "wsfe",
        "default_pto_vta": int(pto_vta) if pto_vta.isdigit() else None,
        "default_cbte_tipo": int(cbte_tipo) if cbte_tipo.isdigit() else None,
        "default_concepto": int(concepto) if concepto.isdigit() else None,
        "default_currency": currency,
        "default_mon_cotiz": str(cotiz),
        "fiscal_name": fiscal_name,
        "fiscal_address": fiscal_address,
        "gross_income": gross_income,
        "activity_start_date": activity_start_date,
        "tax_condition": tax_condition,
        "receiver_tax_condition": receiver_tax_condition,
        "activity_code": activity_code,
        "professional_legend": professional_legend,
        "email_invoice_enabled_default": data.get("email_invoice_enabled_default") == "on",
        "email_subject_template": email_subject_template,
        "email_body_template": email_body_template,
        "diagnosis_required": False,
        "diagnosis_visible_on_invoice": True,
        "certificate_encrypted": existing.get("certificate_encrypted", ""),
        "private_key_encrypted": existing.get("private_key_encrypted", ""),
        "key_passphrase_encrypted": existing.get("key_passphrase_encrypted", ""),
    }

    if certificate_pem:
        settings["certificate_encrypted"] = encrypt_secret(certificate_pem)
    if private_key_pem:
        settings["private_key_encrypted"] = encrypt_secret(private_key_pem)
    if key_passphrase:
        settings["key_passphrase_encrypted"] = encrypt_secret(key_passphrase)

    settings["has_certificate"] = bool(settings["certificate_encrypted"])
    settings["has_private_key"] = bool(settings["private_key_encrypted"])
    settings["has_key_passphrase"] = bool(settings["key_passphrase_encrypted"])
    return settings, errors


def public_arca_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = dict(settings or {})
    settings.pop("certificate_encrypted", None)
    settings.pop("private_key_encrypted", None)
    settings.pop("key_passphrase_encrypted", None)
    settings["has_certificate"] = bool((settings or {}).get("has_certificate"))
    settings["has_private_key"] = bool((settings or {}).get("has_private_key"))
    settings["has_key_passphrase"] = bool((settings or {}).get("has_key_passphrase"))
    return settings
