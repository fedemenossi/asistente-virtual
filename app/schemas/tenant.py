from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.tenant import Tenant


class TenantBase(BaseModel):
    nombre: str
    whatsapp_number: str
    activo: bool = True
    fantasy_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    cuil: str | None = None
    address: str | None = None
    postal_code: str | None = None
    phone: str | None = None


class TenantCreate(TenantBase):
    pass


class TenantUpdate(BaseModel):
    nombre: str | None = None
    whatsapp_number: str | None = None
    activo: bool | None = None
    fantasy_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    cuil: str | None = None
    address: str | None = None
    postal_code: str | None = None
    phone: str | None = None


class TenantRead(TenantBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
