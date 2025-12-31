from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.web.auth import views

router = APIRouter()

router.add_api_route("/login", views.login_get, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/login", views.login_post, methods=["POST"])
router.add_api_route("/logout", views.logout, methods=["POST"])
router.add_api_route(
    "/admin/login",
    views.admin_login_get,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route("/admin/login", views.admin_login_post, methods=["POST"])
router.add_api_route("/push/vapid-public-key", views.push_vapid_key, methods=["GET"])
router.add_api_route("/push/subscribe", views.push_subscribe, methods=["POST"])
router.add_api_route("/push/unsubscribe", views.push_unsubscribe, methods=["POST"])
router.add_api_route("/push/test", views.push_test, methods=["POST"])
router.add_api_route("/notifications/mark-read", views.notifications_mark_read, methods=["POST"])
router.add_api_route(
    "/notifications/mark-all-read",
    views.notifications_mark_all_read,
    methods=["POST"],
)
