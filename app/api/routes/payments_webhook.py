from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_async_session
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/webhook/payments", tags=["webhooks"])


@router.post("/mercadopago")
async def mercadopago_webhook(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    raw_body = await request.body()
    payload = await request.json()
    payment_id = request.query_params.get("payment_id")
    payment_id_int = int(payment_id) if payment_id and payment_id.isdigit() else None
    service = PaymentService(session)
    await service.handle_mp_webhook(request, payload, raw_body, payment_id_int)
    return {"status": "ok"}
