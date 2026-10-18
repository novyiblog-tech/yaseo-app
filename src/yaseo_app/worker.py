"""Исполнитель очереди: берёт задачу, гонит конвейер, пишет результат.

    python -m yaseo_app.worker                  # виртуальные источники, бесконечный цикл
    python -m yaseo_app.worker --once           # одна задача и выход
    python -m yaseo_app.worker --sources live   # настоящие поставщики, ключи из .env

YASEO_LIVE_EMAILS=a@b.ru — при --sources fake эти кабинеты работают на живых данных
(только ночью, 00:00–06:00 МСК), остальные остаются на виртуальных.

Ключи читаются из окружения и файла .env и передаются источникам словарём.
В процесс бесплатного аудита они не попадают (free_audit.isolated_env).
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
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


MSK = timezone(timedelta(hours=3))
WINDOW_END_HOUR = 6  # окно отложенных запросов Search API: 00:00–06:00 МСК


def until_window(now: datetime | None = None) -> timedelta:
    """Сколько ждать начала ночного окна; 0 — окно открыто."""
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    if now.hour < WINDOW_END_HOUR:
        return timedelta(0)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - now


def live_emails(raw: str | None = None) -> set[str]:
    raw = os.environ.get("YASEO_LIVE_EMAILS", "") if raw is None else raw
    return {e.strip().lower() for e in raw.replace(";", ",").split(",") if e.strip()}


def run_once(conn: psycopg.Connection, worker_id: str, srcs: dict,
             allow_private: bool = False, live: dict | None = None,
             live_users: set[str] = frozenset()) -> dict | None:
    """Одна задача из очереди. Возвращает задачу или None, если очередь пуста.

    live + live_users: кабинеты из списка получают настоящих поставщиков, но только в
    ночном окне; днём их задача ждёт в очереди до полуночи МСК. Остальные — srcs."""
    job = jobs.claim(conn, worker_id)
    if job is None:
        return None
    user = conn.execute("SELECT * FROM users WHERE id = %s", (job["user_id"],)).fetchone()
    if live and user["email"].lower() in live_users:
        wait = until_window()
        if wait:
            jobs.postpone(conn, job, wait, "живые данные — в ночном окне 00:00–06:00 МСК")
            log.info("задача %s ждёт ночного окна: %s", job["id"], wait)
            return job
        srcs = live
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

    keys = load_keys(args.env_file)
    srcs = sources.build(args.sources, keys)
    users = live_emails()
    live = None
    if users and args.sources == "fake":
        try:
            live = sources.build("live", keys)
        except ValueError as exc:  # без ключей живой режим не включаем, остальных не роняем
            log.error("YASEO_LIVE_EMAILS задан, но живые источники недоступны: %s", exc)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    conn = db.connect()
    db.migrate(conn)
    log.info("исполнитель %s, источники %s", worker_id, args.sources)
    while True:
        job = run_once(conn, worker_id, srcs, args.allow_private, live, users)
        if args.once:
            return 0
        if job is None:
            time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
