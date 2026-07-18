from __future__ import annotations


RECEIVER_IVA_CONDITIONS = {
    "consumidor_final": "Consumidor final",
    "responsable_inscripto": "Responsable inscripto",
    "monotributista": "Monotributista",
    "exento": "Exento",
    "no_categorizado": "No categorizado",
}


def normalize_receiver_iva_condition(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in RECEIVER_IVA_CONDITIONS:
        raise ValueError("Condicion frente al IVA invalida.")
    return normalized
