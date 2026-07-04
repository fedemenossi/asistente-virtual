from __future__ import annotations

from datetime import date

import requests

from app.integrations.consultorio_movil import (
    APPOINTMENT_LIST_URL,
    SEEN_PATIENT_REPORT_URL,
    fetch_attended_consultations,
    fetch_seen_patient_report,
)


class FakeResponse:
    def __init__(
        self,
        *,
        data=None,
        text: str = "",
        status_code: int = 200,
        content_type: str = "application/json",
        url: str = "https://office.consultoriomovil.net/test",
    ) -> None:
        self._data = data
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = url

    def json(self):
        if self._data is None:
            raise ValueError("No JSON payload")
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def post(self, url: str, data=None, headers=None, timeout=None):
        self.calls.append(("post", url, data))
        if not self.responses:
            raise AssertionError("No fake responses left")
        return self.responses.pop(0)

    def get(self, url: str, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params))
        if not self.responses:
            raise AssertionError("No fake responses left")
        return self.responses.pop(0)


def test_fetch_seen_patient_report_maps_json_payload() -> None:
    session = FakeSession(
        [
            FakeResponse(
                data={
                    "data": [
                        {
                            "id": "seen-101",
                            "fecha": "04/07/2026 09:30",
                            "paciente": "Ana Perez",
                            "dni": "30111222",
                            "mail": "ana@example.com",
                            "obra_social": "OSDE",
                            "profesional": "Dra. Gomez",
                            "practica": "Consulta",
                            "diagnostico": "Control clinico",
                        }
                    ]
                },
                url=SEEN_PATIENT_REPORT_URL,
            )
        ]
    )

    consultations = fetch_seen_patient_report(session, "8", date(2026, 7, 1), date(2026, 7, 4))

    assert len(consultations) == 1
    consultation = consultations[0]
    assert consultation.external_id == "seen-101"
    assert consultation.patient_name == "Ana Perez"
    assert consultation.patient_document == "30111222"
    assert consultation.patient_email == "ana@example.com"
    assert consultation.insurance_name == "OSDE"
    assert consultation.professional_name == "Dra. Gomez"
    assert consultation.practice_name == "Consulta"
    assert consultation.diagnosis == "Control clinico"
    assert session.calls[0][0] == "post"
    assert session.calls[0][1] == SEEN_PATIENT_REPORT_URL


def test_fetch_seen_patient_report_maps_html_table() -> None:
    html = """
    <html>
      <body>
        <table>
          <tr>
            <th>Fecha</th><th>Paciente</th><th>DNI</th><th>Email</th>
            <th>Obra Social</th><th>Profesional</th><th>Practica</th><th>Diagnostico</th>
          </tr>
          <tr>
            <td>04/07/2026 10:15</td><td>Juan Lopez</td><td>28999888</td>
            <td>juan@example.com</td><td>Swiss Medical</td><td>Dr. Ruiz</td>
            <td>Guardia</td><td>Faringitis</td>
          </tr>
        </table>
      </body>
    </html>
    """
    session = FakeSession([FakeResponse(text=html, content_type="text/html")])

    consultations = fetch_seen_patient_report(session, "8", date(2026, 7, 1), date(2026, 7, 4))

    assert len(consultations) == 1
    consultation = consultations[0]
    assert consultation.external_id.startswith("seen-")
    assert consultation.patient_name == "Juan Lopez"
    assert consultation.patient_document == "28999888"
    assert consultation.patient_email == "juan@example.com"
    assert consultation.insurance_name == "Swiss Medical"
    assert consultation.professional_name == "Dr. Ruiz"
    assert consultation.practice_name == "Guardia"
    assert consultation.diagnosis == "Faringitis"


def test_fetch_attended_consultations_falls_back_to_appointment_list() -> None:
    empty_seen_responses = [
        FakeResponse(data={"data": []})
        for _ in range(8)
    ]
    session = FakeSession(
        [
            *empty_seen_responses,
            FakeResponse(
                data={
                    "content": [
                        {
                            "id": "appt-1",
                            "attendedAt": "2026-07-04 11:00:00",
                            "patient": {
                                "id": "patient-1",
                                "name": "Maria Garcia",
                                "email": "maria@example.com",
                                "document": {"number": "27123456"},
                            },
                            "insurance": {"name": "Medife"},
                            "professional": {"name": "Dra. Rivas"},
                            "practice": {"name": "Consulta clinica"},
                            "diagnosis": "Lumbalgia",
                        }
                    ]
                },
                url=APPOINTMENT_LIST_URL,
            ),
        ]
    )

    consultations = fetch_attended_consultations(session, "8", date(2026, 7, 1), date(2026, 7, 4))

    assert len(consultations) == 1
    consultation = consultations[0]
    assert consultation.external_id == "appt-1"
    assert consultation.patient_name == "Maria Garcia"
    assert consultation.patient_document == "27123456"
    assert consultation.patient_email == "maria@example.com"
    assert consultation.insurance_name == "Medife"
    assert consultation.diagnosis == "Lumbalgia"
    assert session.calls[-1][1] == APPOINTMENT_LIST_URL
