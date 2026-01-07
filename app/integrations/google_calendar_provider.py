from __future__ import annotations

import json
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.core.config import get_settings
from app.integrations.interfaces import CalendarProvider, CalendarSlot
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant


class GoogleCalendarProvider(CalendarProvider):
    def __init__(
        self,
        calendar_id: str,
        credentials_json: str,
        delegated_user: str | None = None,
    ) -> None:
        self._calendar_id = calendar_id
        self._credentials_json = credentials_json
        self._delegated_user = delegated_user
        self._scopes = ["https://www.googleapis.com/auth/calendar"]

    def _build_service(self):
        info = json.loads(self._credentials_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=self._scopes
        )
        if self._delegated_user:
            creds = creds.with_subject(self._delegated_user)
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _is_slot_available(event: dict, tags: list[str] | None) -> bool:
        summary = (event.get("summary") or "").lower()
        description = (event.get("description") or "").lower()
        if "[disponible]" not in summary and "slot=available" not in description:
            return False
        if tags:
            for tag in tags:
                tag_lower = tag.lower()
                if tag_lower in summary or tag_lower in description:
                    return True
            return False
        return True

    async def list_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        start,
        end,
    ) -> list[CalendarSlot]:
        settings = tenant.calendar_settings or {}
        tags = settings.get("calendar_tags") or []
        timezone = settings.get("default_timezone") or "America/Argentina/Buenos_Aires"

        service = self._build_service()
        events = (
            service.events()
            .list(
                calendarId=self._calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        slots: list[CalendarSlot] = []
        for event in events.get("items", []):
            if not self._is_slot_available(event, tags):
                continue
            start_info = event.get("start") or {}
            end_info = event.get("end") or {}
            start_dt = start_info.get("dateTime")
            end_dt = end_info.get("dateTime")
            if not start_dt or not end_dt:
                continue
            slots.append(
                CalendarSlot(
                    slot_id=event["id"],
                    start_at=_parse_datetime(start_dt),
                    end_at=_parse_datetime(end_dt),
                    timezone=start_info.get("timeZone") or timezone,
                    provider="google",
                    calendar_id=self._calendar_id,
                )
            )
        return slots

    async def reserve_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slot_id: str,
        patient: Paciente,
        metadata: dict,
    ) -> dict:
        settings = tenant.calendar_settings or {}
        tags = settings.get("calendar_tags") or []
        timezone = settings.get("default_timezone") or "America/Argentina/Buenos_Aires"
        virtual_meet_enabled = bool(settings.get("virtual_meet_enabled"))

        service = self._build_service()
        event = service.events().get(calendarId=self._calendar_id, eventId=slot_id).execute()
        if not self._is_slot_available(event, tags):
            raise RuntimeError("Slot no disponible")

        description = event.get("description") or ""
        patient_block = (
            f"\n\nPaciente: {patient.nombre} {patient.apellido}\n"
            f"Telefono: {patient.telefono}\n"
            f"DNI: {patient.dni}\n"
            f"Email: {patient.email}\n"
            f"Metadata: {json.dumps(metadata)}"
        )
        summary = f"Turno confirmado - {patient.nombre} {patient.apellido}"
        body: dict[str, Any] = {
            "summary": summary,
            "description": description + patient_block,
        }
        if consultorio.tipo.value == "virtual" and virtual_meet_enabled:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"meet-{slot_id}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        updated = (
            service.events()
            .patch(
                calendarId=self._calendar_id,
                eventId=slot_id,
                body=body,
                sendUpdates="all",
                conferenceDataVersion=1,
            )
            .execute()
        )
        start_info = updated.get("start") or {}
        end_info = updated.get("end") or {}
        return {
            "event_id": updated.get("id"),
            "calendar_id": self._calendar_id,
            "start_at": start_info.get("dateTime"),
            "end_at": end_info.get("dateTime"),
            "timezone": start_info.get("timeZone") or timezone,
            "html_link": updated.get("htmlLink"),
            "meet_link": (updated.get("conferenceData") or {}).get("entryPoints", [{}])[0].get("uri"),
        }

    async def cancel_slot(self, external_event_id: str) -> None:
        service = self._build_service()
        service.events().delete(
            calendarId=self._calendar_id, eventId=external_event_id, sendUpdates="all"
        ).execute()

    async def get_event(self, external_event_id: str) -> dict:
        service = self._build_service()
        return service.events().get(calendarId=self._calendar_id, eventId=external_event_id).execute()


def resolve_google_credentials(calendar_settings: dict | None) -> tuple[str | None, str | None]:
    settings = get_settings()
    credentials_json = None
    delegated_user = None
    if calendar_settings:
        credentials_json = calendar_settings.get("google_credentials_json") or credentials_json
        delegated_user = calendar_settings.get("google_delegated_user") or delegated_user
    credentials_json = credentials_json or settings.google_credentials_json
    delegated_user = delegated_user or settings.google_delegated_user
    return credentials_json, delegated_user


def _parse_datetime(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
