from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7

from app.integrations.arca.config import ArcaWsSettings
from app.integrations.arca.http_transport import build_arca_session


class WsaaError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessTicket:
    token: str
    sign: str
    expiration_time: datetime


class WsaaClient:
    def __init__(self, settings: ArcaWsSettings) -> None:
        self.settings = settings

    def request_ticket(self) -> AccessTicket:
        request_xml = self._build_login_ticket_request()
        cms = self._sign_cms(request_xml)
        response_xml = self._login_cms(cms)
        return self._parse_response(response_xml)

    def _build_login_ticket_request(self) -> bytes:
        now = datetime.now(timezone.utc)
        root = ElementTree.Element("loginTicketRequest", version="1.0")
        header = ElementTree.SubElement(root, "header")
        ElementTree.SubElement(header, "uniqueId").text = str(int(time.time()))
        ElementTree.SubElement(header, "generationTime").text = (
            now - timedelta(minutes=10)
        ).isoformat(timespec="seconds")
        ElementTree.SubElement(header, "expirationTime").text = (
            now + timedelta(hours=12)
        ).isoformat(timespec="seconds")
        ElementTree.SubElement(root, "service").text = self.settings.service
        return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)

    def _sign_cms(self, request_xml: bytes) -> str:
        cert_bytes = self.settings.cert_pem.encode("utf-8")
        key_bytes = self.settings.key_pem.encode("utf-8")
        try:
            certificate = x509.load_pem_x509_certificate(cert_bytes)
        except ValueError:
            certificate = x509.load_der_x509_certificate(cert_bytes)

        password = self.settings.key_passphrase.encode("utf-8") if self.settings.key_passphrase else None
        try:
            private_key = serialization.load_pem_private_key(key_bytes, password=password)
        except (TypeError, ValueError) as exc:
            raise WsaaError("No se pudo leer la clave privada ARCA.") from exc

        try:
            cms = (
                pkcs7.PKCS7SignatureBuilder()
                .set_data(request_xml)
                .add_signer(certificate, private_key, hashes.SHA256())
                .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
            )
        except Exception as exc:
            raise WsaaError("No se pudo generar la firma CMS para WSAA.") from exc
        return base64.b64encode(cms).decode("ascii")

    def _login_cms(self, cms: str) -> str:
        try:
            from zeep import Client
            from zeep.exceptions import Error as ZeepError
            from zeep.transports import Transport
        except ImportError as exc:
            raise WsaaError("Falta instalar zeep para invocar WSAA.") from exc

        session = build_arca_session(self.settings.environment)
        transport = Transport(
            session=session,
            timeout=self.settings.timeout_seconds,
            operation_timeout=self.settings.timeout_seconds,
        )
        try:
            client = Client(self.settings.wsaa_wsdl, transport=transport)
            return client.service.loginCms(in0=cms)
        except (requests.RequestException, ZeepError, OSError) as exc:
            raise WsaaError(f"Fallo la comunicacion con WSAA: {exc}") from exc

    @staticmethod
    def _parse_response(response_xml: str) -> AccessTicket:
        try:
            root = ElementTree.fromstring(response_xml)
            token = root.findtext("./credentials/token")
            sign = root.findtext("./credentials/sign")
            expiration = root.findtext("./header/expirationTime")
            if not token or not sign or not expiration:
                raise ValueError("Respuesta incompleta")
            return AccessTicket(
                token=token,
                sign=sign,
                expiration_time=datetime.fromisoformat(expiration.replace("Z", "+00:00")),
            )
        except (ElementTree.ParseError, ValueError) as exc:
            raise WsaaError("WSAA devolvio un LoginTicketResponse invalido.") from exc
