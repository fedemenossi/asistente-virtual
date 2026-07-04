from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.core.timezone import now_ba

LOGIN_URL = "https://office.consultoriomovil.net/office/"
AUTH_URL = f"{LOGIN_URL}login/authenticate"
API_AVAILABILITY_URL = "https://office.consultoriomovil.net/api/appointment-availability"
PATIENT_SAVE_URL = "https://office.consultoriomovil.net/office/patient/save"
APPOINTMENT_SAVE_URL = "https://office.consultoriomovil.net/office/appointment/appointment/save"
APPOINTMENT_PRACTICES_URL = "https://office.consultoriomovil.net/office/appointment/appointment/loadPractices"
APPOINTMENT_STATUS_URL = "https://office.consultoriomovil.net/office/appointment/list/status"
APPOINTMENT_LIST_URL = "https://office.consultoriomovil.net/office/appointment/list/ajax"
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
DAY_NAMES_ES = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
SIMPLE_TIMEZONES: dict[str, tzinfo] = {
    "America/Argentina/Buenos_Aires": timezone(timedelta(hours=-3)),
}


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
        }
    )

    login_page = session.get(LOGIN_URL, timeout=30)
    login_page.raise_for_status()

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
    resp.raise_for_status()

    data = resp.json()
    success = str(data.get("success")).lower() == "true"
    if not success:
        raise RuntimeError("No se pudo iniciar sesion. Verifica credenciales.")

    redirect_url = data.get("redirectTo")
    if redirect_url:
        session.get(requests.compat.urljoin(LOGIN_URL, redirect_url), timeout=30)
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


def fetch_attended_consultations(
    session: requests.Session,
    staff_id: str,
    date_from: date,
    date_to: date,
    tz: tzinfo | None = None,
) -> list[AttendedConsultation]:
    tz = tz or SIMPLE_TIMEZONES["America/Argentina/Buenos_Aires"]
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
    resp.raise_for_status()
    data = resp.json()
    raw_items = data.get("content") or data.get("data") or data.get("items") or []
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items") or raw_items.get("appointments") or []

    consultations: list[AttendedConsultation] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        external_id = str(_first_present(item, "id", "appointment_id", "appointmentId") or "").strip()
        if not external_id:
            continue
        patient = item.get("patient") if isinstance(item.get("patient"), dict) else {}
        document = patient.get("document") if isinstance(patient.get("document"), dict) else {}
        insurance = item.get("insurance") if isinstance(item.get("insurance"), dict) else {}
        professional = item.get("professional") if isinstance(item.get("professional"), dict) else {}
        practice = item.get("practice") if isinstance(item.get("practice"), dict) else {}
        attended_raw = _first_present(
            item,
            "attended_at",
            "attendedAt",
            "start",
            "start_at",
            "date",
            "appointmentDate",
        )
        consultations.append(
            AttendedConsultation(
                external_id=external_id,
                attended_at=_parse_consultorio_datetime(attended_raw, tz),
                patient_external_id=str(_first_present(patient, "id", "patient_id", "patientId") or ""),
                patient_name=str(
                    _first_present(item, "patientName", "patient_name")
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
                ),
                patient_document=str(
                    _first_present(item, "patientDocument", "document")
                    or _first_present(document, "number")
                    or ""
                ),
                patient_email=str(
                    _first_present(item, "patientEmail", "email")
                    or _first_present(patient, "email", "mail")
                    or ""
                ),
                patient_phone=str(_first_present(item, "patientPhone") or _first_present(patient, "phone", "cellPhone") or ""),
                insurance_name=str(
                    _first_present(item, "insuranceName", "obraSocial", "healthInsurance")
                    or _first_present(insurance, "name", "description")
                    or ""
                ),
                professional_name=str(
                    _first_present(item, "professionalName", "staffName")
                    or _first_present(professional, "name", "fullName")
                    or ""
                ),
                practice_name=str(
                    _first_present(item, "practiceName", "subjectName")
                    or _first_present(practice, "name", "description")
                    or ""
                ),
                diagnosis=str(_first_present(item, "diagnosis", "diagnostico", "notes", "note") or ""),
                raw_payload=item,
            )
        )
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
