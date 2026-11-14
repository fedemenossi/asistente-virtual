from __future__ import annotations

import json
import logging
from typing import Any

from app.core.request_context import get_current_request_id
from app.core.tenancy import get_current_tenant_id
from app.core.timezone import now_ba


SAFE_EXTRA_FIELDS = (
    "job_id",
    "sync_source",
    "source",
    "rows_count",
    "row_index",
    "error_type",
    "database_error",
    "logged_errors",
    "sync_total_rows",
    "sync_created",
    "sync_updated",
    "sync_existing",
    "sync_missing_document",
    "sync_conflicts",
    "sync_errors",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record_tenant_id = getattr(record, "tenant_id", None)
        payload: dict[str, Any] = {
            "ts": now_ba().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_current_request_id(),
            "tenant_id": record_tenant_id if record_tenant_id is not None else get_current_tenant_id(),
        }
        for field in SAFE_EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
