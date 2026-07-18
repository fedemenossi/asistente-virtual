from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import engine
from app.models import (  # noqa: F401
    AuditLog,
    BillingEmailLog,
    BillingDiagnostic,
    BillingFiscalContact,
    BillingInvoiceLine,
    BillingSetting,
    BillingSyncState,
    ConversationHistory,
    Notification,
    Payment,
    PaymentEvent,
    PushSubscription,
    Subscription,
    TenantFeature,
)
from app.models.base import Base


SOFT_DELETE_COLUMNS = {
    "tenants": ["deleted_at", "deleted_by"],
    "users": ["deleted_at", "deleted_by"],
    "consultorios": ["deleted_at", "deleted_by"],
    "pacientes": ["deleted_at", "deleted_by"],
    "turnos": ["deleted_at", "deleted_by"],
}

EXTRA_COLUMNS = {
    "users": {
        "created_at": "DATETIME NULL",
        "updated_at": "DATETIME NULL",
    },
    "tenants": {
        "created_at": "DATETIME NULL",
        "updated_at": "DATETIME NULL",
        "payment_settings": "JSON NULL",
        "calendar_settings": "JSON NULL",
        "whatsapp_settings": "JSON NULL",
        "ai_settings": "JSON NULL",
        "arca_settings": "JSON NULL",
        "fantasy_name": "VARCHAR(200) NULL",
        "first_name": "VARCHAR(120) NULL",
        "last_name": "VARCHAR(120) NULL",
        "cuil": "VARCHAR(32) NULL",
        "address": "VARCHAR(255) NULL",
        "postal_code": "VARCHAR(20) NULL",
        "phone": "VARCHAR(40) NULL",
    },
    "estados_conversacion": {
        "status": "VARCHAR(20) NULL",
        "pending_reason": "VARCHAR(50) NULL",
        "pending_message": "VARCHAR(500) NULL",
        "conversation_category": "VARCHAR(64) NULL",
        "conversation_subtype": "VARCHAR(64) NULL",
        "operational_category": "VARCHAR(32) NULL",
        "manual_note": "VARCHAR(1000) NULL",
        "requires_human_review": "BOOLEAN NOT NULL DEFAULT FALSE",
        "has_media": "BOOLEAN NOT NULL DEFAULT FALSE",
        "last_patient_message": "VARCHAR(1000) NULL",
        "media_metadata": "JSON NULL",
        "pending_at": "DATETIME NULL",
        "resolved_at": "DATETIME NULL",
        "resolved_by": "INT NULL",
    },
    "conversaciones_historial": {
        "patient_id": "INT NULL",
        "operational_category": "VARCHAR(32) NULL",
        "manual_note": "VARCHAR(1000) NULL",
    },
    "turnos": {
        "tenant_id": "INT NULL",
        "start_at": "DATETIME NULL",
        "end_at": "DATETIME NULL",
        "timezone": "VARCHAR(64) NULL",
        "provider": "VARCHAR(50) NULL",
        "external_id": "VARCHAR(200) NULL",
        "external_status": "VARCHAR(100) NULL",
        "notes": "TEXT NULL",
        "reminder_24h_sent": "BOOLEAN NOT NULL DEFAULT FALSE",
        "reminder_2h_sent": "BOOLEAN NOT NULL DEFAULT FALSE",
        "created_at": "DATETIME NULL",
        "updated_at": "DATETIME NULL",
        "cancelled_at": "DATETIME NULL",
        "cancellation_reason": "VARCHAR(255) NULL",
        "external_calendar_provider": "VARCHAR(50) NULL",
        "external_calendar_id": "VARCHAR(200) NULL",
        "external_event_id": "VARCHAR(200) NULL",
        "reminder_sent_at": "DATETIME NULL",
        "status": "VARCHAR(32) NULL",
    },
    "pacientes": {
        "created_at": "DATETIME NULL",
        "updated_at": "DATETIME NULL",
        "iva_condition": "VARCHAR(50) NULL",
        "insurance_number": "VARCHAR(100) NULL",
        "fecha_nacimiento": "DATE NULL",
        "tipo_documento": "VARCHAR(50) NULL",
        "numero_documento": "VARCHAR(64) NULL",
        "document_number_normalized": "VARCHAR(64) NULL",
        "financiador_seguro": "VARCHAR(200) NULL",
        "genero": "VARCHAR(50) NULL",
        "telefono_casa": "VARCHAR(64) NULL",
        "direccion": "VARCHAR(200) NULL",
        "direccion_numero": "VARCHAR(40) NULL",
        "departamento": "VARCHAR(40) NULL",
        "piso": "VARCHAR(40) NULL",
        "localidad": "VARCHAR(120) NULL",
        "codigo_postal": "VARCHAR(20) NULL",
        "pais": "VARCHAR(80) NULL",
        "provincia": "VARCHAR(120) NULL",
        "external_provider": "VARCHAR(50) NULL",
        "external_patient_id": "VARCHAR(120) NULL",
        "sync_source": "VARCHAR(50) NULL",
        "synced_at": "DATETIME NULL",
        "external_updated_at": "DATETIME NULL",
        "raw_payload_json": "JSON NULL",
    },
    "push_subscriptions": {
        "created_at": "DATETIME NULL",
        "updated_at": "DATETIME NULL",
    },
    "billing_items": {
        "tax_rate": "DECIMAL(5,2) NULL",
        "iva_id": "VARCHAR(20) NULL",
        "default_item": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "billing_settings": {
        "include_diagnosis_in_invoice_pdf": "BOOLEAN NOT NULL DEFAULT TRUE",
    },
    "billing_sync_states": {
        "last_result": "TEXT NULL",
    },
    "billing_external_consultations": {
        "patient_id": "INT NULL",
        "billing_item_id": "INT NULL",
        "billing_diagnostic_id": "INT NULL",
        "import_batch_id": "VARCHAR(64) NULL",
        "patient_email": "VARCHAR(200) NULL",
        "insurance_name": "VARCHAR(200) NULL",
        "professional_name": "VARCHAR(200) NULL",
        "diagnosis_original": "TEXT NULL",
        "amount": "DECIMAL(12,2) NULL",
        "sale_condition": "VARCHAR(40) NULL",
        "selected_for_billing": "BOOLEAN NOT NULL DEFAULT FALSE",
        "send_email": "BOOLEAN NOT NULL DEFAULT FALSE",
        "status": "VARCHAR(30) NOT NULL DEFAULT 'pending'",
        "billed_at": "DATETIME NULL",
    },
    "billing_invoices": {
        "fiscal_contact_id": "INT NULL",
        "origin": "VARCHAR(30) NOT NULL DEFAULT 'consultation'",
        "receiver_name_snapshot": "VARCHAR(200) NULL",
        "receiver_iva_condition_snapshot": "VARCHAR(50) NULL",
        "service_period_start": "DATE NULL",
        "service_period_end": "DATE NULL",
        "sale_condition": "VARCHAR(80) NULL",
        "external_consultation_id": "INT NULL",
        "billing_item_id": "INT NULL",
        "diagnosis_original_snapshot": "TEXT NULL",
        "diagnosis_final_snapshot": "TEXT NULL",
        "send_email": "BOOLEAN NULL",
        "email_to": "VARCHAR(200) NULL",
        "email_sent_at": "DATETIME NULL",
        "document_html": "TEXT NULL",
        "document_pdf": "LONGBLOB NULL",
        "document_filename": "VARCHAR(255) NULL",
        "document_generated_at": "DATETIME NULL",
        "qr_url": "TEXT NULL",
        "pdf_path": "VARCHAR(500) NULL",
        "pdf_generated_at": "DATETIME NULL",
        "pdf_generated_by": "INT NULL",
        "created_by": "INT NULL",
    },
}


async def _column_exists(conn, db_name: str, table: str, column: str) -> bool:
    stmt = text(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = :schema
          AND TABLE_NAME = :table
          AND COLUMN_NAME = :column
        """
    )
    result = await conn.execute(
        stmt, {"schema": db_name, "table": table, "column": column}
    )
    return (result.scalar() or 0) > 0


async def _add_column(conn, table: str, column: str, ddl: str) -> None:
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def _index_exists(conn, db_name: str, table: str, index_name: str) -> bool:
    stmt = text(
        """
        SELECT COUNT(*)
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = :schema
          AND TABLE_NAME = :table
          AND INDEX_NAME = :index_name
        """
    )
    result = await conn.execute(
        stmt,
        {"schema": db_name, "table": table, "index_name": index_name},
    )
    return (result.scalar() or 0) > 0


async def _timestamp_column_exists(
    conn,
    db_name: str,
    table: str,
) -> bool:
    return await _column_exists(conn, db_name, table, "updated_at")


async def upgrade(db_engine: AsyncEngine, *, dispose: bool = True) -> None:
    url = make_url(db_engine.url)
    db_name = url.database
    if not db_name:
        raise RuntimeError("DATABASE_URL sin nombre de base")

    try:
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            for table, columns in SOFT_DELETE_COLUMNS.items():
                for column in columns:
                    exists = await _column_exists(conn, db_name, table, column)
                    if exists:
                        continue
                    ddl = "DATETIME NULL" if column == "deleted_at" else "INT NULL"
                    await _add_column(conn, table, column, ddl)

            for table, columns in EXTRA_COLUMNS.items():
                for column, ddl in columns.items():
                    exists = await _column_exists(conn, db_name, table, column)
                    if exists:
                        continue
                    await _add_column(conn, table, column, ddl)

            invoice_series_index = "uq_arca_invoice_number_scope"
            if not await _index_exists(conn, db_name, "billing_invoices", invoice_series_index):
                await conn.execute(
                    text(
                        """
                        CREATE UNIQUE INDEX uq_arca_invoice_number_scope
                        ON billing_invoices (
                            tenant_id,
                            environment,
                            represented_cuit,
                            pto_vta,
                            cbte_tipo,
                            cbte_nro
                        )
                        """
                    )
                )

            if await _column_exists(conn, db_name, "turnos", "tenant_id"):
                await conn.execute(
                    text(
                        """
                        UPDATE turnos
                        SET tenant_id = (
                            SELECT consultorios.tenant_id
                            FROM consultorios
                            WHERE consultorios.id = turnos.consultorio_id
                        )
                        WHERE tenant_id IS NULL
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        UPDATE turnos
                        SET provider = COALESCE(provider, external_calendar_provider, 'manual'),
                            external_id = COALESCE(external_id, external_event_id, referencia_externa),
                            external_status = COALESCE(
                                external_status,
                                CASE
                                    WHEN status IS NOT NULL THEN status
                                    WHEN estado IS NOT NULL THEN estado
                                    ELSE 'draft'
                                END
                            ),
                            created_at = COALESCE(created_at, fecha_hora, CURRENT_TIMESTAMP),
                            updated_at = COALESCE(updated_at, created_at, fecha_hora, CURRENT_TIMESTAMP),
                            reminder_24h_sent = COALESCE(reminder_24h_sent, FALSE),
                            reminder_2h_sent = COALESCE(reminder_2h_sent, FALSE)
                        """
                    )
                )

            for table in ("users", "tenants", "pacientes", "push_subscriptions"):
                if await _timestamp_column_exists(conn, db_name, table):
                    await conn.execute(
                        text(
                            f"""
                            UPDATE {table}
                            SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
                            """
                        )
                    )
                    if await _column_exists(conn, db_name, table, "created_at"):
                        await conn.execute(
                            text(
                                f"""
                                UPDATE {table}
                                SET created_at = COALESCE(created_at, updated_at, CURRENT_TIMESTAMP)
                                """
                            )
                        )
    finally:
        if dispose:
            await db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(upgrade(engine))
