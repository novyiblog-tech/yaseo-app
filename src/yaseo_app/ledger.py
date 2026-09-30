"""Журнал расхода и лимиты.

Каждое обращение к платному источнику — строка в `spend`. Все лимиты считаются из этого
журнала, а не из счётчиков в памяти: сколько потратил пользователь сегодня, сколько сервис,
сколько запросов Wordstat ушло за последний час. Строка пишется до вызова, под блокировкой
на источник, — два исполнителя не проскочат потолок одновременно.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import psycopg

# Сутки для лимитов — московские: пользователь видит сброс в полночь по Москве.
DAY_START = ("(date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
             " AT TIME ZONE 'Europe/Moscow')")


class Blocked(Exception):
    """Раздел отчёта не собирается, отчёт говорит почему. Задача не падает."""


class SourceOff(Blocked):
    pass


class PlanNotAllowed(Blocked):
    pass


class UserLimit(Blocked):
    pass


class ServiceCeiling(Blocked):
    pass


class RateLimited(Exception):
    """Квота поставщика на окно исчерпана: задачу откладываем, а не режем отчёт."""

    def __init__(self, message: str, retry_after: timedelta):
        super().__init__(message)
        self.retry_after = retry_after


class Meter:
    """Учёт расхода одной задачи. fake=True — виртуальные источники, их расход
    в живые лимиты не входит."""

    def __init__(self, conn: psycopg.Connection, user_id: int, plan: str,
                 job_id: int | None = None, fake: bool = False):
        self.conn, self.user_id, self.plan = conn, user_id, plan
        self.job_id, self.fake = job_id, fake

    def source(self, name: str) -> dict:
        row = self.conn.execute("SELECT * FROM sources WHERE name = %s", (name,)).fetchone()
        if row is None:
            raise SourceOff(f"источник «{name}» не заведён")
        return row

    def check(self, name: str) -> dict:
        """Выключатели, которые не зависят от объёма: включён ли, доступен ли в тарифе."""
        src = self.source(name)
        if not src["enabled"]:
            raise SourceOff(f"{src['title']}: источник выключен в админке")
        if src["plans"] and self.plan not in src["plans"]:
            raise PlanNotAllowed(f"{src['title']}: не входит в тариф «{self.plan}»")
        return src

    def reserve(self, name: str, units: int, detail: str = "") -> int:
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext('spend:' || %s))", (name,))
            src = self.check(name)
            price = src["price_rub"]
            cost = price * units

            if src["per_user_daily"] is not None:
                used = self._sum("units", name, user=True, since=DAY_START)
                if used + units > src["per_user_daily"]:
                    raise UserLimit(f"{src['title']}: дневной лимит на пользователя "
                                    f"{src['per_user_daily']} обращений исчерпан")

            if src["service_daily_rub"] is not None:
                spent = self._sum("cost_rub", name, user=False, since=DAY_START)
                if spent + cost > src["service_daily_rub"]:
                    raise ServiceCeiling(f"{src['title']}: дневной потолок расхода сервиса "
                                         f"{src['service_daily_rub']} ₽ достигнут")

            if src["rate_per_hour"] is not None:
                if units > src["rate_per_hour"]:
                    raise Blocked(f"{src['title']}: {units} обращений больше часовой квоты")
                self._check_rate(src, units)

            row = self.conn.execute(
                "INSERT INTO spend (user_id, job_id, source, units, price_rub, cost_rub,"
                " fake, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (self.user_id, self.job_id, name, units, price, cost, self.fake, detail),
            ).fetchone()
            return row["id"]

    def settle(self, spend_id: int, ok: bool, detail: str | None = None) -> None:
        self.conn.execute(
            "UPDATE spend SET ok = %s, detail = coalesce(%s, detail) WHERE id = %s",
            (ok, detail, spend_id),
        )

    def cached(self, name: str, units: int, detail: str = "") -> None:
        """Ответ из кэша: строка с нулевой ценой, в лимиты не входит."""
        src = self.source(name)
        self.conn.execute(
            "INSERT INTO spend (user_id, job_id, source, units, price_rub, cost_rub,"
            " cached, fake, ok, detail) VALUES (%s, %s, %s, %s, %s, 0, true, %s, true, %s)",
            (self.user_id, self.job_id, name, units, src["price_rub"], self.fake, detail),
        )

    def job_summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT source, count(*) FILTER (WHERE NOT cached) AS calls,"
            " coalesce(sum(units) FILTER (WHERE NOT cached), 0) AS units,"
            " count(*) FILTER (WHERE cached) AS cache_hits,"
            " coalesce(sum(units * price_rub) FILTER (WHERE cached), 0) AS saved_rub,"
            " coalesce(sum(cost_rub), 0) AS cost_rub"
            " FROM spend WHERE job_id = %s GROUP BY source ORDER BY source",
            (self.job_id,),
        ).fetchall()
        by_source = {r.pop("source"): {k: _plain(v) for k, v in r.items()} for r in rows}
        total = sum((Decimal(str(v["cost_rub"])) for v in by_source.values()), Decimal(0))
        return {"fake": self.fake, "cost_rub": float(total), "by_source": by_source}

    def _sum(self, column: str, name: str, user: bool, since: str) -> Decimal:
        where = "source = %s AND NOT cached AND fake = %s AND at >= " + since
        args: list = [name, self.fake]
        if user:
            where += " AND user_id = %s"
            args.append(self.user_id)
        row = self.conn.execute(
            f"SELECT coalesce(sum({column}), 0) AS v FROM spend WHERE {where}", args
        ).fetchone()
        return row["v"]

    def _check_rate(self, src: dict, units: int) -> None:
        row = self.conn.execute(
            "SELECT coalesce(sum(units), 0) AS used, min(at) + interval '1 hour' - now() AS wait"
            " FROM spend WHERE source = %s AND NOT cached AND fake = %s"
            " AND at > now() - interval '1 hour'",
            (src["name"], self.fake),
        ).fetchone()
        if row["used"] + units > src["rate_per_hour"]:
            wait = max(row["wait"] or timedelta(0), timedelta(seconds=5))
            raise RateLimited(f"{src['title']}: квота {src['rate_per_hour']} в час исчерпана",
                              retry_after=wait)


def _plain(v):
    return float(v) if isinstance(v, Decimal) else v
