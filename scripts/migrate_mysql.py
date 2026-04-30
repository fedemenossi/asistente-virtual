from __future__ import annotations

import argparse
from typing import Iterable, Sequence

import pymysql
from pymysql.cursors import SSCursor
from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migra tablas y datos de una base MySQL origen a otra destino."
    )
    parser.add_argument("--source-url", required=True, help="URL MySQL origen")
    parser.add_argument("--target-url", required=True, help="URL MySQL destino")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Cantidad de filas a insertar por lote",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Elimina tablas existentes en destino antes de crearlas",
    )
    return parser.parse_args()


def normalize_mysql_url(raw_url: str) -> URL:
    if raw_url.startswith("mysql://"):
        raw_url = raw_url.replace("mysql://", "mysql+pymysql://", 1)
    url = make_url(raw_url)
    if not url.drivername.startswith("mysql"):
        raise ValueError("Solo se aceptan URLs MySQL")
    if not url.database:
        raise ValueError("La URL debe incluir el nombre de la base de datos")
    return url


def connect(url: URL, *, server_side: bool = False):
    cursorclass = SSCursor if server_side else pymysql.cursors.Cursor
    return pymysql.connect(
        host=url.host,
        port=url.port or 3306,
        user=url.username,
        password=url.password,
        database=url.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=cursorclass,
    )


def fetch_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        return [row[0] for row in cur.fetchall()]


def fetch_create_table(conn, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(f"SHOW CREATE TABLE `{table}`")
        row = cur.fetchone()
        return row[1]


def fetch_columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM `{table}`")
        return [row[0] for row in cur.fetchall()]


def quote_ident(name: str) -> str:
    return f"`{name.replace('`', '``')}`"


def build_insert_sql(table: str, columns: Sequence[str]) -> str:
    quoted_columns = ", ".join(quote_ident(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO {quote_ident(table)} ({quoted_columns}) VALUES ({placeholders})"


def stream_rows(conn, table: str) -> Iterable[tuple]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {quote_ident(table)}")
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                yield row


def chunked(rows: Iterable[tuple], size: int) -> Iterable[list[tuple]]:
    chunk: list[tuple] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def migrate(source_url: str, target_url: str, *, batch_size: int, drop_existing: bool) -> None:
    source = normalize_mysql_url(source_url)
    target = normalize_mysql_url(target_url)

    source_conn = connect(source, server_side=True)
    target_conn = connect(target)
    try:
        source_tables = fetch_tables(source_conn)
        target_tables = set(fetch_tables(target_conn))

        with target_conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")

        try:
            for table in source_tables:
                print(f"[schema] {table}")
                create_stmt = fetch_create_table(source_conn, table)
                columns = fetch_columns(source_conn, table)
                insert_sql = build_insert_sql(table, columns)

                with target_conn.cursor() as cur:
                    if table in target_tables and drop_existing:
                        cur.execute(f"DROP TABLE {quote_ident(table)}")
                    cur.execute(create_stmt)
                target_conn.commit()

                inserted = 0
                for batch in chunked(stream_rows(source_conn, table), batch_size):
                    with target_conn.cursor() as cur:
                        cur.executemany(insert_sql, batch)
                    target_conn.commit()
                    inserted += len(batch)
                    print(f"[data] {table}: {inserted}")

            print("Migracion completada")
        finally:
            with target_conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            target_conn.commit()
    finally:
        source_conn.close()
        target_conn.close()


if __name__ == "__main__":
    args = parse_args()
    migrate(
        args.source_url,
        args.target_url,
        batch_size=args.batch_size,
        drop_existing=args.drop_existing,
    )
