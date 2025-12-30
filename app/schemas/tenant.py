from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.tenant import Tenant


class TenantBase(BaseModel):
    nombre: str
    whatsapp_number: str
    activo: bool = True


class TenantCreate(TenantBase):
    pass


class TenantUpdate(BaseModel):
    nombre: str | None = None
    whatsapp_number: str | None = None
    activo: bool | None = None


class TenantRead(TenantBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
