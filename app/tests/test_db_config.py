from __future__ import annotations

from aiomysql.connection import Connection

from app.core.db import _engine_options


def test_aiomysql_disables_pool_pre_ping() -> None:
    options = _engine_options("mysql+aiomysql://user:pass@host:3306/db")

    assert options["pool_pre_ping"] is False
    assert options["pool_recycle"] == 1800


def test_non_aiomysql_keeps_pool_pre_ping() -> None:
    options = _engine_options("sqlite+aiosqlite:///test.db")

    assert options == {"pool_pre_ping": True}


def test_aiomysql_can_escape_binary_database_values() -> None:
    connection = object.__new__(Connection)
    connection._writer = None

    assert connection.escape(b"\x00\xff") == "_binary X'00ff'"
