from __future__ import annotations

from fastapi import Request


def add_flash(request: Request, category: str, message: str) -> None:
    flashes = request.session.get("flashes", [])
    flashes.append({"category": category, "message": message})
    request.session["flashes"] = flashes


def pop_flashes(request: Request) -> list[dict]:
    flashes = request.session.pop("flashes", [])
    return flashes
