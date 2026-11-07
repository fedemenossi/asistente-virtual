from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paciente import Paciente
from app.models.patient_contact_link import PatientContactLink, PatientContactLinkStatus
from app.models.whatsapp_contact import WhatsappContact
from app.repositories.conversacion_repository import normalize_phone, normalize_phone_expr


class PatientContactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_contact(self, tenant_id: int, phone: str) -> WhatsappContact:
        normalized_phone = normalize_phone(phone)
        result = await self._session.execute(
            select(WhatsappContact).where(
                WhatsappContact.tenant_id == tenant_id,
                WhatsappContact.phone_normalized == normalized_phone,
            )
        )
        contact = result.scalar_one_or_none()
        if contact is not None:
            return contact

        contact = WhatsappContact(tenant_id=tenant_id, phone_normalized=normalized_phone)
        self._session.add(contact)
        await self._session.flush()
        await self._link_legacy_patients(contact)
        return contact

    async def _link_legacy_patients(self, contact: WhatsappContact) -> None:
        result = await self._session.execute(
            select(Paciente).where(
                Paciente.tenant_id == contact.tenant_id,
                Paciente.deleted_at.is_(None),
                normalize_phone_expr(Paciente.telefono) == contact.phone_normalized,
            )
        )
        for patient in result.scalars().all():
            self._session.add(
                PatientContactLink(
                    tenant_id=contact.tenant_id,
                    contact_id=contact.id,
                    patient_id=patient.id,
                    status=PatientContactLinkStatus.APPROVED.value,
                )
            )
        await self._session.flush()

    async def list_approved_patients(self, contact: WhatsappContact) -> list[Paciente]:
        result = await self._session.execute(
            select(Paciente)
            .join(PatientContactLink, PatientContactLink.patient_id == Paciente.id)
            .where(
                PatientContactLink.tenant_id == contact.tenant_id,
                PatientContactLink.contact_id == contact.id,
                PatientContactLink.status == PatientContactLinkStatus.APPROVED.value,
                Paciente.tenant_id == contact.tenant_id,
                Paciente.deleted_at.is_(None),
            )
            .order_by(Paciente.id)
        )
        return list(result.scalars().all())

    async def get_link(
        self,
        contact: WhatsappContact,
        patient_id: int,
    ) -> PatientContactLink | None:
        result = await self._session.execute(
            select(PatientContactLink).where(
                PatientContactLink.tenant_id == contact.tenant_id,
                PatientContactLink.contact_id == contact.id,
                PatientContactLink.patient_id == patient_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_link_patient(
        self,
        contact: WhatsappContact,
        link_id: int,
    ) -> tuple[PatientContactLink, Paciente] | None:
        result = await self._session.execute(
            select(PatientContactLink, Paciente)
            .join(Paciente, Paciente.id == PatientContactLink.patient_id)
            .where(
                PatientContactLink.id == link_id,
                PatientContactLink.tenant_id == contact.tenant_id,
                PatientContactLink.contact_id == contact.id,
                Paciente.tenant_id == contact.tenant_id,
                Paciente.deleted_at.is_(None),
            )
        )
        return result.one_or_none()

    async def link_patient(
        self,
        contact: WhatsappContact,
        patient: Paciente,
        *,
        status: PatientContactLinkStatus,
    ) -> PatientContactLink:
        if patient.tenant_id != contact.tenant_id:
            raise ValueError("El Contacto y el Paciente deben pertenecer al mismo Tenant")
        existing = await self.get_link(contact, patient.id)
        if existing is not None:
            return existing
        link = PatientContactLink(
            tenant_id=contact.tenant_id,
            contact_id=contact.id,
            patient_id=patient.id,
            status=status.value,
        )
        self._session.add(link)
        await self._session.flush()
        return link
