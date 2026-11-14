from __future__ import annotations

import json
import logging

from app.core.logging import JsonFormatter


def test_json_formatter_includes_safe_background_job_context():
    record = logging.LogRecord(
        name="app.services.patient_sync_service",
        level=logging.ERROR,
        pathname=__file__,
        lineno=12,
        msg="patient_sync_row_failed",
        args=(),
        exc_info=None,
    )
    record.tenant_id = 7
    record.row_index = 23
    record.error_type = "IntegrityError"
    record.database_error = "NOT NULL constraint failed: pacientes.nombre"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["tenant_id"] == 7
    assert payload["row_index"] == 23
    assert payload["error_type"] == "IntegrityError"
    assert payload["database_error"] == "NOT NULL constraint failed: pacientes.nombre"
