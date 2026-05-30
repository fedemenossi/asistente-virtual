from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.core.config import get_settings
from app.integrations.interfaces import CalendarProvider, CalendarSlot
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.services.google_calendar_slots_service import (
    DEFAULT_AVAILABLE_TAG,
    DEFAULT_RESERVED_TAG_TEMPLATE,
    CalculatedSlot,
    get_google_calendar_config,
)


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
        private_props = ((event.get("extendedProperties") or {}).get("private") or {})
        shared_props = ((event.get("extendedProperties") or {}).get("shared") or {})
        slot_status = str(private_props.get("slot_status") or shared_props.get("slot_status") or "").lower()
        generated = str(private_props.get("generated_by_app") or shared_props.get("generated_by_app") or "").lower()
        if slot_status == "available":
            return True
        if generated == "true" and slot_status and slot_status != "available":
            return False
        if DEFAULT_AVAILABLE_TAG.lower() not in summary and "[disponible]" not in summary and "slot=available" not in description:
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
        consultorio_settings = get_google_calendar_config(consultorio)
        tags = settings.get("calendar_tags") or []
        timezone = consultorio_settings.get("timezone") or settings.get("default_timezone") or "America/Argentina/Buenos_Aires"

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
            parsed_start = _parse_datetime(start_dt)
            if parsed_start < datetime.now(parsed_start.tzinfo):
                continue
            slots.append(
                CalendarSlot(
                    slot_id=event["id"],
                    start_at=parsed_start,
                    end_at=_parse_datetime(end_dt),
                    timezone=start_info.get("timeZone") or timezone,
                    provider="google",
                    calendar_id=self._calendar_id,
                )
            )
        return slots

    def list_calendars(self) -> list[dict[str, str]]:
        service = self._build_service()
        result = service.calendarList().list().execute()
        calendars = []
        for item in result.get("items", []) or []:
            calendars.append(
                {
                    "id": item.get("id") or "",
                    "summary": item.get("summary") or item.get("id") or "",
                    "access_role": item.get("accessRole") or "",
                }
            )
        return calendars

    def generate_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slots: list[CalculatedSlot],
    ) -> dict[str, Any]:
        config = get_google_calendar_config(consultorio)
        service = self._build_service()
        if not slots:
            return {"calculated": 0, "created": 0, "duplicates": 0, "conflicts": 0, "errors": []}

        time_min = min(slot.start_at for slot in slots).isoformat()
        time_max = max(slot.end_at for slot in slots).isoformat()
        existing = (
            service.events()
            .list(
                calendarId=self._calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
            .get("items", [])
            or []
        )
        summary = {"calculated": len(slots), "created": 0, "duplicates": 0, "conflicts": 0, "errors": []}
        for slot in slots:
            duplicate = False
            conflict = False
            for event in existing:
                event_start = _event_datetime(event, "start")
                event_end = _event_datetime(event, "end")
                if not event_start or not event_end:
                    continue
                if _same_slot(event, tenant.id, consultorio.id, slot.start_at, slot.end_at):
                    duplicate = True
                    break
                if _overlaps(slot.start_at, slot.end_at, event_start, event_end) and not self._is_slot_available(event, None):
                    conflict = True
                    break
            if duplicate:
                summary["duplicates"] += 1
                continue
            if conflict:
                summary["conflicts"] += 1
                continue
            body = {
                "summary": config.get("available_tag") or DEFAULT_AVAILABLE_TAG,
                "start": {"dateTime": slot.start_at.isoformat(), "timeZone": config["timezone"]},
                "end": {"dateTime": slot.end_at.isoformat(), "timeZone": config["timezone"]},
                "extendedProperties": {
                    "private": {
                        "app": "consultorio_virtual",
                        "tenant_id": str(tenant.id),
                        "consultorio_id": str(consultorio.id),
                        "slot_status": "available",
                        "generated_by_app": "true",
                    }
                },
            }
            try:
                created = service.events().insert(calendarId=self._calendar_id, body=body).execute()
                existing.append(created)
                summary["created"] += 1
            except Exception as exc:
                summary["errors"].append(type(exc).__name__)
        return summary

    async def reserve_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slot_id: str,
        patient: Paciente,
        metadata: dict,
    ) -> dict:
        settings = tenant.calendar_settings or {}
        consultorio_settings = get_google_calendar_config(consultorio)
        tags = settings.get("calendar_tags") or []
        timezone = consultorio_settings.get("timezone") or settings.get("default_timezone") or "America/Argentina/Buenos_Aires"
        virtual_meet_enabled = bool(settings.get("virtual_meet_enabled"))

        service = self._build_service()
        event = service.events().get(calendarId=self._calendar_id, eventId=slot_id).execute()
        if not self._is_slot_available(event, tags):
            raise RuntimeError("Slot no disponible")

        patient_full_name = f"{patient.nombre} {patient.apellido}".strip()
        patient_block = (
            f"Paciente: {patient_full_name}\n"
            f"DNI: {patient.dni or '-'}\n"
            f"Telefono: {patient.telefono or '-'}\n"
            f"Obra social: {patient.obra_social or '-'}\n"
            f"Email: {patient.email or '-'}\n"
            f"Metadata: {json.dumps(metadata, ensure_ascii=True)}"
        )
        template = consultorio_settings.get("reserved_tag_template") or DEFAULT_RESERVED_TAG_TEMPLATE
        summary = template.replace("{patient_full_name}", patient_full_name)
        body: dict[str, Any] = {
            "summary": summary,
            "description": patient_block,
            "extendedProperties": {
                "private": {
                    **(((event.get("extendedProperties") or {}).get("private") or {})),
                    "app": "consultorio_virtual",
                    "tenant_id": str(tenant.id),
                    "consultorio_id": str(consultorio.id),
                    "slot_status": "reserved",
                    "generated_by_app": "true",
                }
            },
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
        event = service.events().get(calendarId=self._calendar_id, eventId=external_event_id).execute()
        private_props = ((event.get("extendedProperties") or {}).get("private") or {})
        private_props["slot_status"] = "available"
        private_props["generated_by_app"] = "true"
        body = {
            "summary": DEFAULT_AVAILABLE_TAG,
            "description": "",
            "extendedProperties": {"private": private_props},
        }
        service.events().patch(
            calendarId=self._calendar_id,
            eventId=external_event_id,
            body=body,
            sendUpdates="all",
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


def _event_datetime(event: dict, field: str):
    value = (event.get(field) or {}).get("dateTime")
    return _parse_datetime(value) if value else None


def _same_slot(event: dict, tenant_id: int, consultorio_id: int, start_at, end_at) -> bool:
    private_props = ((event.get("extendedProperties") or {}).get("private") or {})
    return (
        str(private_props.get("generated_by_app")).lower() == "true"
        and str(private_props.get("tenant_id")) == str(tenant_id)
        and str(private_props.get("consultorio_id")) == str(consultorio_id)
        and _event_datetime(event, "start") == start_at
        and _event_datetime(event, "end") == end_at
    )


def _overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a
