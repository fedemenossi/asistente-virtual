from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.core.timezone import now_ba

LOGIN_URL = "https://office.consultoriomovil.net/office/"
AUTH_URL = f"{LOGIN_URL}login/authenticate"
LOGIN_MAX_ATTEMPTS = 3
API_AVAILABILITY_URL = "https://office.consultoriomovil.net/api/appointment-availability"
PATIENT_SAVE_URL = "https://office.consultoriomovil.net/office/patient/save"
APPOINTMENT_SAVE_URL = "https://office.consultoriomovil.net/office/appointment/appointment/save"
APPOINTMENT_PRACTICES_URL = "https://office.consultoriomovil.net/office/appointment/appointment/loadPractices"
APPOINTMENT_STATUS_URL = "https://office.consultoriomovil.net/office/appointment/list/status"
APPOINTMENT_LIST_URL = "https://office.consultoriomovil.net/office/appointment/list/ajax"
PATIENT_LIST_URL = "https://office.consultoriomovil.net/office/patient/"
SEEN_PATIENT_REPORT_URL = "https://office.consultoriomovil.net/office/report/seenPatientReport/index/"
SEEN_PATIENT_REPORT_AJAX_URLS = [
    SEEN_PATIENT_REPORT_URL,
    "https://office.consultoriomovil.net/office/report/seenPatientReport/ajax",
    "https://office.consultoriomovil.net/office/report/seenPatientReport/list",
    "https://office.consultoriomovil.net/office/report/seenPatientReport/data",
]
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
DAY_NAMES_ES = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
SIMPLE_TIMEZONES: dict[str, tzinfo] = {
    "America/Argentina/Buenos_Aires": timezone(timedelta(hours=-3)),
}
logger = logging.getLogger(__name__)


class CabildoConfigError(RuntimeError):
    pass


class CabildoSlotUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Slot:
    start: datetime
    duration_minutes: int

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class SlotPick:
    index: int
    day: date
    slot: Slot


@dataclass(frozen=True)
class SlotSelection:
    number: int
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    timezone: str
    label: str


@dataclass(frozen=True)
class AttendedConsultation:
    external_id: str
    attended_at: datetime | None
    patient_external_id: str | None
    patient_name: str | None
    patient_document: str | None
    patient_email: str | None
    patient_phone: str | None
    insurance_name: str | None
    professional_name: str | None
    practice_name: str | None
    diagnosis: str | None
    raw_payload: dict[str, Any]


class ConsultorioMovilAccessBlocked(RuntimeError):
    """Raised when Consultorio Movil blocks automated access before login."""


