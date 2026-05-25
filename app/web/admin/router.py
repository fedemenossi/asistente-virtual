from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.core.security import require_super_admin
from app.web.admin import views

router = APIRouter(prefix="/admin", dependencies=[Depends(require_super_admin)])

router.add_api_route(
    "/dashboard", views.dashboard, methods=["GET"], response_class=HTMLResponse
)
router.add_api_route(
    "/tenants", views.tenants_list, methods=["GET"], response_class=HTMLResponse
)
router.add_api_route(
    "/tenants/new", views.tenants_new_get, methods=["GET"], response_class=HTMLResponse
)
router.add_api_route("/tenants/new", views.tenants_new_post, methods=["POST"])
router.add_api_route(
    "/tenants/{tenant_id}",
    views.tenants_detail,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/tenants/{tenant_id}/edit",
    views.tenants_edit_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/tenants/{tenant_id}/edit", views.tenants_edit_post, methods=["POST"])
router.add_api_route("/tenants/{tenant_id}/toggle", views.tenants_toggle, methods=["POST"])
router.add_api_route("/tenants/{tenant_id}/delete", views.tenants_delete, methods=["POST"])

router.add_api_route("/users", views.users_list, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/users/new", views.users_new_get, methods=["GET"], response_class=HTMLResponse
)
router.add_api_route("/users/new", views.users_new_post, methods=["POST"])
router.add_api_route(
    "/users/{user_id}/edit",
    views.users_edit_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/users/{user_id}/edit", views.users_edit_post, methods=["POST"])
router.add_api_route("/users/{user_id}/toggle", views.users_toggle, methods=["POST"])
router.add_api_route("/users/{user_id}/delete", views.users_delete, methods=["POST"])
router.add_api_route("/audit-logs", views.audit_logs, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/calendars", views.calendars, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/appointments",
    views.appointments_list,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/appointments/{turno_id}",
    views.appointment_detail,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/appointments/{turno_id}/cancel",
    views.appointment_cancel,
    methods=["POST"],
)
router.add_api_route(
    "/conversation-states",
    views.conversation_states,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/conversation-states/{tenant_id}/{telefono}",
    views.conversation_state_detail,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/conversation-states/history/{history_id}",
    views.conversation_history_detail,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/conversation-states/{tenant_id}/{telefono}/resolve",
    views.conversation_state_resolve,
    methods=["POST"],
)
router.add_api_route(
    "/conversation-states/{tenant_id}/{telefono}/review",
    views.conversation_state_review_update,
    methods=["POST"],
)
router.add_api_route("/payments", views.payments_list, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/payments/{payment_id}",
    views.payment_detail,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/settings/notifications",
    views.notifications_settings,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/chat-simulator",
    views.chat_simulator_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/chat-simulator/send",
    views.chat_simulator_send,
    methods=["POST"],
)
router.add_api_route(
    "/chat-simulator/api",
    views.chat_simulator_api,
    methods=["POST"],
)
router.add_api_route(
    "/chat-simulator/patients",
    views.chat_simulator_patients,
    methods=["GET"],
)
router.add_api_route(
    "/chat-simulator/reset",
    views.chat_simulator_reset,
    methods=["POST"],
)
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
    "/tenant-features",
    views.tenant_features_list,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/tenant-features/{tenant_id}",
    views.tenant_features_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/tenant-features/{tenant_id}",
    views.tenant_features_post,
    methods=["POST"],
)
