from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.consultorio import TipoConsultorio


class ConsultorioBase(BaseModel):
    tenant_id: int
    nombre: str
    tipo: TipoConsultorio
    proveedor_turnos: str | None = None
    configuracion_externa: dict | None = None


class ConsultorioCreate(ConsultorioBase):
    pass


class ConsultorioUpdate(BaseModel):
    nombre: str | None = None
    tipo: TipoConsultorio | None = None
    proveedor_turnos: str | None = None
    configuracion_externa: dict | None = None


class ConsultorioRead(ConsultorioBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
