from __future__ import annotations

from app.services.messaging_service import MessagingService


class _FakeMessages:
    def __init__(self) -> None:
        self.last_payload = None

    def create(self, **kwargs):
        self.last_payload = kwargs


class _FakeClient:
    def __init__(self, sid: str, token: str) -> None:
        self.sid = sid
        self.token = token
        self.messages = _FakeMessages()


def test_messaging_service_uses_tenant_twilio_settings(monkeypatch):
    captured = {}

    def _client_factory(sid: str, token: str):
        client = _FakeClient(sid, token)
        captured["client"] = client
        return client

    monkeypatch.setattr("app.services.messaging_service.Client", _client_factory)

    class _Tenant:
        whatsapp_settings = {
            "twilio_account_sid": "AC_TENANT",
            "twilio_auth_token": "TENANT_TOKEN",
            "twilio_whatsapp_number": "whatsapp:+5491111111111",
        }

    service = MessagingService()
    service.send_whatsapp("5492222222222", "hola", tenant=_Tenant())

    client = captured["client"]
    assert client.sid == "AC_TENANT"
    assert client.token == "TENANT_TOKEN"
    assert client.messages.last_payload is not None
    assert client.messages.last_payload["from_"] == "whatsapp:+5491111111111"
    assert client.messages.last_payload["to"] == "whatsapp:5492222222222"
