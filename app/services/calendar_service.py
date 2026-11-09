from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.integrations.cabildo_provider import CabildoProvider
from app.integrations.drapp_provider import DrAppProvider
from app.integrations.google_calendar_provider import (
    GoogleCalendarProvider,
    get_google_service_account_email,
    resolve_google_credentials,
)
from app.integrations.interfaces import CalendarProvider, CalendarSlot
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.services.google_calendar_slots_service import CalculatedSlot, get_google_calendar_config


logger = logging.getLogger(__name__)


class CalendarService:
    def resolve_provider_name(self, consultorio: Consultorio) -> str:
        # Valor normalizado persistido en turnos.provider / external_calendar_provider.
        provider = (consultorio.proveedor_turnos or "").strip().lower()
        if provider == "google_calendar":
            provider = "google"
        return provider or "google"

    def resolve_external_source_id(self, tenant: Tenant, consultorio: Consultorio) -> str | None:
        provider = self.resolve_provider_name(consultorio)
        if provider == "consultorio_movil":
            return str((((consultorio.configuracion_externa or {}).get("cabildo") or {}).get("staff_id")) or "")
        if provider == "drapp":
            return str((((consultorio.configuracion_externa or {}).get("drapp") or {}).get("staff_id")) or "")
        if provider == "google":
            consultorio_settings = ((consultorio.configuracion_externa or {}).get("google_calendar") or {})
            if consultorio_settings.get("calendar_id"):
                return consultorio_settings.get("calendar_id")
        settings = tenant.calendar_settings or {}
        return settings.get("google_calendar_id")

    def _get_provider(self, tenant: Tenant, consultorio: Consultorio) -> CalendarProvider:
        # Punto unico de resolucion de proveedor externo para evitar ramificaciones
        # divergentes entre Google y Cabildo.
        provider = self.resolve_provider_name(consultorio)
        if provider == "consultorio_movil":
            return CabildoProvider()
        if provider == "drapp":
            return DrAppProvider()
        if provider != "google":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Proveedor de turnos no soportado",
            )
        settings = tenant.calendar_settings or {}
        consultorio_settings = get_google_calendar_config(consultorio)
        calendar_id = consultorio_settings.get("calendar_id") or settings.get("google_calendar_id")
        logger.info(
            "calendar_provider_resolve tenant_id=%s consultorio_id=%s provider=%s has_consultorio_calendar=%s has_fallback_calendar=%s selected_calendar_id=%s has_credentials=%s delegated_user=%s",
            tenant.id,
            consultorio.id,
            provider,
            bool(consultorio_settings.get("calendar_id")),
            bool(settings.get("google_calendar_id")),
            _mask_calendar_id(calendar_id),
            bool(settings.get("google_credentials_json")),
            bool(settings.get("google_delegated_user")),
        )
        if not calendar_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Calendario no configurado",
            )
        credentials_json, delegated_user = resolve_google_credentials(settings)
        if not credentials_json:
            logger.warning(
                "calendar_provider_missing_google_credentials tenant_id=%s consultorio_id=%s calendar_id=%s",
                tenant.id,
                consultorio.id,
                _mask_calendar_id(calendar_id),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Credenciales de Google no configuradas",
            )
        return GoogleCalendarProvider(calendar_id, credentials_json, delegated_user)

    def list_google_calendars(self, tenant: Tenant, candidate_calendar_id: str | None = None) -> list[dict[str, str]]:
        settings = tenant.calendar_settings or {}
        registered_ids = _normalize_calendar_ids(settings.get("google_calendar_ids"))
        legacy_calendar_id = str(settings.get("google_calendar_id") or "").strip()
        if legacy_calendar_id and legacy_calendar_id not in registered_ids:
            registered_ids.append(legacy_calendar_id)
        if candidate_calendar_id:
            registered_ids.append(candidate_calendar_id)
        return self._list_google_calendars(settings, registered_ids)

    def register_google_calendars(
        self,
        calendar_settings: dict,
        calendar_ids: list[str],
    ) -> list[dict[str, str]]:
        registered_ids = _normalize_calendar_ids(calendar_ids)
        calendars = self._list_google_calendars(calendar_settings, registered_ids)
        found_ids = {calendar["id"] for calendar in calendars}
        missing_ids = [calendar_id for calendar_id in registered_ids if calendar_id not in found_ids]
        if missing_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No se pudo acceder a estos calendarios: "
                    f"{', '.join(missing_ids)}. Verifica los IDs y compartilos con la service account "
                    "con permisos para modificar eventos."
                ),
            )
        return calendars

    def _list_google_calendars(
        self,
        settings: dict,
        candidate_calendar_ids: list[str],
    ) -> list[dict[str, str]]:
        credentials_json, delegated_user = resolve_google_credentials(settings)
        if not credentials_json:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Credenciales de Google no configuradas",
            )
        provider = GoogleCalendarProvider("primary", credentials_json, delegated_user)
        calendars = provider.list_calendars()
        for calendar_id in _normalize_calendar_ids(candidate_calendar_ids):
            calendars.extend(provider.list_calendars(calendar_id))
        return _deduplicate_calendars(calendars)

    def get_google_service_account_email(self, tenant: Tenant) -> str | None:
        return get_google_service_account_email(tenant.calendar_settings or {})

    async def list_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        start,
        end,
    ) -> list[CalendarSlot]:
        provider = self._get_provider(tenant, consultorio)
        return await provider.list_available_slots(tenant, consultorio, start, end)

    async def list_calendar_events(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        start,
        end,
    ) -> list[dict]:
        provider = self._get_provider(tenant, consultorio)
        if isinstance(provider, GoogleCalendarProvider):
            return await provider.list_calendar_events(tenant, consultorio, start, end)
        return []

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

    async def generate_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slots: list[CalculatedSlot],
    ) -> dict:
        provider = self._get_provider(tenant, consultorio)
        if not isinstance(provider, GoogleCalendarProvider):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El consultorio no usa Google Calendar")
        logger.info(
            "calendar_service_generate_available_slots tenant_id=%s consultorio_id=%s slots=%s",
            tenant.id,
            consultorio.id,
            len(slots),
        )
        return provider.generate_available_slots(tenant, consultorio, slots)

    async def cancel_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        external_provider: str | None,
        external_event_id: str,
    ) -> None:
        provider_name = (external_provider or self.resolve_provider_name(consultorio)).strip().lower()
        if provider_name == "consultorio_movil":
            await CabildoProvider().cancel_slot_for_context(tenant, consultorio, external_event_id)
            return
        if provider_name == "drapp":
            await DrAppProvider().cancel_slot_for_context(tenant, consultorio, external_event_id)
            return
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
        if provider_name == "consultorio_movil":
            provider = CabildoProvider()
        elif provider_name == "drapp":
            provider = DrAppProvider()
        else:
            provider = self._get_provider(tenant, consultorio)
        return await provider.get_event(external_event_id)


def _mask_calendar_id(calendar_id: str | None) -> str:
    value = str(calendar_id or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def _normalize_calendar_ids(values) -> list[str]:
    if isinstance(values, str):
        values = values.splitlines()
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        calendar_id = str(value or "").strip()
        if calendar_id and calendar_id not in result:
            result.append(calendar_id)
    return result


def _deduplicate_calendars(calendars: list[dict]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for calendar in calendars:
        calendar_id = str(calendar.get("id") or "").strip()
        if not calendar_id or calendar_id in seen:
            continue
        seen.add(calendar_id)
        result.append(
            {
                "id": calendar_id,
                "summary": str(calendar.get("summary") or calendar_id),
                "access_role": str(calendar.get("access_role") or ""),
            }
        )
    return result
