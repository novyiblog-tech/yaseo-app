"""Подключение к Postgres и накат схемы."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "dbname=yaseo_app"
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def dsn() -> str:
    return os.environ.get("YASEO_APP_DSN", DEFAULT_DSN)


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Соединение в autocommit: транзакции открываются явно там, где нужны."""
    return psycopg.connect(conninfo or dsn(), autocommit=True, row_factory=dict_row)


def migrate(conn: psycopg.Connection) -> None:
    with conn.transaction():
        # Два исполнителя, стартующих разом, не должны накатывать схему одновременно.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('yaseo_app.schema'))")
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
