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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


class DatabaseSettings(BaseSettings):
    database_url: str = Field(alias="DATABASE_URL")

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()
