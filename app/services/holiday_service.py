from __future__ import annotations

from datetime import date


ARGENTINA_HOLIDAYS: dict[int, set[str]] = {
    2026: {
        "2026-01-01",
        "2026-02-16",
        "2026-02-17",
        "2026-03-24",
        "2026-04-02",
        "2026-04-03",
        "2026-05-01",
        "2026-05-25",
        "2026-06-20",
        "2026-07-09",
        "2026-12-08",
        "2026-12-25",
    }
}


class HolidayService:
    def is_argentina_holiday(self, day: date) -> bool:
        return day.isoformat() in ARGENTINA_HOLIDAYS.get(day.year, set())

    def missing_years(self, start: date, end: date) -> list[int]:
        years = range(start.year, end.year + 1)
        return [year for year in years if year not in ARGENTINA_HOLIDAYS]
