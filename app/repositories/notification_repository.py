from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        title: str,
        message: str,
        notif_type: str = "info",
        tenant_id: int | None = None,
        user_id: int | None = None,
    ) -> Notification:
        notification = Notification(
            tenant_id=tenant_id,
            user_id=user_id,
            type=notif_type,
            title=title,
            message=message,
        )
        self._session.add(notification)
        await self._session.flush()
        return notification
