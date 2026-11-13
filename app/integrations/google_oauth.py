from __future__ import annotations

from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build


GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def has_google_oauth_client(calendar_settings: dict | None) -> bool:
    settings = calendar_settings or {}
    return bool(
        str(settings.get("google_oauth_client_id") or "").strip()
        and str(settings.get("google_oauth_client_secret") or "").strip()
    )


def has_google_oauth_connection(calendar_settings: dict | None) -> bool:
    settings = calendar_settings or {}
    return bool(
        has_google_oauth_client(settings)
        and str(settings.get("google_oauth_refresh_token") or "").strip()
    )


def build_google_oauth_flow(
    calendar_settings: dict,
    redirect_uri: str,
    *,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    client_config = {
        "web": {
            "client_id": str(
                calendar_settings.get("google_oauth_client_id") or ""
            ).strip(),
            "client_secret": str(
                calendar_settings.get("google_oauth_client_secret") or ""
            ).strip(),
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_CALENDAR_SCOPES,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=code_verifier is None,
    )
    flow.redirect_uri = redirect_uri
    return flow


def exchange_google_oauth_code(
    calendar_settings: dict,
    redirect_uri: str,
    code: str,
    state: str,
    code_verifier: str,
) -> Credentials:
    flow = build_google_oauth_flow(
        calendar_settings,
        redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )
    flow.fetch_token(code=code)
    return flow.credentials


def resolve_google_oauth_credentials(
    calendar_settings: dict | None,
) -> Credentials | None:
    settings = calendar_settings or {}
    if not has_google_oauth_connection(settings):
        return None
    return Credentials(
        token=None,
        refresh_token=str(settings["google_oauth_refresh_token"]).strip(),
        token_uri=GOOGLE_TOKEN_URI,
        client_id=str(settings["google_oauth_client_id"]).strip(),
        client_secret=str(settings["google_oauth_client_secret"]).strip(),
        scopes=GOOGLE_CALENDAR_SCOPES,
    )


def get_google_oauth_account_email(credentials: Credentials) -> str:
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    calendar: dict[str, Any] = service.calendars().get(calendarId="primary").execute()
    return str(calendar.get("id") or "").strip()