def login(username: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/118.0 Safari/537.36",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://office.consultoriomovil.net/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        }
    )

    logger.info(
        "consultorio_movil_login_start login_url=%s username_present=%s",
        LOGIN_URL,
        bool(username),
        extra={"login_url": LOGIN_URL, "username_present": bool(username)},
    )
    login_page = None
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        login_page = session.get(LOGIN_URL, timeout=30)
        logger.info(
            "consultorio_movil_login_page_response status_code=%s final_url=%s content_type=%s attempt=%s",
            login_page.status_code,
            str(login_page.url),
            login_page.headers.get("content-type", ""),
            attempt,
            extra={
                "status_code": login_page.status_code,
                "final_url": str(login_page.url),
                "content_type": login_page.headers.get("content-type", ""),
                "attempt": attempt,
            },
        )
        if login_page.status_code < 400:
            break
        if login_page.status_code == 403 and attempt < LOGIN_MAX_ATTEMPTS:
            delay = attempt * 2
            logger.warning(
                "consultorio_movil_login_403_retry attempt=%s delay_seconds=%s",
                attempt,
                delay,
                extra={"attempt": attempt, "delay_seconds": delay, "final_url": str(login_page.url)},
            )
            time.sleep(delay)
            continue
        message = f"Consultorio Movil devolvio HTTP {login_page.status_code} al abrir login: {login_page.url}"
        if login_page.status_code == 403:
            raise ConsultorioMovilAccessBlocked(message)
        raise RuntimeError(message)

    payload = {
        "email": username,
        "password": password,
        "auto": 0,
        "remember": 1,
        "X-REQUESTED_WITH": "XMLHttpRequest",
    }
    headers = {
        "Referer": LOGIN_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    resp = session.post(AUTH_URL, data=payload, headers=headers, timeout=30)
    logger.info(
        "consultorio_movil_auth_response status_code=%s final_url=%s content_type=%s",
        resp.status_code,
        str(resp.url),
        resp.headers.get("content-type", ""),
        extra={
            "status_code": resp.status_code,
            "final_url": str(resp.url),
            "content_type": resp.headers.get("content-type", ""),
        },
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Consultorio Movil devolvio HTTP {resp.status_code} al autenticar: {resp.url}")

    data = resp.json()
    success = str(data.get("success")).lower() == "true"
    if not success:
        logger.warning(
            "consultorio_movil_auth_rejected",
            extra={"messages_count": len(data.get("messages") or []), "has_redirect": bool(data.get("redirectTo"))},
        )
        raise RuntimeError("No se pudo iniciar sesion. Verifica credenciales.")

    redirect_url = data.get("redirectTo")
    if redirect_url:
        redirect_response = session.get(requests.compat.urljoin(LOGIN_URL, redirect_url), timeout=30)
        logger.info(
            "consultorio_movil_login_redirect_response status_code=%s final_url=%s",
            redirect_response.status_code,
            str(redirect_response.url),
            extra={"status_code": redirect_response.status_code, "final_url": str(redirect_response.url)},
        )
        if redirect_response.status_code >= 400:
            raise RuntimeError(
                f"Consultorio Movil devolvio HTTP {redirect_response.status_code} al finalizar login: {redirect_response.url}"
            )
    logger.info("consultorio_movil_login_success")
    return session


def _resolve_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        fallback = SIMPLE_TIMEZONES.get(name)
        if fallback:
            return fallback
        return SIMPLE_TIMEZONES["America/Argentina/Buenos_Aires"]


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _now_in_timezone_naive(tz: tzinfo) -> datetime:
    return now_ba().astimezone(tz).replace(tzinfo=None)


def _to_epoch_seconds(dt: datetime, tz: tzinfo) -> int:
    localized = dt.replace(tzinfo=tz)
    return int(localized.timestamp())


def fetch_availability(
    session: requests.Session,
    staff_id: str,
    start_date: datetime,
    days: int,
    appointment_type: str,
    tz: tzinfo,
) -> OrderedDict[date, list[Slot]]:
    start_dt = _start_of_day(start_date)
    end_dt = _start_of_day(start_date + timedelta(days=max(days, 1)))

    params = {
        "staffId": staff_id,
        "dateFrom": _to_epoch_seconds(start_dt, tz),
        "dateTo": _to_epoch_seconds(end_dt, tz) - 1,
        "type": appointment_type,
    }
    resp = session.get(API_AVAILABILITY_URL, params=params, headers={"Accept": "application/json"})
    resp.raise_for_status()

    payload = resp.json()
    items = payload.get("_embedded", {}).get("items", [])
    availability: OrderedDict[date, list[Slot]] = OrderedDict()

    for block in items:
        for day_str in sorted(block.keys()):
            slots = block[day_str]
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
            free_slots: list[Slot] = []
            for hour_str, metadata in sorted(slots.items()):
                if metadata.get("taken"):
                    continue
                hour = datetime.strptime(hour_str, "%H:%M").time()
                start = datetime.combine(day, hour)
                free_slots.append(Slot(start=start, duration_minutes=int(metadata.get("duration", 0) or 0)))
            if free_slots:
                availability.setdefault(day, []).extend(free_slots)

    return availability


def flatten_availability(availability: OrderedDict[date, list[Slot]]) -> list[SlotPick]:
    entries: list[SlotPick] = []
    idx = 1
    for day, slots in availability.items():
        for slot in slots:
            entries.append(SlotPick(index=idx, day=day, slot=slot))
            idx += 1
    return entries


def _slot_timestamp(slot: Slot, tz: tzinfo) -> int:
    localized = slot.start.replace(tzinfo=tz)
    utc_ts = int(localized.astimezone(timezone.utc).timestamp())
    offset_seconds = int(localized.utcoffset().total_seconds()) if localized.utcoffset() else 0
    return utc_ts + offset_seconds


def _sanitize_digits(value: str) -> str:
    return re.sub(r"\D+", "", value)


def _first_present(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return None


def _normalize_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _first_by_normalized_key(data: dict[str, Any], *names: str) -> Any:
    normalized = {_normalize_key(key): value for key, value in data.items()}
    for name in names:
        key = _normalize_key(name)
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    return None


def _parse_consultorio_datetime(value: Any, tz: tzinfo) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        raw = int(value)
        if raw > 10_000_000_000:
            raw = raw // 1000
        return datetime.fromtimestamp(raw, tz=tz).replace(tzinfo=None)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _search_patients(session: requests.Session, query: str) -> list[dict[str, Any]]:
    if not query:
        return []
    resp = session.post(
        "https://office.consultoriomovil.net/office/patient/search",
        data={"search": query},
        headers=XHR_HEADERS,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("content") or []


class _LinkTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_dict = {key: value or "" for key, value in attrs}
        self._current = {
            "href": attrs_dict.get("href", ""),
            "rel": attrs_dict.get("rel", ""),
            "class": attrs_dict.get("class", ""),
            "text_parts": [],
        }

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            cleaned = " ".join(data.split())
            if cleaned:
                self._current["text_parts"].append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current is None:
            return
        text = " ".join(self._current.pop("text_parts", [])).strip()
        if self._current.get("href"):
            self._current["text"] = text
            self.links.append(self._current)
        self._current = None


class _FormFieldParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.label_for: dict[str, str] = {}
        self.inputs: list[dict[str, str]] = []
        self.text_chunks: list[str] = []
        self._current_label_for: str | None = None
        self._current_label_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "label":
            self._current_label_for = attrs_dict.get("for", "")
            self._current_label_parts = []
        elif tag in {"input", "textarea", "select"}:
            self.inputs.append(
                {
                    "id": attrs_dict.get("id", ""),
                    "name": attrs_dict.get("name", ""),
                    "placeholder": attrs_dict.get("placeholder", ""),
                    "value": attrs_dict.get("value", ""),
                }
            )

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if cleaned:
            self.text_chunks.append(cleaned)
        if self._current_label_for is not None and cleaned:
            self._current_label_parts.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._current_label_for is not None:
            label = " ".join(self._current_label_parts).strip()
            if label and self._current_label_for:
                self.label_for[self._current_label_for] = label
            self._current_label_for = None
            self._current_label_parts = []


def _extract_links(html: str) -> list[dict[str, Any]]:
    parser = _LinkTextParser()
    parser.feed(html)
    return parser.links


def _admin_patient_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for link in _extract_links(html):
        text_key = _normalize_key(str(link.get("text") or ""))
        href = str(link.get("href") or "").strip()
        href_key = _normalize_key(href)
        looks_like_admin_href = (
            "administrativa" in href_key
            or "administrative" in href_key
            or "admin" in href_key
            or "ficha" in href_key
        )
        if "ver_ficha_administrativa" not in text_key and not looks_like_admin_href:
            continue
        if href and href != "#":
            links.append(requests.compat.urljoin(base_url, href))
    return links


def _next_patient_page(html: str, base_url: str) -> str | None:
    for link in _extract_links(html):
        text_key = _normalize_key(str(link.get("text") or ""))
        rel_key = _normalize_key(str(link.get("rel") or ""))
        class_key = _normalize_key(str(link.get("class") or ""))
        if rel_key == "next" or text_key in {"siguiente", "next"} or "pagination_next" in class_key:
            href = str(link.get("href") or "").strip()
            if href and href != "#":
                return requests.compat.urljoin(base_url, href)
    return None


def _extract_admin_detail_fields(html: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    table_parser = _HtmlTableParser()
    table_parser.feed(html)
    for row in table_parser.rows:
        if len(row) >= 2:
            key = row[0].rstrip(":")
            value = " ".join(row[1:]).strip()
            if key and value:
                fields[key] = value

    form_parser = _FormFieldParser()
    form_parser.feed(html)
    for item in form_parser.inputs:
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        label = form_parser.label_for.get(item.get("id") or "") or item.get("placeholder") or item.get("name")
        if label:
            fields[str(label).rstrip(":")] = value

    text = "\n".join(form_parser.text_chunks)
    known_labels = [
        "Apellido",
        "Nombres",
        "Fecha de nacimiento",
        "Tipo de documento",
        "Número de documento",
        "Numero de documento",
        "Financiador / Seguro",
        "Nro. Afiliado",
        "Email",
        "Celular / Otro",
        "Teléfono de casa",
        "Telefono de casa",
        "Género",
        "Genero",
        "Dirección",
        "Direccion",
        "Número",
        "Numero",
        "Departamento",
        "Piso",
        "Localidad",
        "Código Postal",
        "Codigo Postal",
        "País",
        "Pais",
        "Provincia",
    ]
    for label in known_labels:
        if label in fields:
            continue
        match = re.search(rf"{re.escape(label)}\s*:?\s*([^\n]+)", text, flags=re.IGNORECASE)
        if match:
            fields[label] = match.group(1).strip()
    return fields


def _first_field(fields: dict[str, Any], *names: str) -> str:
    value = _first_by_normalized_key(fields, *names)
    return str(value or "").strip()


def _split_full_name(full_name: str) -> tuple[str, str]:
    text = " ".join((full_name or "").split())
    if not text:
        return "", ""
    if "," in text:
        last, first = text.split(",", 1)
        return last.strip(), first.strip()
    parts = text.split()
    if len(parts) <= 1:
        return "", text
    return parts[0], " ".join(parts[1:])


def _candidate_to_patient_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    document = candidate.get("document") if isinstance(candidate.get("document"), dict) else {}
    raw_name = str(candidate.get("name") or candidate.get("fullName") or "").strip()
    apellido = str(candidate.get("lastName") or candidate.get("last_name") or "").strip()
    nombres = str(candidate.get("firstName") or candidate.get("first_name") or "").strip()
    if raw_name and (not apellido or not nombres):
        split_apellido, split_nombres = _split_full_name(raw_name)
        apellido = apellido or split_apellido
        nombres = nombres or split_nombres
    return {
        "Apellido": apellido,
        "Nombres": nombres,
        "Tipo de documento": str(
            document.get("type")
            or document.get("typeName")
            or candidate.get("documentType")
            or candidate.get("document_type")
            or ""
        ).strip(),
        "Numero de documento": str(
            document.get("number")
            or candidate.get("documentNumber")
            or candidate.get("document_number")
            or candidate.get("dni")
            or ""
        ).strip(),
        "Email": str(candidate.get("email") or candidate.get("mail") or "").strip(),
        "Celular / Otro": str(
            candidate.get("cellPhone")
            or candidate.get("phone")
            or candidate.get("mobile")
            or candidate.get("telephone")
            or ""
        ).strip(),
        "external_patient_id": str(candidate.get("id") or candidate.get("patientId") or "").strip(),
        "_raw_fields": candidate,
    }


def _merge_patient_payloads(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, value in fallback.items():
        if key not in merged or merged.get(key) in (None, ""):
            merged[key] = value
    raw_fields = {}
    if isinstance(fallback.get("_raw_fields"), dict):
        raw_fields.update(fallback["_raw_fields"])
    if isinstance(primary.get("_raw_fields"), dict):
        raw_fields.update(primary["_raw_fields"])
    if raw_fields:
        merged["_raw_fields"] = raw_fields
    return merged


def _admin_fields_to_patient_payload(fields: dict[str, Any], source_url: str) -> dict[str, Any]:
    apellido = _first_field(fields, "Apellido")
    nombres = _first_field(fields, "Nombres", "Nombre")
    if not apellido or not nombres:
        full_name = _first_field(fields, "Paciente", "Nombre completo", "Apellido y nombre")
        split_apellido, split_nombres = _split_full_name(full_name)
        apellido = apellido or split_apellido
        nombres = nombres or split_nombres
    payload = {
        "Apellido": apellido,
        "Nombres": nombres,
        "Fecha de nacimiento": _first_field(fields, "Fecha de nacimiento", "Nacimiento"),
        "Tipo de documento": _first_field(fields, "Tipo de documento", "Tipo documento", "Documento tipo"),
        "Número de documento": _first_field(fields, "Número de documento", "Numero de documento", "Documento", "Nro documento"),
        "Financiador / Seguro": _first_field(fields, "Financiador / Seguro", "Financiador", "Seguro", "Obra social"),
        "Nro. Afiliado": _first_field(fields, "Nro. Afiliado", "Numero de afiliado", "Nro afiliado", "Afiliado"),
        "Email": _first_field(fields, "Email", "Correo"),
        "Celular / Otro": _first_field(fields, "Celular / Otro", "Celular", "Telefono celular", "Teléfono celular"),
        "Teléfono de casa": _first_field(fields, "Teléfono de casa", "Telefono de casa", "Telefono fijo"),
        "Género": _first_field(fields, "Género", "Genero", "Sexo"),
        "Dirección": _first_field(fields, "Dirección", "Direccion", "Domicilio"),
        "Número": _first_field(fields, "Número", "Numero", "Altura"),
        "Departamento": _first_field(fields, "Departamento", "Depto"),
        "Piso": _first_field(fields, "Piso"),
        "Localidad": _first_field(fields, "Localidad"),
        "Código Postal": _first_field(fields, "Código Postal", "Codigo Postal", "CP"),
        "País": _first_field(fields, "País", "Pais"),
        "Provincia": _first_field(fields, "Provincia"),
        "external_patient_id": _first_field(fields, "ID", "Id paciente", "Paciente ID"),
        "_source_url": source_url,
        "_raw_fields": fields,
    }
    return payload


def _candidate_detail_urls(candidate: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text == "#":
            return
        url = requests.compat.urljoin(LOGIN_URL, text)
        if url not in urls:
            urls.append(url)

    for key in (
        "url",
        "href",
        "link",
        "adminUrl",
        "admin_url",
        "administrativeUrl",
        "administrative_url",
        "fichaAdministrativaUrl",
        "ficha_administrativa_url",
    ):
        add(candidate.get(key))

    links = candidate.get("links") or candidate.get("_links")
    if isinstance(links, dict):
        for value in links.values():
            if isinstance(value, dict):
                add(value.get("href") or value.get("url"))
            else:
                add(value)
    elif isinstance(links, list):
        for value in links:
            if isinstance(value, dict):
                add(value.get("href") or value.get("url"))
            else:
                add(value)

    patient_id = str(candidate.get("id") or candidate.get("patientId") or candidate.get("patient_id") or "").strip()
    if patient_id:
        for path in (
            f"patient/{patient_id}/admin",
            f"patient/{patient_id}/administrative",
            f"patient/{patient_id}/administrativeFile",
            f"patient/administrative/{patient_id}",
            f"patient/administrativeFile/{patient_id}",
            f"patient/show/{patient_id}",
            f"patient/edit/{patient_id}",
            f"patient/{patient_id}",
        ):
            add(path)
    return urls


def _fetch_candidate_admin_payload(
    session: requests.Session,
    candidate: dict[str, Any],
    document_number: str,
) -> dict[str, Any] | None:
    doc = _sanitize_digits(document_number)
    fallback = _candidate_to_patient_payload(candidate)
    for url in _candidate_detail_urls(candidate):
        try:
            response = session.get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=30)
            logger.info(
                "consultorio_movil_patient_admin_candidate_response url=%s status_code=%s",
                str(response.url),
                response.status_code,
                extra={"url": str(response.url), "status_code": response.status_code},
            )
            if response.status_code >= 400:
                continue
            fields = _extract_admin_detail_fields(response.text or "")
            payload = _merge_patient_payloads(_admin_fields_to_patient_payload(fields, str(response.url)), fallback)
            parsed_doc = _sanitize_digits(
                str(
                    payload.get("Numero de documento")
                    or payload.get("NÃºmero de documento")
                    or payload.get("Número de documento")
                    or ""
                )
            )
            logger.info(
                "consultorio_movil_patient_admin_candidate_parsed url=%s field_count=%s document_match=%s",
                str(response.url),
                len(fields),
                bool(parsed_doc and parsed_doc == doc),
                extra={"url": str(response.url), "field_count": len(fields), "document_match": bool(parsed_doc and parsed_doc == doc)},
            )
            if fields and (not parsed_doc or parsed_doc == doc):
                return payload
        except requests.RequestException:
            logger.warning("consultorio_movil_patient_admin_candidate_failed url=%s", url, exc_info=True)
    return None


def fetch_patient_by_document(session: requests.Session, document_number: str | None) -> dict[str, Any] | None:
    doc = _sanitize_digits(document_number or "")
    if not doc:
        return None
    candidate = find_patient_by_document(session, doc)
    if candidate is None:
        logger.info("consultorio_movil_patient_search_no_match document_last4=%s", doc[-4:])
        return None
    payload = _candidate_to_patient_payload(candidate)
    detail_payload = _fetch_candidate_admin_payload(session, candidate, doc)
    if detail_payload is not None:
        payload = _merge_patient_payloads(detail_payload, payload)
    logger.info(
        "consultorio_movil_patient_search_match document_last4=%s external_patient_id=%s has_admin_detail=%s",
        doc[-4:],
        payload.get("external_patient_id") or "",
        bool(detail_payload),
        extra={
            "document_last4": doc[-4:],
            "external_patient_id": payload.get("external_patient_id") or "",
            "has_admin_detail": bool(detail_payload),
        },
    )
    return payload


def fetch_all_patients(session: requests.Session) -> list[dict[str, Any]]:
    logger.info("consultorio_movil_patients_scrape_start", extra={"url": PATIENT_LIST_URL})
    patients: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    seen_detail_urls: set[str] = set()
    page_url: str | None = PATIENT_LIST_URL

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        response = session.get(page_url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=30)
        logger.info(
            "consultorio_movil_patients_list_response",
            extra={"status_code": response.status_code, "url": str(response.url)},
        )
        response.raise_for_status()
        links = _admin_patient_links(response.text or "", str(response.url))
        logger.info(
            "consultorio_movil_patients_admin_links_found url=%s links_count=%s",
            str(response.url),
            len(links),
            extra={"url": str(response.url), "links_count": len(links)},
        )
        for detail_url in links:
            if detail_url in seen_detail_urls:
                continue
            seen_detail_urls.add(detail_url)
            try:
                detail_response = session.get(detail_url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=30)
                logger.info(
                    "consultorio_movil_patient_detail_response",
                    extra={"status_code": detail_response.status_code, "url": str(detail_response.url)},
                )
                detail_response.raise_for_status()
                fields = _extract_admin_detail_fields(detail_response.text or "")
                patients.append(_admin_fields_to_patient_payload(fields, str(detail_response.url)))
                logger.info(
                    "consultorio_movil_patient_detail_parsed url=%s field_count=%s",
                    str(detail_response.url),
                    len(fields),
                    extra={"url": str(detail_response.url), "field_count": len(fields)},
                )
            except requests.RequestException:
                logger.exception("consultorio_movil_patient_detail_failed", extra={"url": detail_url})
        page_url = _next_patient_page(response.text or "", str(response.url))

    logger.info(
        "consultorio_movil_patients_scrape_done pages_count=%s patients_count=%s",
        len(seen_pages),
        len(patients),
        extra={"pages_count": len(seen_pages), "patients_count": len(patients)},
    )
    return patients


class _HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
            self._in_cell = True

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._current_cell is not None:
            cleaned = " ".join(data.split())
            if cleaned:
                self._current_cell.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            self._current_row.append(" ".join(self._current_cell).strip())
            self._current_cell = None
            self._in_cell = False
        elif tag == "tr" and self._current_row is not None:
            if any(cell for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = None


def _extract_json_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("content", "data", "items", "rows", "aaData", "appointments", "patients"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _extract_json_items(value)
            if nested:
                return nested
    embedded = payload.get("_embedded")
    if isinstance(embedded, dict):
        return _extract_json_items(embedded)
    return []


def _extract_html_report_items(html: str) -> list[dict[str, Any]]:
    parser = _HtmlTableParser()
    parser.feed(html)
    rows = parser.rows
    if len(rows) < 2:
        return []
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any(_normalize_key(cell) in {"fecha", "paciente", "dni", "documento"} for cell in row)
        ),
        0,
    )
    headers = [_normalize_key(cell) or f"col_{idx}" for idx, cell in enumerate(rows[header_index])]
    items: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        if len(row) < 2:
            continue
        item = {headers[idx] if idx < len(headers) else f"col_{idx}": cell for idx, cell in enumerate(row)}
        if any(item.values()):
            items.append(item)
    return items


def _response_report_items(response: requests.Response) -> list[Any]:
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        return _extract_json_items(response.json())
    text = response.text or ""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return _extract_json_items(response.json())
        except ValueError:
            pass
    return _extract_html_report_items(text)


def _build_seen_patient_payload(staff_id: str, date_from: date, date_to: date) -> dict[str, str]:
    return {
        "staff_id": staff_id,
        "staff": staff_id,
        "staffId": staff_id,
        "dateFrom": date_from.strftime("%Y-%m-%d"),
        "dateTo": date_to.strftime("%Y-%m-%d"),
        "date_from": date_from.strftime("%Y-%m-%d"),
        "date_to": date_to.strftime("%Y-%m-%d"),
        "from": date_from.strftime("%Y-%m-%d"),
        "to": date_to.strftime("%Y-%m-%d"),
        "start": date_from.strftime("%Y-%m-%d"),
        "end": date_to.strftime("%Y-%m-%d"),
        "X-REQUESTED_WITH": "XMLHttpRequest",
    }


def _attended_from_item(item: Any, tz: tzinfo, fallback_index: int = 0) -> AttendedConsultation | None:
    if isinstance(item, (list, tuple)):
        item = {f"col_{index}": value for index, value in enumerate(item)}
    if not isinstance(item, dict):
        return None
    patient = item.get("patient") if isinstance(item.get("patient"), dict) else {}
    document = patient.get("document") if isinstance(patient.get("document"), dict) else {}
    insurance = item.get("insurance") if isinstance(item.get("insurance"), dict) else {}
    professional = item.get("professional") if isinstance(item.get("professional"), dict) else {}
    practice = item.get("practice") if isinstance(item.get("practice"), dict) else {}
    attended_raw = (
        _first_present(item, "attended_at", "attendedAt", "start", "start_at", "date", "appointmentDate")
        or _first_by_normalized_key(item, "fecha", "fecha_atencion", "fecha_de_atencion", "atendido")
    )
    patient_name = str(
        _first_present(item, "patientName", "patient_name")
        or _first_by_normalized_key(item, "paciente", "nombre_paciente", "apellido_y_nombre")
        or _first_present(patient, "name", "fullName")
        or " ".join(
            part
            for part in [
                str(patient.get("firstName") or "").strip(),
                str(patient.get("lastName") or "").strip(),
            ]
            if part
        )
        or ""
    )
    patient_document = str(
        _first_present(item, "patientDocument", "document")
        or _first_by_normalized_key(item, "dni", "documento", "nro_documento", "numero_documento")
        or _first_present(document, "number")
        or ""
    )
    attended_at = _parse_consultorio_datetime(attended_raw, tz)
    external_id = str(
        _first_present(item, "id", "appointment_id", "appointmentId", "consultation_id")
        or _first_by_normalized_key(item, "id", "turno", "consulta")
        or ""
    ).strip()
    if not external_id:
        basis = f"{attended_raw or ''}|{patient_document}|{patient_name}|{fallback_index}"
        external_id = f"seen-{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:16]}"
    return AttendedConsultation(
        external_id=external_id,
        attended_at=attended_at,
        patient_external_id=str(_first_present(patient, "id", "patient_id", "patientId") or ""),
        patient_name=patient_name,
        patient_document=patient_document,
        patient_email=str(
            _first_present(item, "patientEmail", "email")
            or _first_by_normalized_key(item, "email", "mail", "correo")
            or _first_present(patient, "email", "mail")
            or ""
        ),
        patient_phone=str(
            _first_present(item, "patientPhone")
            or _first_by_normalized_key(item, "telefono", "celular")
            or _first_present(patient, "phone", "cellPhone")
            or ""
        ),
        insurance_name=str(
            _first_present(item, "insuranceName", "obraSocial", "healthInsurance")
            or _first_by_normalized_key(item, "obra_social", "cobertura", "prepaga")
            or _first_present(insurance, "name", "description")
            or ""
        ),
        professional_name=str(
            _first_present(item, "professionalName", "staffName")
            or _first_by_normalized_key(item, "profesional", "medico", "staff")
            or _first_present(professional, "name", "fullName")
            or ""
        ),
        practice_name=str(
            _first_present(item, "practiceName", "subjectName")
            or _first_by_normalized_key(item, "practica", "prestacion", "servicio")
            or _first_present(practice, "name", "description")
            or ""
        ),
        diagnosis=str(
            _first_present(item, "diagnosis", "diagnostico", "notes", "note")
            or _first_by_normalized_key(item, "diagnostico", "motivo", "observaciones")
            or ""
        ),
        raw_payload=dict(item),
    )


def fetch_seen_patient_report(
    session: requests.Session,
    staff_id: str,
    date_from: date,
    date_to: date,
    tz: tzinfo | None = None,
) -> list[AttendedConsultation]:
    tz = tz or SIMPLE_TIMEZONES["America/Argentina/Buenos_Aires"]
    payload = _build_seen_patient_payload(staff_id, date_from, date_to)
    headers = {**XHR_HEADERS, "Accept": "application/json, text/html, */*; q=0.01"}
    seen_items: list[Any] = []
    logger.info(
        "consultorio_movil_seen_report_fetch_start",
        extra={"staff_id": staff_id, "date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
    )
    for url in SEEN_PATIENT_REPORT_AJAX_URLS:
        for method in ("post", "get"):
            if method == "post":
                response = session.post(url, data=payload, headers=headers, timeout=30)
            else:
                response = session.get(url, params=payload, headers=headers, timeout=30)
            logger.info(
                "consultorio_movil_seen_report_response",
                extra={
                    "method": method.upper(),
                    "url": url,
                    "status_code": response.status_code,
                    "final_url": str(response.url),
                    "content_type": response.headers.get("content-type", ""),
                    "bytes": len(response.content or b"") if hasattr(response, "content") else len(response.text or ""),
                },
            )
            if response.status_code in {404, 405}:
                continue
            if response.status_code >= 500:
                logger.warning(
                    "consultorio_movil_seen_report_server_error",
                    extra={
                        "method": method.upper(),
                        "url": url,
                        "status_code": response.status_code,
                        "final_url": str(response.url),
                    },
                )
                continue
            response.raise_for_status()
            items = _response_report_items(response)
            logger.info(
                "consultorio_movil_seen_report_parsed",
                extra={"method": method.upper(), "url": url, "items_count": len(items)},
            )
            if items:
                seen_items = items
                break
        if seen_items:
            break
    consultations: list[AttendedConsultation] = []
    for index, item in enumerate(seen_items):
        consultation = _attended_from_item(item, tz, index)
        if consultation is not None:
            consultations.append(consultation)
    logger.info("consultorio_movil_seen_report_fetch_done", extra={"consultations_count": len(consultations)})
    return consultations


def fetch_attended_consultations(
    session: requests.Session,
    staff_id: str,
    date_from: date,
    date_to: date,
    tz: tzinfo | None = None,
) -> list[AttendedConsultation]:
    tz = tz or SIMPLE_TIMEZONES["America/Argentina/Buenos_Aires"]
    seen_report = fetch_seen_patient_report(session, staff_id, date_from, date_to, tz)
    if seen_report:
        logger.info("consultorio_movil_attended_source_seen_report", extra={"consultations_count": len(seen_report)})
        return seen_report
    logger.info(
        "consultorio_movil_attended_fallback_start",
        extra={"url": APPOINTMENT_LIST_URL, "staff_id": staff_id, "date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
    )
    payload = {
        "staff_id": staff_id,
        "staff": staff_id,
        "dateFrom": date_from.strftime("%Y-%m-%d"),
        "dateTo": date_to.strftime("%Y-%m-%d"),
        "status": "attended",
        "status_id": "attended",
        "X-REQUESTED_WITH": "XMLHttpRequest",
    }
    resp = session.post(
        APPOINTMENT_LIST_URL,
        data=payload,
        headers={**XHR_HEADERS, "Accept": "application/json, text/javascript, */*; q=0.01"},
        timeout=30,
    )
    logger.info(
        "consultorio_movil_attended_fallback_response",
        extra={
            "status_code": resp.status_code,
            "final_url": str(resp.url),
            "content_type": resp.headers.get("content-type", ""),
            "bytes": len(resp.content or b"") if hasattr(resp, "content") else len(resp.text or ""),
        },
    )
    resp.raise_for_status()
    data = resp.json()
    raw_items = data.get("content") or data.get("data") or data.get("items") or []
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items") or raw_items.get("appointments") or []

    consultations: list[AttendedConsultation] = []
    for item in raw_items:
        consultation = _attended_from_item(item, tz, len(consultations))
        if consultation is not None:
            consultations.append(consultation)
    logger.info("consultorio_movil_attended_fallback_done", extra={"consultations_count": len(consultations)})
    return consultations


def find_patient_by_document(session: requests.Session, document_number: str | None) -> dict[str, Any] | None:
    doc = _sanitize_digits(document_number or "")
    if not doc:
        return None
    candidates = _search_patients(session, doc)
    for candidate in candidates:
        doc_info = candidate.get("document") or {}
        cand_doc = _sanitize_digits(doc_info.get("number") or "")
        if cand_doc and cand_doc == doc:
            return candidate
    return None


def find_patient_by_name(
    session: requests.Session,
    first_name: str | None,
    last_name: str | None,
) -> dict[str, Any] | None:
    if not first_name or not last_name:
        return None
    query = f"{first_name.strip()} {last_name.strip()}".strip()
    if not query:
        return None
    candidates = _search_patients(session, query)
    normalized_query = query.lower()
    for candidate in candidates:
        cand_name = (candidate.get("name") or "").strip().lower()
        if cand_name == normalized_query:
            return candidate
    return None


def create_patient(
    session: requests.Session,
    first_name: str,
    last_name: str,
    prefix: str,
    number: str,
    email: str = "",
    internal_id: str = "",
    document_number: str | None = None,
    country: str = "AR",
) -> int:
    payload = {
        "id": "",
        "internalId": internal_id,
        "linkStaff": "0",
        "firstName": first_name,
        "lastName": last_name,
        "email": email,
        "documentType": "11" if document_number else "",
        "documentNumber": document_number or "",
        "noDocument": "false" if document_number else "true",
        "taxId": "",
        "celPhonePrefix": prefix,
        "celPhoneNumber": number,
        "homePhonePrefix": "",
        "homePhoneNumber": "",
        "addressCountry": country,
        "addressState": "",
        "addressCity": "",
        "addressStreet1": "",
        "caregiver": "[]",
    }
    resp = session.post(PATIENT_SAVE_URL, data=payload, headers=XHR_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("success")).lower() != "true":
        messages = data.get("messages") or []
        message = " ".join(messages).strip()
        if not message:
            response_text = resp.text.strip()
            if response_text:
                message = f"No se pudo crear el paciente. Respuesta Cabildo: {response_text}"
            else:
                message = "No se pudo crear el paciente."
        raise RuntimeError(message)
    content = data.get("content") or {}
    return int(content["id"])


def resolve_subject(session: requests.Session, staff_id: str, subject_id: str | None) -> str:
    if subject_id:
        return subject_id
    resp = session.post(APPOINTMENT_PRACTICES_URL, data={"staff_id": staff_id}, headers=XHR_HEADERS)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return ""
    content = data.get("content")
    if isinstance(content, str):
        content = content.strip()
        return content.split(",")[0].strip() if content else ""
    if isinstance(content, dict) and "id" in content:
        return str(content["id"])
    return ""


def book_appointment(
    session: requests.Session,
    staff_id: str,
    slot: Slot,
    patient_id: int,
    subject_id: str,
    appointment_type: str,
    tz: tzinfo,
    indications: str,
    note: str,
    urgent: bool,
    allow_overlap: bool,
) -> dict[str, Any]:
    payload = {
        "id": "",
        "staff": staff_id,
        "subject": subject_id,
        "type": appointment_type,
        "times": slot.start.strftime("%H:%M"),
        "appointmentDuration": str(slot.duration_minutes),
        "patient": str(patient_id),
        "appointment_indications": indications or "",
        "appointment_note": note or "",
        "overlapping": "true" if allow_overlap else "false",
        "urgent": "true" if urgent else "false",
        "appointmentDate": str(_slot_timestamp(slot, tz)),
        "duration": str(slot.duration_minutes),
        "virtual": "true" if appointment_type == "telemed" else "false",
    }
    resp = session.post(APPOINTMENT_SAVE_URL, data=payload, headers=XHR_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("success")).lower() != "true":
        message = " ".join(data.get("messages") or []) or "No se pudo agendar el turno."
        raise RuntimeError(message)
    return data.get("content") or {}


def _split_phone_components(phone_number: str | None) -> tuple[str, str]:
    digits = _sanitize_digits(phone_number or "")
    if len(digits) >= 10:
        prefix = digits[:4]
        number = digits[4:]
    elif len(digits) >= 6:
        prefix = digits[:3]
        number = digits[3:]
    else:
        prefix = digits[:2] or "11"
        number = digits[2:] or (digits or "00000000")
    return prefix, number


def _resolve_cabildo_config(
    consultorio: Consultorio | None,
) -> dict[str, Any]:
    cabildo_cfg = {}
    if consultorio and consultorio.configuracion_externa:
        cabildo_cfg = consultorio.configuracion_externa.get("cabildo") or {}
    return {
        "user": cabildo_cfg.get("user"),
        "password": cabildo_cfg.get("password"),
        "staff_id": cabildo_cfg.get("staff_id"),
        "days": int(cabildo_cfg.get("days") or 21),
        "timezone": cabildo_cfg.get("timezone") or "America/Argentina/Buenos_Aires",
    }


def list_next_presential_slots(
    tenant: Tenant,
    consultorio: Consultorio | None = None,
    limit: int = 5,
) -> list[SlotSelection]:
    cfg = _resolve_cabildo_config(consultorio)
    if not (cfg["user"] and cfg["password"] and cfg["staff_id"]):
        raise CabildoConfigError("Configuracion de Cabildo incompleta.")

    tz = _resolve_timezone(cfg["timezone"])
    session = login(cfg["user"], cfg["password"])
    availability = fetch_availability(
        session=session,
        staff_id=str(cfg["staff_id"]),
        start_date=_now_in_timezone_naive(tz),
        days=int(cfg["days"]),
        appointment_type="presential",
        tz=tz,
    )

    picks = flatten_availability(availability)
    selections: list[SlotSelection] = []
    for pick in picks[:limit]:
        start_at = pick.slot.start.replace(tzinfo=tz)
        end_at = pick.slot.end.replace(tzinfo=tz)
        day_name = DAY_NAMES_ES[pick.day.weekday()]
        label = f"{day_name} {start_at.strftime('%d/%m %H:%M')}"
        selections.append(
            SlotSelection(
                number=pick.index,
                start_at=start_at,
                end_at=end_at,
                duration_minutes=pick.slot.duration_minutes,
                timezone=getattr(tz, "key", "America/Argentina/Buenos_Aires"),
                label=label,
            )
        )
    return selections


def reserve_presential_slot(
    tenant: Tenant,
    consultorio: Consultorio | None,
    selection: SlotSelection,
    paciente: Paciente,
) -> dict[str, Any]:
    cfg = _resolve_cabildo_config(consultorio)
    if not (cfg["user"] and cfg["password"] and cfg["staff_id"]):
        raise CabildoConfigError("Configuracion de Cabildo incompleta.")

    tz = _resolve_timezone(cfg["timezone"])
    session = login(cfg["user"], cfg["password"])

    availability = fetch_availability(
        session=session,
        staff_id=str(cfg["staff_id"]),
        start_date=_now_in_timezone_naive(tz),
        days=int(cfg["days"]),
        appointment_type="presential",
        tz=tz,
    )
    picks = flatten_availability(availability)
    match = None
    for pick in picks:
        start = pick.slot.start.replace(tzinfo=tz)
        if start == selection.start_at and pick.slot.duration_minutes == selection.duration_minutes:
            match = pick
            break
    if not match:
        raise CabildoSlotUnavailable("Slot no disponible.")

    existing = find_patient_by_document(session, paciente.dni)
    if not existing:
        existing = find_patient_by_name(session, paciente.nombre, paciente.apellido)
    if existing:
        cabildo_patient_id = int(existing["id"])
    else:
        prefix, number = _split_phone_components(paciente.telefono)
        email = paciente.email or f"{prefix}{number}@turnos.test"
        cabildo_patient_id = create_patient(
            session=session,
            first_name=paciente.nombre,
            last_name=paciente.apellido,
            prefix=prefix,
            number=number,
            email=email,
            internal_id=str(paciente.id),
            document_number=_sanitize_digits(paciente.dni) if paciente.dni else None,
            country="AR",
        )

    subject_id = resolve_subject(session, str(cfg["staff_id"]), None)
    reservation = book_appointment(
        session=session,
        staff_id=str(cfg["staff_id"]),
        slot=match.slot,
        patient_id=cabildo_patient_id,
        subject_id=subject_id,
        appointment_type="presential",
        tz=tz,
        indications="Reservado via asistente virtual",
        note=f"Reserva WhatsApp paciente_id={paciente.id}",
        urgent=False,
        allow_overlap=False,
    )
    return {
        "cabildo_id": str(reservation.get("id") or ""),
        "start_at": selection.start_at,
        "end_at": selection.end_at,
        "timezone": selection.timezone,
    }


def cancel_presential_slot(
    tenant: Tenant,
    consultorio: Consultorio | None,
    external_event_id: str,
) -> dict[str, Any]:
    _ = tenant
    cfg = _resolve_cabildo_config(consultorio)
    if not (cfg["user"] and cfg["password"] and cfg["staff_id"]):
        raise CabildoConfigError("Configuracion de Cabildo incompleta.")
    appointment_id = str(external_event_id or "").strip()
    if not appointment_id:
        raise CabildoSlotUnavailable("ID externo de turno inexistente.")

    session = login(cfg["user"], cfg["password"])
    payload = {
        "appointment_id": appointment_id,
        "status_id": "cancelled",
        "X-REQUESTED_WITH": "XMLHttpRequest",
    }
    resp = session.post(
        APPOINTMENT_STATUS_URL,
        data=payload,
        headers={**XHR_HEADERS, "Accept": "application/json, text/javascript, */*; q=0.01"},
        timeout=30,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if str(data.get("success", True)).lower() == "false":
        message = " ".join(data.get("messages") or []) or "No se pudo cancelar el turno en Cabildo."
        raise RuntimeError(message)
    return {"event_id": appointment_id, "provider": "consultorio_movil", "status": "cancelled"}


def sync_cabildo_cancel(turno_id: int) -> None:
    raise NotImplementedError("Usar cancel_presential_slot con tenant, consultorio e ID externo.")


def sync_cabildo_update(turno_id: int) -> None:
    raise NotImplementedError
