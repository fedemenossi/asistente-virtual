from __future__ import annotations

import hmac
import hashlib
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.payment import PaymentStatus


class MercadoPagoService:
    def __init__(self, access_token: str, base_url: str = "https://api.mercadopago.com") -> None:
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout = 15.0

    async def create_payment_preference(
        self,
        amount: float,
        currency: str,
        description: str,
        external_reference: str,
        notification_url: str | None = None,
        success_url: str | None = None,
        failure_url: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "items": [
                {
                    "title": description,
                    "quantity": 1,
                    "currency_id": currency,
                    "unit_price": float(amount),
                }
            ],
            "external_reference": external_reference,
        }
        if notification_url:
            payload["notification_url"] = notification_url
        if success_url or failure_url:
            payload["back_urls"] = {
                "success": success_url or "",
                "failure": failure_url or "",
            }
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base_url}/checkout/preferences", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self._base_url}/v1/payments/{payment_id}", headers=headers)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def map_mp_status_to_internal(status: str | None) -> PaymentStatus:
        if not status:
            return PaymentStatus.PENDING
        normalized = status.lower()
        if normalized in {"approved"}:
            return PaymentStatus.APPROVED
        if normalized in {"rejected"}:
            return PaymentStatus.REJECTED
        if normalized in {"cancelled"}:
            return PaymentStatus.CANCELLED
        if normalized in {"refunded"}:
            return PaymentStatus.REFUNDED
        return PaymentStatus.PENDING

    @staticmethod
    def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
        # MP sends: x-signature: ts=...,v1=...
        parts = {}
        for item in signature.split(","):
            if "=" in item:
                key, value = item.split("=", 1)
                parts[key.strip()] = value.strip()
        ts = parts.get("ts", "")
        provided = parts.get("v1", "")
        if not ts or not provided:
            return False
        raw = f"{ts}.{payload.decode('utf-8')}"
        digest = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, provided)


def resolve_mp_credentials(payment_settings: dict | None) -> tuple[str | None, str | None]:
    settings = get_settings()
    access_token = None
    webhook_secret = None
    if payment_settings:
        access_token = payment_settings.get("mp_access_token") or access_token
        webhook_secret = payment_settings.get("mp_webhook_secret") or webhook_secret
    access_token = access_token or settings.mp_access_token
    webhook_secret = webhook_secret or settings.mp_webhook_secret
    return access_token, webhook_secret
