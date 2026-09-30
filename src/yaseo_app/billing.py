"""Тарифы, лимиты, оплата и продление.

Платёжный сервис — за интерфейсом Provider. Сейчас есть только виртуальный (FakeProvider):
на нём проходят покупка, отказ, продление и неудачное списание без денег и без договора.
Настоящий (ЮKassa или CloudPayments) подключается тем же интерфейсом, когда будут ИП,
магазин и ключи.

Правила:
- платёж сначала создаётся у нас, потом у сервиса; ключ идемпотентности — наш;
- статус платежа меняется только после запроса к сервису: телу уведомления не верим;
- повторное уведомление о том же платеже тариф второй раз не продлевает.

    python -m yaseo_app.billing renew    # продлить подписки, у которых кончился период
"""
from __future__ import annotations

import argparse
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import psycopg

from yaseo_app import db, legal
from yaseo_app.report import plural
from yaseo_app.accounts import Refused

PERIOD = timedelta(days=30)
# Не списалось — даём три дня и три попытки, потом тариф падает до бесплатного.
GRACE = timedelta(days=3)
MAX_RENEW_ATTEMPTS = 3
RETRY_EVERY = timedelta(days=1)


# --- тариф и лимиты ---------------------------------------------------------------

def plans(conn: psycopg.Connection, public_only: bool = True) -> list[dict]:
    where = "WHERE public" if public_only else ""
    return conn.execute(f"SELECT * FROM plans {where} ORDER BY sort").fetchall()


def plan(conn: psycopg.Connection, code: str) -> dict:
    row = conn.execute("SELECT * FROM plans WHERE code = %s", (code,)).fetchone()
    if row is None:
        raise Refused("Такого тарифа нет.")
    return row


def subscription(conn: psycopg.Connection, user_id: int) -> dict | None:
    return conn.execute("SELECT * FROM subscriptions WHERE user_id = %s",
                        (user_id,)).fetchone()


def current(conn: psycopg.Connection, user: dict) -> dict:
    """Действующий тариф, границы периода и подписка. Истёкшая подписка — это free."""
    sub = subscription(conn, user["id"])
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    live = sub and sub["status"] != "canceled" and (
        sub["period_end"] > now
        or (sub["status"] == "past_due" and sub["period_end"] + GRACE > now))
    if live:
        p = plan(conn, sub["plan"])
        # Бесплатные по сути тарифы (бета) считают проверки в скользящем окне, как free.
        start = now - timedelta(days=p["free_every_days"] or 30) if p["period"] == "free" \
            else sub["period_start"]
        return {"plan": p, "sub": sub, "start": start, "end": sub["period_end"]}
    free = plan(conn, "free")
    days = timedelta(days=free["free_every_days"] or 30)
    return {"plan": free, "sub": sub, "start": now - days, "end": None}


