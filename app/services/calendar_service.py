from __future__ import annotations

from fastapi import HTTPException, status

from app.integrations.cabildo_provider import CabildoProvider
from app.integrations.google_calendar_provider import (
    GoogleCalendarProvider,
    resolve_google_credentials,
)
from app.integrations.interfaces import CalendarProvider, CalendarSlot
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant


class CalendarService:
    def resolve_provider_name(self, consultorio: Consultorio) -> str:
        # Valor normalizado persistido en turnos.provider / external_calendar_provider.
        provider = (consultorio.proveedor_turnos or "").strip().lower()
        return provider or "google"

    def resolve_external_source_id(self, tenant: Tenant, consultorio: Consultorio) -> str | None:
        provider = self.resolve_provider_name(consultorio)
        if provider == "consultorio_movil":
            return str((((consultorio.configuracion_externa or {}).get("cabildo") or {}).get("staff_id")) or "")
        settings = tenant.calendar_settings or {}
        return settings.get("google_calendar_id")

    def _get_provider(self, tenant: Tenant, consultorio: Consultorio) -> CalendarProvider:
        # Punto unico de resolucion de proveedor externo para evitar ramificaciones
        # divergentes entre Google y Cabildo.
        provider = self.resolve_provider_name(consultorio)
        if provider == "consultorio_movil":
            return CabildoProvider()
        if provider != "google":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Proveedor de turnos no soportado",
            )
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
        provider = self._get_provider(tenant, consultorio)
        return await provider.list_available_slots(tenant, consultorio, start, end)

    async def reserve_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slot_id: str,
        patient: Paciente,
        metadata: dict,
    ) -> dict:
        provider = self._get_provider(tenant, consultorio)
        return await provider.reserve_slot(tenant, consultorio, slot_id, patient, metadata)

    async def cancel_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        external_provider: str | None,
        external_event_id: str,
    ) -> None:
        provider_name = (external_provider or self.resolve_provider_name(consultorio)).strip().lower()
        if provider_name == "consultorio_movil":
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="La cancelacion externa en Consultorio Movil no esta implementada",
            )
        provider = self._get_provider(tenant, consultorio)
        await provider.cancel_slot(external_event_id)

    async def get_event(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        external_provider: str | None,
        external_event_id: str,
    ) -> dict:
        provider_name = (external_provider or self.resolve_provider_name(consultorio)).strip().lower()
        provider = CabildoProvider() if provider_name == "consultorio_movil" else self._get_provider(tenant, consultorio)
        return await provider.get_event(external_event_id)
