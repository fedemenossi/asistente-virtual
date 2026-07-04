from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable

import requests

from app.integrations.arca.config import ArcaWsSettings
from app.integrations.arca.http_transport import build_arca_session


class WsfeError(RuntimeError):
    def __init__(self, message: str, errors: list["ServiceMessage"] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass(frozen=True)
class ServiceMessage:
    code: int
    message: str


@dataclass
class WsfeResult:
    data: Any = None
    errors: list[ServiceMessage] = field(default_factory=list)
    events: list[ServiceMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "errors": [asdict(item) for item in self.errors],
            "events": [asdict(item) for item in self.events],
        }


def normalize(value: Any) -> Any:
    try:
        from zeep.helpers import serialize_object
    except ImportError:
        serialized = value
    else:
        serialized = serialize_object(value)
    if is_dataclass(serialized):
        return asdict(serialized)
    if isinstance(serialized, dict):
        return {str(key): normalize(item) for key, item in serialized.items()}
    if isinstance(serialized, (list, tuple)):
        return [normalize(item) for item in serialized]
    return serialized


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _read_messages(container: Any, item_name: str) -> list[ServiceMessage]:
    data = normalize(container)
    if not data:
        return []
    if isinstance(data, dict):
        data = data.get(item_name, data.get(item_name.lower(), []))
    messages = []
    for item in _as_list(data):
        if isinstance(item, dict):
            code = item.get("Code", item.get("code", 0))
            msg = item.get("Msg", item.get("msg", ""))
        else:
            code = getattr(item, "Code", 0)
            msg = getattr(item, "Msg", "")
        messages.append(ServiceMessage(int(code), str(msg)))
    return messages


class WsfeClient:
    def __init__(self, settings: ArcaWsSettings, auth_provider: Callable[[], dict[str, Any]]) -> None:
        self.settings = settings
        self._auth_provider = auth_provider
        self._client = None

    def dummy(self) -> WsfeResult:
        return self._invoke("FEDummy", authenticated=False)

    def get_puntos_venta(self) -> WsfeResult:
        return self._invoke("FEParamGetPtosVenta")

    def get_tipos_comprobante(self) -> WsfeResult:
        return self._invoke("FEParamGetTiposCbte")

    def get_ultimo_autorizado(self, pto_vta: int, cbte_tipo: int) -> WsfeResult:
        return self._invoke("FECompUltimoAutorizado", PtoVta=pto_vta, CbteTipo=cbte_tipo)

    def consultar_comprobante(self, pto_vta: int, cbte_tipo: int, cbte_nro: int) -> WsfeResult:
        return self._invoke(
            "FECompConsultar",
            FeCompConsReq={"CbteTipo": cbte_tipo, "CbteNro": cbte_nro, "PtoVta": pto_vta},
        )

    def solicitar_cae(self, fe_cae_req: dict[str, Any]) -> WsfeResult:
        return self._invoke("FECAESolicitar", FeCAEReq=fe_cae_req)

    @property
    def client(self):
        if self._client is None:
            try:
                from zeep import Client
                from zeep.exceptions import Error as ZeepError
                from zeep.transports import Transport
            except ImportError as exc:
                raise WsfeError("Falta instalar zeep para invocar WSFEv1.") from exc
            session = build_arca_session(self.settings.environment)
            transport = Transport(
                session=session,
                timeout=self.settings.timeout_seconds,
                operation_timeout=self.settings.timeout_seconds,
            )
            try:
                self._client = Client(self.settings.wsfe_wsdl, transport=transport)
            except (requests.RequestException, ZeepError, OSError) as exc:
                raise WsfeError(f"No se pudo inicializar WSFEv1: {exc}") from exc
        return self._client

    def _invoke(self, operation: str, *, authenticated: bool = True, **kwargs: Any) -> WsfeResult:
        try:
            from zeep.exceptions import Error as ZeepError
        except ImportError:
            ZeepError = Exception
        if authenticated:
            kwargs = {"Auth": self._auth_provider(), **kwargs}
        try:
            response = getattr(self.client.service, operation)(**kwargs)
        except (requests.RequestException, ZeepError, OSError) as exc:
            raise WsfeError(f"Fallo WSFEv1.{operation}: {exc}") from exc

        errors = _read_messages(getattr(response, "Errors", None), "Err")
        events = _read_messages(getattr(response, "Events", None), "Evt")
        data = normalize(getattr(response, "ResultGet", response))
        result = WsfeResult(data=data, errors=errors, events=events)
        if errors:
            details = "; ".join(f"{item.code}: {item.message}" for item in errors)
            raise WsfeError(f"WSFEv1.{operation} devolvio errores: {details}", errors)
        return result
