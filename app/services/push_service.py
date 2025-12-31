from __future__ import annotations

import json
from typing import Any

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.push_subscription import PushSubscription
from app.models.user import User, UserRole


def _get_vapid_config() -> dict[str, str] | None:
    settings = get_settings()
    if not settings.vapid_public_key or not settings.vapid_private_key:
        return None
    return {
        "public": settings.vapid_public_key,
        "private": settings.vapid_private_key,
        "subject": settings.vapid_subject or "mailto:admin@example.com",
    }


async def save_subscription(
    session: AsyncSession,
    user_id: int,
    tenant_id: int | None,
    subscription: dict[str, Any],
) -> None:
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return

    result = await session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.user_id = user_id
        existing.tenant_id = tenant_id
        existing.p256dh = p256dh
        existing.auth = auth
        await session.flush()
        return

    session.add(
        PushSubscription(
            user_id=user_id,
            tenant_id=tenant_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
        )
    )
    await session.flush()


async def delete_subscription(
    session: AsyncSession,
    user_id: int,
    endpoint: str,
) -> None:
    result = await session.execute(
        select(PushSubscription).where(
            PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        await session.delete(existing)


async def delete_user_subscriptions(session: AsyncSession, user_id: int) -> None:
    result = await session.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    for sub in result.scalars().all():
        await session.delete(sub)


async def _send_subscription_payload(
    session: AsyncSession,
    subscription: PushSubscription,
    payload: dict[str, Any],
) -> None:
    config = _get_vapid_config()
    if not config:
        return
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=config["private"],
            vapid_claims={"sub": config["subject"]},
        )
    except WebPushException as exc:
        if getattr(exc, "response", None) is not None and exc.response.status_code in (404, 410):
            await session.delete(subscription)


async def send_push_to_user(
    session: AsyncSession,
    user_id: int,
    title: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    result = await session.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    subs = list(result.scalars().all())
    if not subs:
        return
    payload = {"title": title, "message": message, "data": data or {}}
    for sub in subs:
        await _send_subscription_payload(session, sub, payload)


async def send_push_for_notification(
    session: AsyncSession,
    notification_id: int,
    title: str,
    message: str,
    tenant_id: int | None,
    user_id: int | None,
    link: str | None = None,
) -> None:
    if user_id:
        await send_push_to_user(
            session,
            user_id=user_id,
            title=title,
            message=message,
            data={"notification_id": notification_id, "link": link},
        )
        return

    if tenant_id is not None:
        result = await session.execute(
            select(User.id).where(
                User.tenant_id == tenant_id,
                User.active.is_(True),
                User.role == UserRole.TENANT_ADMIN.value,
            )
        )
        user_ids = [row[0] for row in result.all()]
    else:
        result = await session.execute(
            select(User.id).where(
                User.role == UserRole.SUPER_ADMIN.value, User.active.is_(True)
            )
        )
        user_ids = [row[0] for row in result.all()]

    for uid in user_ids:
        await send_push_to_user(
            session,
            user_id=uid,
            title=title,
            message=message,
            data={"notification_id": notification_id, "link": link},
        )
