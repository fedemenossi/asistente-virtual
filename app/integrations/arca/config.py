from __future__ import annotations

from dataclasses import dataclass


WSAA_WSDL = {
    "homo": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms?WSDL",
    "prod": "https://wsaa.afip.gov.ar/ws/services/LoginCms?WSDL",
}

WSFE_WSDL = {
    "homo": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL",
    "prod": "https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL",
}


@dataclass(frozen=True)
class ArcaWsSettings:
    environment: str
    represented_cuit: int
    service: str
    cert_pem: str
    key_pem: str
    key_passphrase: str | None = None
    timeout_seconds: int = 30

    @property
    def wsaa_wsdl(self) -> str:
        return WSAA_WSDL[self.environment]

    @property
    def wsfe_wsdl(self) -> str:
        return WSFE_WSDL[self.environment]
