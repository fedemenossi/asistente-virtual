from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from app.integrations import consultorio_movil
from app.integrations.interfaces import CalendarProvider, CalendarSlot
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant


class DrAppInvalidResponse(RuntimeError):
    pass


def _config(consultorio: Consultorio) -> dict:
    return (consultorio.configuracion_externa or {}).get("drapp") or {}


def _encode_selection(selection: consultorio_movil.SlotSelection) -> str:
    return json.dumps(
        {
            "v": 1,
            "n": selection.number,
            "s": selection.start_at.isoformat(),
            "d": selection.duration_minutes,
            "z": selection.timezone,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_selection(slot_id: str) -> consultorio_movil.SlotSelection:
    try:
        data = json.loads(slot_id)
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError("version")
        start_at = datetime.fromisoformat(str(data["s"]))
        duration_minutes = int(data["d"])
        selection = consultorio_movil.SlotSelection(
            number=int(data["n"]),
            start_at=start_at,
            end_at=start_at + timedelta(minutes=duration_minutes),
            duration_minutes=duration_minutes,
            timezone=str(data["z"]),
            label="",
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DrAppInvalidResponse("Referencia de disponibilidad DrApp invalida.") from exc
    if (
        selection.start_at.utcoffset() is None
        or selection.duration_minutes <= 0
        or not selection.timezone.strip()
    ):
        raise DrAppInvalidResponse("Referencia de disponibilidad DrApp invalida.")
    return selection


class DrAppProvider(CalendarProvider):
    async def list_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        start: datetime,
        end: datetime,
    ) -> list[CalendarSlot]:
        del start, end
        if consultorio.tipo != TipoConsultorio.PRESENCIAL:
            return []
        selections = await asyncio.to_thread(
            consultorio_movil.list_next_presential_slots,
            tenant,
            consultorio,
            50,
        )
        if not isinstance(selections, list):
            raise DrAppInvalidResponse("DrApp devolvio una respuesta de disponibilidad invalida.")
        staff_id = str(_config(consultorio).get("staff_id") or "")
        slots: list[CalendarSlot] = []
        for selection in selections:
            if not isinstance(selection, consultorio_movil.SlotSelection):
                raise DrAppInvalidResponse("DrApp devolvio una disponibilidad invalida.")
            if (
                not isinstance(selection.start_at, datetime)
                or not isinstance(selection.end_at, datetime)
                or selection.start_at.utcoffset() is None
                or selection.end_at.utcoffset() is None
                or selection.end_at <= selection.start_at
                or selection.duration_minutes <= 0
                or not str(selection.timezone or "").strip()
            ):
                raise DrAppInvalidResponse("DrApp devolvio una disponibilidad invalida.")
            slots.append(
                CalendarSlot(
                    slot_id=_encode_selection(selection),
                    start_at=selection.start_at,
                    end_at=selection.end_at,
                    timezone=selection.timezone,
                    provider="drapp",
                    calendar_id=staff_id,
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
        del metadata
        selection = _decode_selection(slot_id)
        result = await asyncio.to_thread(
            consultorio_movil.reserve_presential_slot,
            tenant,
            consultorio,
            selection,
            patient,
        )
        external_id = str(result.get("cabildo_id") or "").strip() if isinstance(result, dict) else ""
        if not external_id or len(external_id) > 200:
            raise DrAppInvalidResponse("DrApp no devolvio una referencia de reserva valida.")
        start_at = result.get("start_at") or selection.start_at
        end_at = result.get("end_at") or selection.end_at
        if not isinstance(start_at, datetime) or not isinstance(end_at, datetime) or end_at <= start_at:
            raise DrAppInvalidResponse("DrApp devolvio fechas de reserva invalidas.")
        return {
            "calendar_id": str(_config(consultorio).get("staff_id") or ""),
            "event_id": external_id,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "timezone": str(result.get("timezone") or selection.timezone),
        }

    async def cancel_slot(self, external_event_id: str) -> None:
        del external_event_id

    async def cancel_slot_for_context(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        external_event_id: str,
    ) -> dict:
        return await asyncio.to_thread(
            consultorio_movil.cancel_presential_slot,
            tenant,
            consultorio,
            external_event_id,
        )

    async def get_event(self, external_event_id: str) -> dict:
        return {"event_id": external_event_id, "provider": "drapp"}
