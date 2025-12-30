from __future__ import annotations

from pydantic import BaseModel, Field


class TwilioWebhookPayload(BaseModel):
    from_number: str = Field(alias="From")
    to_number: str = Field(alias="To")
    body: str = Field(alias="Body")
    message_sid: str | None = Field(default=None, alias="MessageSid")

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }
