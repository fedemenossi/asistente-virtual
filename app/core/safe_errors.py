from __future__ import annotations

import re


_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(?:token|sign|authorization|certificate|certificado|private[_ -]?key|clave privada|password|secret)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]+-----")


def safe_operational_error(value: object, *, fallback: str) -> str:
    """Return a short operator message without credentials or PEM material."""
    message = " ".join(str(value or "").split())
    if not message or _PEM_BLOCK.search(message):
        return fallback
    message = _SENSITIVE_VALUE.sub("dato protegido", message)
    return message[:500]
