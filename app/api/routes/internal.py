from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_async_session
from app.services.reminder_service import ReminderService

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/reminders/run")
async def run_reminders(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    settings = get_settings()
    token = request.headers.get("X-Internal-Token")
    if not settings.internal_job_token or token != settings.internal_job_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    hours_before = request.query_params.get("hours_before", "24")
    try:
        hours_value = int(hours_before)
    except ValueError:
        hours_value = 24
    sent = await ReminderService(session).run(request, hours_before=hours_value)
    return {"sent": sent}
