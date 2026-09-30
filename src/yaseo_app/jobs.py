"""Очередь фоновых задач на Postgres, без отдельного брокера.

Задачу берут через FOR UPDATE SKIP LOCKED: два исполнителя одну задачу не получат.
Исполнитель держит аренду; упал — аренда истекла, задачу подберёт другой.
"""
from __future__ import annotations

from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

LEASE = timedelta(minutes=20)
BACKOFF = timedelta(seconds=30)


class LeaseLost(Exception):
    """Аренда истекла, и задачу уже взял другой исполнитель: результат не пишем."""


def enqueue(conn: psycopg.Connection, user_id: int, kind: str, params: dict,
            site_id: int | None = None, max_attempts: int = 3,
            dedupe_key: str | None = None) -> int | None:
    """Номер задачи. С dedupe_key повторная постановка вернёт None и ничего не создаст."""
    row = conn.execute(
        "INSERT INTO jobs (user_id, site_id, kind, params, max_attempts, dedupe_key)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING RETURNING id",
        (user_id, site_id, kind, Jsonb(params), max_attempts, dedupe_key),
    ).fetchone()
    return row["id"] if row else None


def _reap(conn: psycopg.Connection) -> None:
    """Брошенные задачи, у которых кончились попытки, закрываются ошибкой."""
    conn.execute(
        "UPDATE jobs SET status = 'failed', finished_at = now(), locked_by = NULL,"
        " error = coalesce(error || '; ', '') || 'исполнитель пропал, попытки кончились'"
        " WHERE status = 'running' AND locked_until < now() AND attempts >= max_attempts"
    )


def claim(conn: psycopg.Connection, worker: str, lease: timedelta = LEASE) -> dict | None:
    _reap(conn)
    return conn.execute(
        """
        UPDATE jobs SET status = 'running', attempts = attempts + 1,
               locked_by = %(w)s, locked_until = now() + %(lease)s,
               started_at = coalesce(started_at, now())
        WHERE id = (
            SELECT id FROM jobs
            WHERE (status = 'queued' AND run_after <= now())
               OR (status = 'running' AND locked_until < now())
            ORDER BY run_after, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """,
        {"w": worker, "lease": lease},
    ).fetchone()


def _mine(cur: psycopg.Cursor, job_id: int) -> None:
    if cur.rowcount != 1:
        raise LeaseLost(f"задача {job_id} больше не за этим исполнителем")


def finish(conn: psycopg.Connection, job: dict, result: dict,
           summary: dict | None = None) -> None:
    summary = summary or {}
    cur = conn.execute(
        "UPDATE jobs SET status = 'done', result = %s, error = NULL, finished_at = now(),"
        " locked_by = NULL, locked_until = NULL, score = %s, lights = %s"
        " WHERE id = %s AND locked_by = %s AND status = 'running'",
        (Jsonb(result), summary.get("score"), Jsonb(summary.get("lights")),
         job["id"], job["locked_by"]),
    )
    _mine(cur, job["id"])


def fail(conn: psycopg.Connection, job: dict, error: str) -> str:
    """Возвращает новый статус: queued — будет повтор с растущей паузой, failed — всё."""
    row = conn.execute(
        """
        UPDATE jobs SET
            status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
            run_after = now() + %(backoff)s * power(2, attempts - 1),
            finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
            error = %(error)s, locked_by = NULL, locked_until = NULL
        WHERE id = %(id)s AND locked_by = %(w)s AND status = 'running'
        RETURNING status
        """,
        {"backoff": BACKOFF, "error": error, "id": job["id"], "w": job["locked_by"]},
    ).fetchone()
    if row is None:
        raise LeaseLost(f"задача {job['id']} больше не за этим исполнителем")
    return row["status"]


def postpone(conn: psycopg.Connection, job: dict, delay: timedelta, reason: str) -> None:
    """Отложить без траты попытки: упёрлись в квоту поставщика, а не сломались."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', attempts = attempts - 1,"
        " run_after = now() + %s, error = %s, locked_by = NULL, locked_until = NULL"
        " WHERE id = %s AND locked_by = %s AND status = 'running'",
        (delay, reason, job["id"], job["locked_by"]),
    )
    _mine(cur, job["id"])
