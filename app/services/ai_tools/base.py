from __future__ import annotations

from typing import Any


def tool_error(
    *,
    consultorio_type: str,
    source: str = "none",
    message: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "source": source,
        "consultorio_type": consultorio_type,
        "slots": [],
        "message": message,
        "error": error,
    }


def tool_success(
    *,
    consultorio_type: str,
    source: str,
    slots: list[dict[str, Any]],
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "source": source,
        "consultorio_type": consultorio_type,
        "slots": slots,
        "message": message,
        "error": None,
    }
