from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from app.models.turno import Turno


class CalendarProvider(ABC):
    @abstractmethod
    async def create_appointment(self, turno: Turno) -> str:
        raise NotImplementedError

    @abstractmethod
    async def cancel_appointment(self, external_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_available_slots(self, start: datetime, end: datetime) -> list[datetime]:
        raise NotImplementedError

    @abstractmethod
    async def book_slot(self, fecha_hora: datetime, notes: str | None = None) -> str:
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
