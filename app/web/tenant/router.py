from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.core.security import require_tenant_admin
from app.web.tenant import views

router = APIRouter(prefix="/t", dependencies=[Depends(require_tenant_admin)])

router.add_api_route(
    "/dashboard", views.dashboard, methods=["GET"], response_class=HTMLResponse
)
router.add_api_route(
    "/consultorios", views.consultorios_list, methods=["GET"], response_class=HTMLResponse
)
router.add_api_route(
    "/consultorios/new",
    views.consultorios_new_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/consultorios/new", views.consultorios_new_post, methods=["POST"])
router.add_api_route(
    "/consultorios/{consultorio_id}/edit",
    views.consultorios_edit_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/consultorios/{consultorio_id}/edit", views.consultorios_edit_post, methods=["POST"])
router.add_api_route("/consultorios/{consultorio_id}/delete", views.consultorios_delete, methods=["POST"])

router.add_api_route("/pacientes", views.pacientes_list, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/pacientes/new",
    views.pacientes_new_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/pacientes/new", views.pacientes_new_post, methods=["POST"])
router.add_api_route(
    "/pacientes/{paciente_id}/edit",
    views.pacientes_edit_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/pacientes/{paciente_id}/edit", views.pacientes_edit_post, methods=["POST"])
router.add_api_route("/pacientes/{paciente_id}/delete", views.pacientes_delete, methods=["POST"])

router.add_api_route("/turnos", views.turnos_list, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/turnos/{turno_id}",
    views.turnos_detail,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/payments", views.payments_list, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/payments/{payment_id}",
    views.payment_detail,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/conversation-states",
    views.conversation_states,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/audit-logs", views.audit_logs, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/notifications",
    views.notifications_list,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/notifications/{notification_id}/read",
    views.notifications_mark_read,
    methods=["POST"],
)
router.add_api_route(
    "/settings", views.settings_get, methods=["GET"], response_class=HTMLResponse
)
router.add_api_route("/settings", views.settings_post, methods=["POST"])
router.add_api_route(
    "/settings/payments",
    views.payment_settings_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/settings/payments", views.payment_settings_post, methods=["POST"])
