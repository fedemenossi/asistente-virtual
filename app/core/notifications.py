from __future__ import annotations

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, UserRole
from app.core.timezone import now_ba
from app.models.notification import Notification


async def create_notification(
    session: AsyncSession,
    title: str,
    message: str,
    notif_type: str = "info",
    tenant_id: int | None = None,
    user_id: int | None = None,
    link: str | None = None,
) -> Notification:
    notification = Notification(
        tenant_id=tenant_id,
        user_id=user_id,
        type=notif_type,
        title=title,
        message=message,
    )
    session.add(notification)
    await session.flush()
    try:
        from app.services.push_service import send_push_for_notification

        await send_push_for_notification(
            session,
            notification_id=notification.id,
            title=title,
            message=message,
            tenant_id=tenant_id,
            user_id=user_id,
            link=link,
        )
    except Exception:
        pass
    return notification


async def get_recent_notifications(
    session: AsyncSession,
    user: CurrentUser | None,
    limit: int = 10,
) -> list[Notification]:
    if user is None:
        return []
    if user.role == UserRole.SUPER_ADMIN:
        stmt = select(Notification).where(Notification.tenant_id.is_(None))
    else:
        stmt = select(Notification).where(
            or_(
                Notification.user_id == user.id,
                Notification.tenant_id == user.tenant_id,
            )
        )
    result = await session.execute(stmt.order_by(desc(Notification.created_at)).limit(limit))
    return list(result.scalars().all())


async def count_unread_notifications(
    session: AsyncSession,
    user: CurrentUser | None,
) -> int:
    if user is None:
        return 0
    if user.role == UserRole.SUPER_ADMIN:
        stmt = select(func.count()).select_from(Notification).where(
            Notification.tenant_id.is_(None), Notification.read_at.is_(None)
        )
    else:
        stmt = select(func.count()).select_from(Notification).where(
            or_(
                Notification.user_id == user.id,
                Notification.tenant_id == user.tenant_id,
            ),
            Notification.read_at.is_(None),
        )
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def mark_notification_read(
    session: AsyncSession,
    notification: Notification,
) -> None:
    notification.read_at = now_ba()
    await session.flush()
