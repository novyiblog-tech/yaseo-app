"""Планировщик: одна функция tick, которую можно звать хоть раз в минуту.

Всё, что он ставит, защищено ключами от повтора, поэтому лишний проход ничего не
удваивает, а пропущенный догоняется следующим.

- с 03:00 по Москве — съём позиций на сегодня (ночью отложенные запросы дешевле);
- понедельник с 09:00 — письма «что изменилось»;
- каждый проход — отправка писем из очереди и продление подписок.

    python -m yaseo_app.scheduler          # цикл раз в минуту
    python -m yaseo_app.scheduler --once
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime

import psycopg

from yaseo_app import billing, db, digest, mailer, monitor

log = logging.getLogger("yaseo_app.scheduler")

POSITIONS_HOUR = 3
DIGEST_WEEKDAY, DIGEST_HOUR = 0, 9  # понедельник


def msk_now(conn: psycopg.Connection) -> datetime:
    return conn.execute("SELECT now() AT TIME ZONE 'Europe/Moscow' AS t").fetchone()["t"]


def tick(conn: psycopg.Connection, now: datetime | None = None) -> dict:
    now = now or msk_now(conn)
    out: dict = {}
    if now.hour >= POSITIONS_HOUR:
        out["positions"] = monitor.schedule_daily(conn, now.date())
    if now.weekday() == DIGEST_WEEKDAY and now.hour >= DIGEST_HOUR:
        out["digests"] = digest.queue_weekly(conn, now.date())
    out["mail"] = mailer.send_pending(conn)
    out["billing"] = billing.renew_due(conn, billing.provider(conn))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Планировщик yaseo-app")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--every", type=float, default=60.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    conn = db.connect()
    db.migrate(conn)
    while True:
        try:
            out = tick(conn)
            if any(v for k, v in out.items() if k in ("positions", "digests")) \
                    or out["mail"]["sent"] or out["billing"]["renewed"]:
                log.info("%s", out)
        except Exception:  # планировщик не падает от одной ошибки
            log.exception("проход планировщика")
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())
