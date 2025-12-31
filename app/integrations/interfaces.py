from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant


@dataclass
class CalendarSlot:
    slot_id: str
    start_at: datetime
    end_at: datetime
    timezone: str
    provider: str
    calendar_id: str


class CalendarProvider(ABC):
    @abstractmethod
    async def list_available_slots(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        start: datetime,
        end: datetime,
    ) -> list[CalendarSlot]:
        raise NotImplementedError

    @abstractmethod
    async def reserve_slot(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        slot_id: str,
        patient: Paciente,
        metadata: dict,
    ) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def cancel_slot(self, external_event_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_event(self, external_event_id: str) -> dict:
        raise NotImplementedError


class PaymentsProvider(ABC):
    @abstractmethod
    async def create_payment_preference(self, amount: float, currency: str, description: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def get_payment(self, external_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def map_status(self, status: str | None) -> str:
        raise NotImplementedError


class ExternalSchedulingProvider(ABC):
    @abstractmethod
    async def request_slot(self, fecha_hora: datetime, notes: str | None = None) -> str:
        raise NotImplementedError
