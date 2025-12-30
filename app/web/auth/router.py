from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.web.auth import views

router = APIRouter()

router.add_api_route("/login", views.login_get, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/login", views.login_post, methods=["POST"])
router.add_api_route("/logout", views.logout, methods=["POST"])
router.add_api_route("/admin/login", views.login_get, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/admin/login", views.login_post, methods=["POST"])
