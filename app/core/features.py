from __future__ import annotations

FEATURE_REGISTRY: dict[str, dict] = {
    "dashboard": {
        "label": "Dashboard",
        "routes": ["/t/dashboard"],
        "default_enabled": True,
    },
    "consultorios": {
        "label": "Consultorios",
        "routes": ["/t/consultorios"],
        "default_enabled": True,
    },
    "pacientes": {
        "label": "Pacientes",
        "routes": ["/t/pacientes"],
        "default_enabled": True,
    },
    "turnos": {
        "label": "Turnos legacy",
        "routes": ["/t/turnos"],
        "default_enabled": True,
    },
    "appointments": {
        "label": "Turnos",
        "routes": ["/t/appointments"],
        "default_enabled": True,
    },
    "payments": {
        "label": "Pagos",
        "routes": ["/t/payments"],
        "default_enabled": True,
    },
    "billing_arca": {
        "label": "Facturacion ARCA",
        "routes": [
            "/t/billing",
            "/t/billing-arca",
            "/t/billing/pending",
            "/t/billing/invoices",
            "/t/settings/billing",
            "/t/settings/billing-arca",
        ],
        "default_enabled": True,
    },
    "conversaciones": {
        "label": "Conversaciones",
        "routes": ["/t/conversation-states"],
        "default_enabled": True,
    },
    "audit_logs": {
        "label": "Audit logs",
        "routes": ["/t/audit-logs"],
        "default_enabled": True,
    },
    "notifications": {
        "label": "Notificaciones",
        "routes": ["/t/notifications"],
        "default_enabled": True,
    },
    "settings": {
        "label": "Settings",
        "routes": ["/t/settings"],
        "default_enabled": True,
        "children": ["settings_payments", "settings_calendar", "settings_notifications"],
    },
    "settings_payments": {
        "label": "Settings pagos",
        "routes": ["/t/settings/payments"],
        "default_enabled": True,
    },
    "settings_calendar": {
        "label": "Settings calendario",
        "routes": ["/t/settings/calendar"],
        "default_enabled": True,
    },
    "settings_notifications": {
        "label": "Settings notificaciones",
        "routes": ["/t/settings/notifications"],
        "default_enabled": True,
    },
}


def feature_defaults() -> dict[str, bool]:
    return {
        key: bool(meta.get("default_enabled", True))
        for key, meta in FEATURE_REGISTRY.items()
    }
