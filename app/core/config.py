from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="development", alias="APP_ENV")
    app_name: str = Field(default="asistente-virtual", alias="APP_NAME")
    secret_key: str = Field(alias="SECRET_KEY")

    twilio_account_sid: str = Field(alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(alias="TWILIO_AUTH_TOKEN")
    twilio_whatsapp_number: str = Field(alias="TWILIO_WHATSAPP_NUMBER")

    database_url: str = Field(alias="DATABASE_URL")
    admin_user: str = Field(default="admin", alias="ADMIN_USER")
    admin_password: str = Field(default="admin", alias="ADMIN_PASSWORD")
    admin_email: str = Field(default="admin@example.com", alias="ADMIN_EMAIL")
    admin_password_seed: str = Field(default="change_me", alias="ADMIN_PASSWORD_SEED")
    mp_access_token: str | None = Field(default=None, alias="MP_ACCESS_TOKEN")
    mp_webhook_secret: str | None = Field(default=None, alias="MP_WEBHOOK_SECRET")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    google_credentials_json: str | None = Field(default=None, alias="GOOGLE_CREDENTIALS_JSON")
    google_delegated_user: str | None = Field(default=None, alias="GOOGLE_DELEGATED_USER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    internal_job_token: str | None = Field(default=None, alias="INTERNAL_JOB_TOKEN")
    vapid_public_key: str | None = Field(default=None, alias="VAPID_PUBLIC_KEY")
    vapid_private_key: str | None = Field(default=None, alias="VAPID_PRIVATE_KEY")
    vapid_subject: str | None = Field(default="mailto:admin@example.com", alias="VAPID_SUBJECT")
    cab_turnos_user: str | None = Field(default=None, alias="CAB_TURNOS_USER")
    cab_turnos_password: str | None = Field(default=None, alias="CAB_TURNOS_PASSWORD")
    cab_turnos_staff_id: str | None = Field(default=None, alias="CAB_TURNOS_STAFF_ID")
    cab_turnos_days: int = Field(default=21, alias="CAB_TURNOS_DAYS")

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )


class DatabaseSettings(BaseSettings):
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    db_host: str | None = Field(default=None, alias="DB_HOST")
    db_user: str | None = Field(default=None, alias="DB_USER")
    db_password: str | None = Field(default=None, alias="DB_PASSWORD")
    db_name: str | None = Field(default=None, alias="DB_NAME")
    db_port: int | None = Field(default=None, alias="DB_PORT")

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()