def usage(conn: psycopg.Connection, user: dict, cur: dict | None = None) -> dict:
    """Сколько выбрано в текущем периоде. Проверки в нейросетях считаются по журналу
    расхода — то есть ровно то, за что сервис заплатил."""
    cur = cur or current(conn, user)
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM sites WHERE user_id = %(u)s) AS sites,
          (SELECT count(*) FROM jobs WHERE user_id = %(u)s AND kind = 'audit'
             AND status <> 'failed' AND created_at >= %(s)s) AS audits,
          (SELECT count(*) FROM spend WHERE user_id = %(u)s AND source = 'yandex-gen'
             AND NOT cached AND at >= %(s)s) AS ai_checks
        """,
        {"u": user["id"], "s": cur["start"]},
    ).fetchone()
    return dict(row)


def _left(limit, used) -> int | None:
    return None if limit is None else max(0, limit - used)


def check_site_slot(conn: psycopg.Connection, user: dict) -> None:
    cur = current(conn, user)
    left = _left(cur["plan"]["sites"], usage(conn, user, cur)["sites"])
    if left == 0:
        raise Refused(f"В тарифе «{cur['plan']['title']}» сайтов не больше "
                      f"{cur['plan']['sites']}. Перейдите на тариф выше.")


def audit_allowance(conn: psycopg.Connection, user: dict, site_url: str | None = None) -> dict:
    """Лимиты, с которыми ставится проверка. Нет попыток — отказ с понятной причиной."""
    cur = current(conn, user)
    p, used = cur["plan"], usage(conn, user, cur)
    if p["code"] == "free" and site_url:
        # Бесплатная проверка — одна на домен на все кабинеты: десять регистраций
        # не дают десять бесплатных проверок одного сайта.
        from yaseo_app.sources import host
        taken = conn.execute(
            "SELECT 1 FROM jobs WHERE plan = 'free' AND kind = 'audit' AND status <> 'failed'"
            " AND created_at > now() - make_interval(days => %s) AND user_id <> %s"
            " AND lower(regexp_replace(params->>'url', '^https?://(www\\.)?([^/:]+).*$', '\\2')) = %s"
            " LIMIT 1", (p["free_every_days"] or 30, user["id"], host(site_url))).fetchone()
        if taken:
            raise Refused("Бесплатная проверка этого сайта уже была в последние "
                          f"{p['free_every_days']} дней. Полная проверка — на платном тарифе.")
    if _left(p["audits_per_period"], used["audits"]) == 0:
        if p["period"] == "free":
            n = p["audits_per_period"]
            raise Refused((f"Бесплатная проверка — раз в {p['free_every_days']} дней. " if n == 1
                           else f"По тарифу «{p['title']}» — {n} {plural(n, 'проверка', 'проверки', 'проверок')} за "
                                f"{p['free_every_days']} дней, они закончились. ")
                          + "Следующую можно раньше на платном тарифе.")
        raise Refused(f"Проверки по тарифу «{p['title']}» на этот период закончились.")
    return {"plan": p["code"],
            "max_pages": p.get("max_pages"),
            "queries": p["queries_per_audit"],
            "answers": _left(p["ai_checks"], used["ai_checks"])}


# --- платёжные сервисы ------------------------------------------------------------

class Provider:
    name = ""

    def create(self, payment: dict, return_url: str) -> tuple[str, str]:
        """Платёж у сервиса. Возвращает его номер и адрес страницы оплаты."""
        raise NotImplementedError

    def fetch(self, provider_id: str) -> dict:
        """Статус у сервиса: {"status": pending|succeeded|canceled, "method": str|None}."""
        raise NotImplementedError

    def charge(self, payment: dict, method: str) -> tuple[str, dict]:
        """Списание сохранённым способом без участия человека (продление)."""
        raise NotImplementedError


class FakeProvider(Provider):
    """Виртуальный платёжный сервис: своя таблица вместо чужого сервера.
    Страница оплаты — /pay/fake/{id} в самом кабинете, с кнопками «оплатить» и «отказаться».
    Способ оплаты, который начинается с 'decline', при продлении не списывается."""
    name = "fake"

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        conn.execute("CREATE TABLE IF NOT EXISTS fake_provider (id text PRIMARY KEY,"
                     " amount numeric(12, 2) NOT NULL, status text NOT NULL,"
                     " method text, return_url text)")

    def create(self, payment, return_url):
        pid = "fake_" + secrets.token_hex(8)
        self.conn.execute("INSERT INTO fake_provider (id, amount, status, return_url)"
                          " VALUES (%s, %s, 'pending', %s)",
                          (pid, payment["amount_rub"], return_url))
        return pid, f"/pay/fake/{pid}"

    def fetch(self, provider_id):
        row = self.conn.execute("SELECT * FROM fake_provider WHERE id = %s",
                                (provider_id,)).fetchone()
        if row is None:
            raise LookupError(f"платёж {provider_id} у сервиса не найден")
        return {"status": row["status"], "method": row["method"]}

    def charge(self, payment, method):
        pid = "fake_" + secrets.token_hex(8)
        status = "canceled" if (method or "").startswith("decline") else "succeeded"
        self.conn.execute("INSERT INTO fake_provider (id, amount, status, method)"
                          " VALUES (%s, %s, %s, %s)",
                          (pid, payment["amount_rub"], status, method))
        return pid, {"status": status, "method": method}

    # то, что на настоящем сервисе делает человек на странице оплаты
    def page(self, provider_id: str) -> dict | None:
        return self.conn.execute("SELECT * FROM fake_provider WHERE id = %s",
                                 (provider_id,)).fetchone()

    def decide(self, provider_id: str, pay: bool, method: str = "card_4242") -> None:
        self.conn.execute(
            "UPDATE fake_provider SET status = %s, method = %s"
            " WHERE id = %s AND status = 'pending'",
            ("succeeded" if pay else "canceled", method if pay else None, provider_id))


def provider(conn: psycopg.Connection) -> Provider:
    name = os.environ.get("YASEO_PAYMENTS", "fake")
    if name == "fake":
        return FakeProvider(conn)
    if not legal.complete():
        # Настоящие деньги — только когда в оферте есть реквизиты исполнителя.
        raise ValueError("реквизиты оферты не заполнены: "
                         + ", ".join(legal.requisites()["missing"]))
    raise ValueError(f"платёжный сервис «{name}» не подключён: нужны ИП, магазин и ключи")


# --- покупка ------------------------------------------------------------------------

def start_purchase(conn: psycopg.Connection, user: dict, plan_code: str, prov: Provider,
                   return_url: str) -> str:
    from yaseo_app import verify
    verify.require_confirmed(user)
    p = plan(conn, plan_code)
    if p["period"] == "free" or not p["public"]:
        raise Refused("Этот тариф не покупается.")
    cur = current(conn, user)
    if cur["plan"]["code"] == p["code"] and p["period"] == "month":
        raise Refused("Этот тариф у вас уже действует.")
    pay = conn.execute(
        "INSERT INTO payments (user_id, plan, amount_rub, purpose, provider, idempotence_key,"
        " offer_version) VALUES (%s, %s, %s, 'purchase', %s, %s, %s) RETURNING *",
        (user["id"], p["code"], p["price_rub"], prov.name, secrets.token_hex(16),
         legal.OFFER_VERSION),
    ).fetchone()
    pid, url = prov.create(pay, return_url.replace("{payment}", str(pay["id"])))
    conn.execute("UPDATE payments SET provider_id = %s WHERE id = %s", (pid, pay["id"]))
    return url


def confirm(conn: psycopg.Connection, prov: Provider, provider_id: str) -> dict | None:
    """Сверить платёж с сервисом и, если оплачен, включить тариф. Повтор безопасен."""
    with conn.transaction():
        pay = conn.execute("SELECT * FROM payments WHERE provider_id = %s AND provider = %s"
                           " FOR UPDATE", (provider_id, prov.name)).fetchone()
        if pay is None:
            return None
        if pay["status"] != "pending":
            return pay
        state = prov.fetch(provider_id)
        if state["status"] == "succeeded":
            _activate(conn, pay["user_id"], plan(conn, pay["plan"]), state.get("method"))
        if state["status"] != "pending":
            pay = conn.execute(
                "UPDATE payments SET status = %s, paid_at = CASE WHEN %s = 'succeeded'"
                " THEN now() END WHERE id = %s RETURNING *",
                (state["status"], state["status"], pay["id"]),
            ).fetchone()
        return pay


def _activate(conn: psycopg.Connection, user_id: int, p: dict, method: str | None) -> None:
    """Новый период с этой минуты. Пересчёта остатка при смене тарифа пока нет."""
    conn.execute(
        """
        INSERT INTO subscriptions (user_id, plan, status, period_start, period_end,
                                   auto_renew, payment_method)
        VALUES (%(u)s, %(p)s, 'active', now(), now() + %(d)s, %(r)s, %(m)s)
        ON CONFLICT (user_id) DO UPDATE SET plan = excluded.plan, status = 'active',
            period_start = now(), period_end = now() + %(d)s, auto_renew = excluded.auto_renew,
            payment_method = coalesce(excluded.payment_method, subscriptions.payment_method),
            renew_attempts = 0, updated_at = now()
        """,
        {"u": user_id, "p": p["code"], "d": PERIOD, "r": p["period"] == "month", "m": method},
    )
    conn.execute("UPDATE users SET plan = %s WHERE id = %s", (p["code"], user_id))


def set_auto_renew(conn: psycopg.Connection, user: dict, on: bool) -> None:
    conn.execute("UPDATE subscriptions SET auto_renew = %s, updated_at = now()"
                 " WHERE user_id = %s", (on, user["id"]))


def payment_history(conn: psycopg.Connection, user: dict, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT p.*, pl.title FROM payments p JOIN plans pl ON pl.code = p.plan"
        " WHERE p.user_id = %s ORDER BY p.id DESC LIMIT %s", (user["id"], limit)).fetchall()


# --- продление ----------------------------------------------------------------------

def renew_due(conn: psycopg.Connection, prov: Provider) -> dict:
    """Раз в час из планировщика. Итог — сколько продлено, не списалось, закрыто."""
    out = {"renewed": 0, "failed": 0, "expired": 0}
    due = conn.execute(
        "SELECT s.*, p.price_rub, p.period FROM subscriptions s JOIN plans p ON p.code = s.plan"
        " WHERE s.status IN ('active', 'past_due') AND s.period_end <= now()"
    ).fetchall()
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    for sub in due:
        renewable = sub["period"] == "month" and sub["auto_renew"] and sub["payment_method"]
        if not renewable or now >= sub["period_end"] + GRACE \
                or sub["renew_attempts"] >= MAX_RENEW_ATTEMPTS:
            if not renewable or now >= sub["period_end"] + GRACE:
                _expire(conn, sub["user_id"])
                out["expired"] += 1
            continue
        if sub["renew_attempts"] and sub["updated_at"] > now - RETRY_EVERY:
            continue  # повтор списания — не чаще раза в сутки
        key = f"renew:{sub['user_id']}:{sub['period_end'].isoformat()}:{sub['renew_attempts']}"
        pay = conn.execute(
            "INSERT INTO payments (user_id, plan, amount_rub, purpose, provider, idempotence_key)"
            " VALUES (%s, %s, %s, 'renewal', %s, %s) ON CONFLICT (idempotence_key) DO NOTHING"
            " RETURNING *",
            (sub["user_id"], sub["plan"], sub["price_rub"], prov.name, key),
        ).fetchone()
        if pay is None:
            continue  # эта попытка уже была
        try:
            pid, state = prov.charge(pay, sub["payment_method"])
        except Exception as exc:  # сеть или сервис — считаем неудачей, повторим позже
            pid, state = None, {"status": "canceled", "error": str(exc)}
        ok = state["status"] == "succeeded"
        conn.execute(
            "UPDATE payments SET provider_id = %s, status = %s, error = %s,"
            " paid_at = CASE WHEN %s THEN now() END WHERE id = %s",
            (pid, "succeeded" if ok else "canceled", state.get("error"), ok, pay["id"]))
        if ok:
            conn.execute(
                "UPDATE subscriptions SET status = 'active', period_start = period_end,"
                " period_end = period_end + %s, renew_attempts = 0, updated_at = now()"
                " WHERE user_id = %s", (PERIOD, sub["user_id"]))
            out["renewed"] += 1
        else:
            conn.execute(
                "UPDATE subscriptions SET status = 'past_due', renew_attempts = renew_attempts + 1,"
                " updated_at = now() WHERE user_id = %s", (sub["user_id"],))
            out["failed"] += 1
    return out


def _expire(conn: psycopg.Connection, user_id: int) -> None:
    conn.execute("UPDATE subscriptions SET status = 'canceled', updated_at = now()"
                 " WHERE user_id = %s", (user_id,))
    conn.execute("UPDATE users SET plan = 'free' WHERE id = %s", (user_id,))


def money(v) -> str:
    d = Decimal(str(v))
    s = f"{d:,.0f}" if d == d.to_integral() else f"{d:,.2f}"
    return s.replace(",", " ").replace(".", ",") + " ₽"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Деньги yaseo-app")
    parser.add_argument("command", choices=("renew",))
    args = parser.parse_args(argv)
    conn = db.connect()
    db.migrate(conn)
    if args.command == "renew":
        print(renew_due(conn, provider(conn)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
