from __future__ import annotations

from fastapi import HTTPException, status

from app.integrations.google_calendar_provider import (
    GoogleCalendarProvider,
    resolve_google_credentials,
)
from app.integrations.interfaces import CalendarProvider, CalendarSlot
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant


class CalendarService:
    def _get_provider(self, tenant: Tenant) -> CalendarProvider:
        settings = tenant.calendar_settings or {}
        calendar_id = settings.get("google_calendar_id")
        if not calendar_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Calendario no configurado",
            )
        credentials_json, delegated_user = resolve_google_credentials(settings)
        if not credentials_json:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Credenciales de Google no configuradas",
            )
        return GoogleCalendarProvider(calendar_id, credentials_json, delegated_user)

    async def list_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        start,
        end,
    ) -> list[CalendarSlot]:
        provider = self._get_provider(tenant)
        return await provider.list_available_slots(tenant, consultorio, start, end)

    async def reserve_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slot_id: str,
        patient: Paciente,
        metadata: dict,
    ) -> dict:
        provider = self._get_provider(tenant)
        return await provider.reserve_slot(tenant, consultorio, slot_id, patient, metadata)

    async def cancel_slot(self, tenant: Tenant, external_event_id: str) -> None:
        provider = self._get_provider(tenant)
        await provider.cancel_slot(external_event_id)

    async def get_event(self, tenant: Tenant, external_event_id: str) -> dict:
        provider = self._get_provider(tenant)
        return await provider.get_event(external_event_id)
