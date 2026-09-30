"""Исполнитель очереди: берёт задачу, гонит конвейер, пишет результат.

    python -m yaseo_app.worker                  # виртуальные источники, бесконечный цикл
    python -m yaseo_app.worker --once           # одна задача и выход
    python -m yaseo_app.worker --sources live   # настоящие поставщики, ключи из .env

Ключи читаются из окружения и файла .env и передаются источникам словарём.
В процесс бесплатного аудита они не попадают (free_audit.isolated_env).
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from pathlib import Path

import psycopg

from yaseo_app import db, free_audit, history, jobs, monitor, pipeline, sources, watch

log = logging.getLogger("yaseo_app.worker")


def load_keys(env_file: Path | None) -> dict[str, str]:
    keys: dict[str, str] = {}
    if env_file and env_file.exists():
        from yaseo.env import parse_env_file
        keys.update({k: v for k, v in parse_env_file(env_file).items()
                     if k in free_audit.SECRET_ENV})
    keys.update({k: os.environ[k] for k in free_audit.SECRET_ENV if os.environ.get(k)})
    return keys


def run_once(conn: psycopg.Connection, worker_id: str, srcs: dict,
             allow_private: bool = False) -> dict | None:
    """Одна задача из очереди. Возвращает задачу или None, если очередь пуста."""
    job = jobs.claim(conn, worker_id)
    if job is None:
        return None
    user = conn.execute("SELECT * FROM users WHERE id = %s", (job["user_id"],)).fetchone()
    try:
        if job["kind"] == "audit":
            result = pipeline.run_audit(conn, job, user, srcs, allow_private=allow_private)
        elif job["kind"] == "positions":
            result = monitor.run_positions(conn, job, user, srcs)
        elif job["kind"] == "health":
            result = watch.run_health(conn, job, allow_private=allow_private)
        else:
            raise ValueError(f"неизвестный вид задачи «{job['kind']}»")
    except pipeline.Postpone as exc:
        jobs.postpone(conn, job, exc.delay, str(exc))
        log.info("задача %s отложена на %s: %s", job["id"], exc.delay, exc)
        return job
    except Exception as exc:  # задача падает, исполнитель живёт
        status = jobs.fail(conn, job, f"{type(exc).__name__}: {exc}"[:1000])
        log.warning("задача %s: %s → %s", job["id"], exc, status)
        return job
    try:
        summary = history.summarize(result) if job["kind"] == "audit" else None
    except Exception as exc:  # оценка не должна терять готовый результат
        log.warning("задача %s: оценка не посчиталась: %s", job["id"], exc)
        summary = None
    try:
        jobs.finish(conn, job, result, summary)
    except jobs.LeaseLost as exc:
        log.warning("%s", exc)
        return job
    if job["kind"] == "audit":
        try:
            watch.after_audit(conn, job)
        except Exception as exc:  # письмо не должно ронять исполнителя
            log.warning("задача %s: письмо об автопроверке: %s", job["id"], exc)
    return job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Исполнитель очереди yaseo-app")
    parser.add_argument("--once", action="store_true", help="одна задача и выход")
    parser.add_argument("--sources", default=os.environ.get("YASEO_SOURCES", "fake"),
                        choices=("fake", "live"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--allow-private", action="store_true",
                        help="пускать на локальные адреса — только для тестового сайта")
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    srcs = sources.build(args.sources, load_keys(args.env_file))
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    conn = db.connect()
    db.migrate(conn)
    log.info("исполнитель %s, источники %s", worker_id, args.sources)
    while True:
        job = run_once(conn, worker_id, srcs, args.allow_private)
        if args.once:
            return 0
        if job is None:
            time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
