from __future__ import annotations

import asyncio
from datetime import datetime

from app.integrations.consultorio_movil import (
    SlotSelection,
    cancel_presential_slot,
    list_next_presential_slots,
    reserve_presential_slot,
)
from app.integrations.interfaces import CalendarProvider, CalendarSlot
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant


def _encode_slot_id(selection: SlotSelection) -> str:
    return "|".join(
        [
            selection.start_at.isoformat(),
            selection.end_at.isoformat(),
            str(selection.duration_minutes),
            selection.timezone,
            selection.label,
        ]
    )


def _decode_slot_id(slot_id: str) -> SlotSelection:
    start_at, end_at, duration, timezone_name, label = slot_id.split("|", 4)
    return SlotSelection(
        number=0,
        start_at=datetime.fromisoformat(start_at),
        end_at=datetime.fromisoformat(end_at),
        duration_minutes=int(duration),
        timezone=timezone_name,
        label=label,
    )


class CabildoProvider(CalendarProvider):
    async def list_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        start: datetime,
        end: datetime,
    ) -> list[CalendarSlot]:
        if consultorio.tipo != TipoConsultorio.PRESENCIAL:
            return []

        selections = await asyncio.to_thread(
            list_next_presential_slots,
            tenant,
            consultorio,
            10,
        )
        return [
            CalendarSlot(
                slot_id=_encode_slot_id(selection),
                start_at=selection.start_at,
                end_at=selection.end_at,
                timezone=selection.timezone,
                provider="consultorio_movil",
                calendar_id=str(
                    (((consultorio.configuracion_externa or {}).get("cabildo") or {}).get("staff_id"))
                    or ""
                ),
            )
            for selection in selections
        ]

    async def reserve_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slot_id: str,
        patient: Paciente,
        metadata: dict,
    ) -> dict:
        _ = metadata
        selection = _decode_slot_id(slot_id)
        result = await asyncio.to_thread(
            reserve_presential_slot,
            tenant,
            consultorio,
            selection,
            patient,
        )
        return {
            "calendar_id": str(
                (((consultorio.configuracion_externa or {}).get("cabildo") or {}).get("staff_id"))
                or ""
            ),
            "event_id": result.get("cabildo_id") or slot_id,
            "start_at": result["start_at"].isoformat(),
            "end_at": result["end_at"].isoformat(),
            "timezone": result.get("timezone") or selection.timezone,
        }

    async def cancel_slot(self, external_event_id: str) -> None:
        _ = external_event_id
        return None

    async def cancel_slot_for_context(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        external_event_id: str,
    ) -> dict:
        return await asyncio.to_thread(
            cancel_presential_slot,
            tenant,
            consultorio,
            external_event_id,
        )

    async def get_event(self, external_event_id: str) -> dict:
        return {"event_id": external_event_id, "provider": "consultorio_movil"}
