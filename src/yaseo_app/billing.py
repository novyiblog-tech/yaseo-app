"""Тарифы, лимиты, оплата, продление и переход на тариф выше.

Платёжный сервис — за интерфейсом Provider. Сейчас есть только виртуальный (FakeProvider):
на нём проходят покупка, отказ, продление и неудачное списание без денег и без договора.
Настоящий (ЮKassa или CloudPayments) подключается тем же интерфейсом, когда будут ИП,
магазин и ключи.

Сроки (Сергей, 30.09.2026):
- тариф оплачивается на месяц (30 дней) или на 3 месяца; лимиты — на каждые 30 дней срока;
  разовый аудит на 3 месяца — по полному аудиту на каждые 30 дней (01.10.2026);
- сам срок не продлевается: письмо за 3 дня до конца и в день окончания, продлить можно
  в кабинете — новый срок добавляется к текущему;
- автопродление — только если человек сам включил его с отдельным согласием;
- тариф выше можно взять в любой момент: неиспользованные полные дни текущего
  засчитываются, новый срок начинается с оплаты. Тариф ниже — после конца текущего.

Правила платежей:
- платёж сначала создаётся у нас, потом у сервиса; ключ идемпотентности — наш;
- статус платежа меняется только после запроса к сервису: телу уведомления не верим;
- повторное уведомление о том же платеже тариф второй раз не продлевает.

    python -m yaseo_app.billing renew    # напомнить, продлить и закрыть истёкшие сроки
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import ssl
import urllib.request
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal

import psycopg

from yaseo_app import db, legal
from yaseo_app.report import plural
from yaseo_app.accounts import Refused

PERIOD = timedelta(days=30)
# Не списалось при автопродлении — даём три дня и три попытки, потом тариф бесплатный.
GRACE = timedelta(days=3)
MAX_RENEW_ATTEMPTS = 3
RETRY_EVERY = timedelta(days=1)
REMIND_BEFORE = timedelta(days=3)


# --- тариф и лимиты ---------------------------------------------------------------

def plans(conn: psycopg.Connection, public_only: bool = True) -> list[dict]:
    where = "WHERE public" if public_only else ""
    rows = conn.execute(f"SELECT * FROM plans {where} ORDER BY sort").fetchall()
    terms = terms_by_plan(conn)
    for r in rows:
        r["terms"] = terms.get(r["code"], [])
    return rows


def terms_by_plan(conn: psycopg.Connection) -> dict[str, list[dict]]:
    """Сроки дольше месяца: цена и выгода против помесячной оплаты."""
    out: dict[str, list[dict]] = {}
    for t in conn.execute("SELECT t.*, p.price_rub AS monthly FROM plan_terms t"
                          " JOIN plans p ON p.code = t.plan ORDER BY t.plan, t.months").fetchall():
        t["saving"] = t["monthly"] * t["months"] - t["price_rub"]
        out.setdefault(t["plan"], []).append(t)
    return out


def plan(conn: psycopg.Connection, code: str) -> dict:
    row = conn.execute("SELECT * FROM plans WHERE code = %s", (code,)).fetchone()
    if row is None:
        raise Refused("Такого тарифа нет.")
    return row


def term_price(conn: psycopg.Connection, p: dict, months: int) -> Decimal:
    if months == 1:
        return p["price_rub"]
    if p["period"] not in ("month", "once"):
        raise Refused("Этот тариф не покупается на срок.")
    row = conn.execute("SELECT price_rub FROM plan_terms WHERE plan = %s AND months = %s",
                       (p["code"], months)).fetchone()
    if row is None:
        raise Refused(f"Тариф «{p['title']}» на {months} мес. не продаётся.")
    return row["price_rub"]


def subscription(conn: psycopg.Connection, user_id: int) -> dict | None:
    return conn.execute("SELECT * FROM subscriptions WHERE user_id = %s",
                        (user_id,)).fetchone()


def current(conn: psycopg.Connection, user: dict) -> dict:
    """Действующий тариф, начало текущих 30 дней лимитов, конец срока и подписка.
    Истёкшая подписка — это free."""
    sub = subscription(conn, user["id"])
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    live = sub and sub["status"] != "canceled" and (
        sub["period_end"] > now
        or (sub["status"] == "past_due" and sub["period_end"] + GRACE > now))
    if live:
        p = plan(conn, sub["plan"])
        if p["period"] == "free":
            # Бесплатные по сути тарифы (промокод, бета) считают проверки в скользящем окне.
            start = now - timedelta(days=p["free_every_days"] or 30)
        else:
            # Срок в 3 месяца — это три окна по 30 дней, у каждого свои лимиты.
            passed = max(0, int((now - sub["period_start"]) / PERIOD))
            start = sub["period_start"] + PERIOD * passed
        return {"plan": p, "sub": sub, "start": start, "end": sub["period_end"], "now": now}
    free = plan(conn, "free")
    days = timedelta(days=free["free_every_days"] or 30)
    return {"plan": free, "sub": sub, "start": now - days, "end": None, "now": now}


def usage(conn: psycopg.Connection, user: dict, cur: dict | None = None) -> dict:
    """Сколько выбрано в текущих 30 днях. Ответы нейросети считаются по журналу
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


def feature_list(conn: psycopg.Connection) -> list[dict]:
    """Настройки кабинета и с какого тарифа каждая (plans.sort — порядок тарифов)."""
    return conn.execute("SELECT f.*, p.title AS plan_title, p.sort FROM features f"
                        " JOIN plans p ON p.code = f.from_plan ORDER BY p.sort, f.code").fetchall()


def features(conn: psycopg.Connection, user: dict, cur: dict | None = None) -> dict[str, bool]:
    """Какие настройки доступны кабинету на его тарифе сейчас."""
    cur = cur or current(conn, user)
    return {f["code"]: cur["plan"]["sort"] >= f["sort"] for f in feature_list(conn)}


def has(conn: psycopg.Connection, user: dict, code: str) -> bool:
    return features(conn, user).get(code, False)


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
        raise Refused(f"Проверки по тарифу «{p['title']}» на эти 30 дней закончились. "
                      "Больше проверок — на тарифе выше.")
    return {"plan": p["code"],
            "max_pages": p.get("max_pages"),
            "queries": p["queries_per_audit"],
            "answers": _left(p["ai_checks"], used["ai_checks"])}


# --- платёжные сервисы ------------------------------------------------------------

class Provider:
    name = ""

    def create(self, payment: dict, return_url: str) -> tuple[str, str]:
        """Платёж у сервиса. Возвращает его номер и адрес страницы оплаты.
        payment["auto_renew"] — сохранить карту для автопродления (с согласия человека)."""
        raise NotImplementedError

    def fetch(self, provider_id: str) -> dict:
        """Статус у сервиса: {"status": pending|succeeded|canceled, "method": str|None}."""
        raise NotImplementedError

    def charge(self, payment: dict, method: str) -> tuple[str, dict]:
        """Списание сохранённым способом без участия человека (автопродление)."""
        raise NotImplementedError

    def saved_method(self, payment: dict) -> str | None:
        """Сохранённый способ оплаты покупателя, если сервис не прислал его сам."""
        return None


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

    def refund(self, provider_id: str) -> None:
        self.conn.execute("UPDATE fake_provider SET status = 'refunded'"
                          " WHERE id = %s AND status = 'succeeded'", (provider_id,))

    def decide(self, provider_id: str, pay: bool, method: str = "card_4242") -> None:
        self.conn.execute(
            "UPDATE fake_provider SET status = %s, method = %s"
            " WHERE id = %s AND status = 'pending'",
            ("succeeded" if pay else "canceled", method if pay else None, provider_id))


class TBankProvider(Provider):
    """Т-Банк Эквайринг, платёжная форма банка (без PCI DSS): Init -> редирект на PaymentURL ->
    GetState. Тестовый терминал (ключ оканчивается на DEMO) работает на том же хосте.
    Автопродление (Recurrent/RebillId) пока не подключено: charge не реализован."""
    name = "tbank"
    API = "https://securepay.tinkoff.ru/v2/"
    # Статусы банка: https://developer.tbank.ru/eacq/api/get-state
    DONE = {"CONFIRMED"}
    FAILED = {"REJECTED", "CANCELED", "DEADLINE_EXPIRED", "AUTH_FAIL", "REVERSED"}
    REFUNDED = {"REFUNDED"}  # PARTIAL_REFUNDED тариф не отключает: часть денег осталась

    def __init__(self, conn: psycopg.Connection, terminal: str | None = None,
                 password: str | None = None):
        self.conn = conn
        self.terminal = terminal or os.environ.get("YASEO_TBANK_TERMINAL", "")
        self.password = password or os.environ.get("YASEO_TBANK_PASSWORD", "")
        if not self.terminal or not self.password:
            raise ValueError("Т-Банк: не заданы YASEO_TBANK_TERMINAL и YASEO_TBANK_PASSWORD")

    @property
    def demo(self) -> bool:
        return self.terminal.endswith("DEMO")

    def token(self, fields: dict) -> str:
        """SHA-256 от значений корневых полей и пароля в порядке имён полей; вложенные не берём."""
        flat = {k: v for k, v in fields.items() if not isinstance(v, (dict, list)) and v is not None}
        flat["Password"] = self.password
        joined = "".join(("true" if flat[k] is True else "false" if flat[k] is False else str(flat[k]))
                         for k in sorted(flat))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def call(self, method: str, fields: dict) -> dict:
        body = {"TerminalKey": self.terminal, **fields}
        body["Token"] = self.token(body)
        req = urllib.request.Request(self.API + method, json.dumps(body).encode("utf-8"),
                                     {"Content-Type": "application/json"})
        # Банк выпускает сертификат в корне Минцифры: на сервере его может не быть в системном
        # хранилище — тогда путь к файлу корня в YASEO_TBANK_CA.
        ctx = ssl.create_default_context(cafile=os.environ.get("YASEO_TBANK_CA") or None)
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            out = json.loads(resp.read())
        if isinstance(out, list):  # GetCardList отвечает списком карт
            return out
        if not out.get("Success"):
            raise ValueError(f"Т-Банк {method}: {out.get('ErrorCode')} {out.get('Message')} "
                             f"{out.get('Details') or ''}".strip())
        return out

    def _receipt(self, payment: dict, kopecks: int) -> dict | None:
        taxation = os.environ.get("YASEO_TBANK_TAXATION")  # usn_income и т. п.; пусто — без чека
        if not taxation:
            return None
        user = self.conn.execute("SELECT email FROM users WHERE id = %s",
                                 (payment["user_id"],)).fetchone()
        title = self.conn.execute("SELECT title FROM plans WHERE code = %s",
                                  (payment["plan"],)).fetchone()["title"]
        return {"Email": user["email"], "Taxation": taxation,
                "Items": [{"Name": f"yaSEO, тариф «{title}»"[:128], "Price": kopecks,
                           "Quantity": 1, "Amount": kopecks,
                           "Tax": os.environ.get("YASEO_TBANK_TAX", "none"),
                           "PaymentMethod": "full_payment", "PaymentObject": "service"}]}

    def create(self, payment, return_url):
        kopecks = int((Decimal(payment["amount_rub"]) * 100).to_integral_value())
        base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
        fields = {"Amount": kopecks, "OrderId": f"yaseo-{payment['id']}",
                  "Description": "Оплата тарифа yaSEO",
                  "SuccessURL": return_url, "FailURL": return_url,
                  "NotificationURL": f"{base}/pay/webhook/tbank"}
        if payment.get("auto_renew"):
            # С согласия человека: банк сохранит карту и вернёт RebillId (CIT, первый платёж).
            fields.update({"Recurrent": "Y", "CustomerKey": self.customer_key(payment),
                           "OperationInitiatorType": "1"})
        else:
            fields["DATA"] = {"OperationInitiatorType": "0"}
        receipt = self._receipt(payment, kopecks)
        if receipt:
            fields["Receipt"] = receipt
        out = self.call("Init", fields)
        return str(out["PaymentId"]), out["PaymentURL"]

    def _status(self, st: str) -> str:
        return ("succeeded" if st in self.DONE else "refunded" if st in self.REFUNDED
                else "canceled" if st in self.FAILED else "pending")

    def fetch(self, provider_id):
        out = self.call("GetState", {"PaymentId": provider_id})
        st = out.get("Status", "")
        return {"status": self._status(st), "method": None,
                "amount": Decimal(out["Amount"]) / 100, "bank_status": st}

    @staticmethod
    def customer_key(payment: dict) -> str:
        return f"yaseo-{payment['user_id']}"

    def notification_valid(self, body: dict) -> bool:
        """Подпись уведомления банка: та же схема, что у запроса, поле Token не входит."""
        token = body.get("Token")
        if not isinstance(token, str) or body.get("TerminalKey") != self.terminal:
            return False
        rest = {k: v for k, v in body.items() if k != "Token"}
        return hmac.compare_digest(token, self.token(rest))

    def saved_method(self, payment):
        """RebillId последней сохранённой карты покупателя (если уведомление не дошло)."""
        out = self.call("GetCardList", {"CustomerKey": self.customer_key(payment)})
        cards = out if isinstance(out, list) else out.get("Cards") or []
        ids = [c.get("RebillId") for c in cards
               if c.get("RebillId") and c.get("Status", "A") == "A"]
        return str(ids[-1]) if ids else None

    def charge(self, payment, method):
        """Автопродление: новый платёж (Init) и списание с сохранённой карты (Charge)."""
        kopecks = int((Decimal(payment["amount_rub"]) * 100).to_integral_value())
        base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
        fields = {"Amount": kopecks, "OrderId": f"yaseo-{payment['id']}",
                  "Description": "Продление тарифа yaSEO",
                  "CustomerKey": self.customer_key(payment),
                  "NotificationURL": f"{base}/pay/webhook/tbank",
                  "OperationInitiatorType": "R"}
        receipt = self._receipt(payment, kopecks)
        if receipt:
            fields["Receipt"] = receipt
        pid = str(self.call("Init", fields)["PaymentId"])
        out = self.call("Charge", {"PaymentId": pid, "RebillId": method})
        status = self._status(out.get("Status", ""))
        if status == "pending":
            status = self.fetch(pid)["status"]
        return pid, {"status": status, "method": method,
                     "error": None if status == "succeeded" else f"Т-Банк: {out.get('Status')}"}


class OffProvider(Provider):
    """Боевой сервер без подключённой оплаты: виртуальная оплата выдавала бы тариф даром.
    Покупка отказывает до создания платежа; напоминания и закрытие сроков работают."""
    name = "off"

    def charge(self, payment, method):
        raise RuntimeError("оплата не подключена")


def provider(conn: psycopg.Connection) -> Provider:
    name = os.environ.get("YASEO_PAYMENTS", "fake")
    if name == "fake":
        return FakeProvider(conn)
    if name == "off":
        return OffProvider()
    if name == "tbank":
        prov = TBankProvider(conn)
        # Тестовый терминал: денег нет, реквизиты оферты не нужны.
        if prov.demo or legal.complete():
            return prov
    if not legal.complete():
        # Настоящие деньги — только когда в оферте есть реквизиты исполнителя.
        raise ValueError("реквизиты оферты не заполнены: "
                         + ", ".join(legal.requisites()["missing"]))
    raise ValueError(f"платёжный сервис «{name}» не подключён: нужны ИП, магазин и ключи")


# --- что будет, если купить -------------------------------------------------------

def credit(conn: psycopg.Connection, cur: dict) -> Decimal:
    """Сколько стоят неиспользованные полные дни текущего срока: цена срока последней
    оплаты, делённая на его дни. Бесплатные тарифы и разовый аудит на месяц не засчитываются:
    он и есть одна проверка. Разовый аудит на 3 месяца засчитывается, как месячный (01.10.2026)."""
    sub = cur["sub"]
    if sub is None or cur["end"] is None:
        return Decimal(0)
    period = cur["plan"]["period"]
    if period != "month" and not (period == "once" and sub["months"] > 1):
        return Decimal(0)
    pay = conn.execute(
        "SELECT * FROM payments WHERE user_id = %s AND plan = %s AND status = 'succeeded'"
        " ORDER BY paid_at DESC NULLS LAST, id DESC LIMIT 1",
        (sub["user_id"], sub["plan"])).fetchone()
    if pay is None:
        return Decimal(0)
    days_left = max(0, (cur["end"] - cur["now"]).days)
    per_day = (pay["amount_rub"] + pay["credit_rub"]) / (pay["months"] * PERIOD.days)
    return (per_day * days_left).quantize(Decimal(1), rounding=ROUND_FLOOR)


def quote(conn: psycopg.Connection, user: dict, plan_code: str, months: int = 1,
          cur: dict | None = None) -> dict:
    """Что будет при покупке: вид платежа, цена срока, зачёт и сумма к оплате.
    Отказ — Refused с понятной причиной."""
    p = plan(conn, plan_code)
    if p["period"] == "free" or not p["public"]:
        raise Refused("Этот тариф не покупается.")
    price = term_price(conn, p, months)
    cur = cur or current(conn, user)
    have = cur["plan"]
    paid_now = have["period"] in ("once", "month") and cur["end"] is not None
    kind, off = "purchase", Decimal(0)
    if paid_now and have["code"] == p["code"]:
        if p["period"] == "once":
            raise Refused("Разовый аудит уже оплачен. Нужны ещё проверки — тариф «Старт».")
        kind = "renewal"
    elif paid_now and p["sort"] < have["sort"]:
        raise Refused(f"Перейти на «{p['title']}» можно, когда закончится «{have['title']}» — "
                      f"{cur['end'].astimezone():%d.%m.%Y}.")
    elif paid_now:
        kind, off = "upgrade", credit(conn, cur)
        if off >= price:
            raise Refused(f"Остаток по «{have['title']}» ({money(off)}) больше цены этого срока. "
                          "Выберите срок подольше.")
    return {"plan": p, "months": months, "kind": kind, "price": price, "credit": off,
            "amount": price - off}


def offers(conn: psycopg.Connection, user: dict) -> dict:
    """Для страницы тарифа: по каждому платному тарифу — что можно купить (сроки с суммой)
    и, если ничего нельзя, причина."""
    cur = current(conn, user)
    out: dict = {}
    for p in plans(conn):
        if p["period"] == "free":
            continue
        options, why = [], None
        for months in [1] + [t["months"] for t in p["terms"]]:
            try:
                options.append(quote(conn, user, p["code"], months, cur))
            except Refused as exc:
                why = why or str(exc)
        out[p["code"]] = {"options": options, "refused": None if options else why}
    return out


# --- покупка ------------------------------------------------------------------------

def start_purchase(conn: psycopg.Connection, user: dict, plan_code: str, prov: Provider,
                   return_url: str, months: int = 1, auto_renew: bool = False) -> str:
    from yaseo_app import verify
    verify.require_confirmed(user)
    if prov.name == "off":
        raise Refused("Оплата ещё не подключена. Тарифы откроем в ближайшее время.")
    q = quote(conn, user, plan_code, months)
    pay = conn.execute(
        "INSERT INTO payments (user_id, plan, amount_rub, purpose, provider, idempotence_key,"
        " offer_version, months, credit_rub, auto_renew)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (user["id"], q["plan"]["code"], q["amount"], q["kind"], prov.name,
         secrets.token_hex(16), legal.OFFER_VERSION, months, q["credit"],
         auto_renew and q["plan"]["period"] == "month"),
    ).fetchone()
    pid, url = prov.create(pay, return_url.replace("{payment}", str(pay["id"])))
    conn.execute("UPDATE payments SET provider_id = %s WHERE id = %s", (pid, pay["id"]))
    return url


def confirm(conn: psycopg.Connection, prov: Provider, provider_id: str) -> dict | None:
    """Сверить платёж с сервисом и, если оплачен, включить тариф; если оплаченный платёж
    вернули — отключить. Повтор безопасен."""
    with conn.transaction():
        pay = conn.execute("SELECT * FROM payments WHERE provider_id = %s AND provider = %s"
                           " FOR UPDATE", (provider_id, prov.name)).fetchone()
        if pay is None:
            return None
        if pay["status"] == "succeeded":
            try:
                state = prov.fetch(provider_id)
            except Exception:  # сервис недоступен — оставляем как есть, проверим в следующий раз
                return pay
            if state["status"] == "refunded":
                return _refund(conn, pay)
            return pay
        if pay["status"] != "pending":
            return pay
        state = prov.fetch(provider_id)
        if state["status"] == "refunded":  # оплатили и вернули, пока мы не смотрели
            state["status"] = "canceled"
        if state["status"] == "succeeded" and state.get("amount") is not None \
                and Decimal(state["amount"]) != Decimal(pay["amount_rub"]):
            raise ValueError(f"сумма платежа {provider_id} у сервиса {state['amount']} "
                             f"не совпадает с нашей {pay['amount_rub']}")
        if state["status"] == "succeeded":
            method = state.get("method") or pay.get("payment_method")
            if not method and pay["auto_renew"]:
                try:
                    method = prov.saved_method(pay)
                except Exception:  # без карты автопродление просто не включится
                    method = None
            _activate(conn, pay, method)
        if state["status"] != "pending":
            pay = conn.execute(
                "UPDATE payments SET status = %s, paid_at = CASE WHEN %s = 'succeeded'"
                " THEN now() END WHERE id = %s RETURNING *",
                (state["status"], state["status"], pay["id"]),
            ).fetchone()
        return pay


def save_method(conn: psycopg.Connection, prov: Provider, provider_id: str, method: str) -> None:
    """Сервис прислал сохранённый способ оплаты (RebillId). Берём его только из проверенного
    уведомления. Если платёж уже включил тариф, способ уходит и в подписку."""
    with conn.transaction():
        pay = conn.execute(
            "UPDATE payments SET payment_method = %s WHERE provider = %s AND provider_id = %s"
            " AND auto_renew RETURNING *", (method, prov.name, provider_id)).fetchone()
        if pay and pay["status"] == "succeeded":
            conn.execute("UPDATE subscriptions SET payment_method = %s, updated_at = now()"
                         " WHERE user_id = %s AND plan = %s AND status <> 'canceled'",
                         (method, pay["user_id"], pay["plan"]))


def _refund(conn: psycopg.Connection, pay: dict) -> dict:
    """Деньги вернули: платёж помечается, тариф отключается. Продление отнимает только свой
    срок. Если после этого платежа уже была другая оплата, срок не трогаем."""
    pay = conn.execute("UPDATE payments SET status = 'refunded', refunded_at = now()"
                       " WHERE id = %s RETURNING *", (pay["id"],)).fetchone()
    later = conn.execute("SELECT 1 FROM payments WHERE user_id = %s AND status = 'succeeded'"
                         " AND id > %s", (pay["user_id"], pay["id"])).fetchone()
    sub = subscription(conn, pay["user_id"])
    if later or sub is None or sub["plan"] != pay["plan"] or sub["status"] == "canceled":
        return pay
    shorter = conn.execute("SELECT %s::timestamptz - %s::interval AS t",
                           (sub["period_end"], PERIOD * pay["months"])).fetchone()["t"]
    if pay["purpose"] == "renewal" and shorter > sub["period_start"]:
        conn.execute("UPDATE subscriptions SET period_end = %s, updated_at = now()"
                     " WHERE user_id = %s", (shorter, pay["user_id"]))
    else:
        conn.execute("UPDATE subscriptions SET status = 'canceled', auto_renew = false,"
                     " period_end = least(period_end, now()), updated_at = now()"
                     " WHERE user_id = %s", (pay["user_id"],))
        conn.execute("UPDATE users SET plan = 'free' WHERE id = %s", (pay["user_id"],))
    return pay


def _activate(conn: psycopg.Connection, pay: dict, method: str | None) -> None:
    """Продление добавляет срок к текущему, покупка и переход начинают новый с этой минуты."""
    args = {"u": pay["user_id"], "p": pay["plan"], "d": PERIOD * pay["months"],
            "n": pay["months"], "r": pay["auto_renew"], "m": method}
    sub = subscription(conn, pay["user_id"])
    extend = (pay["purpose"] == "renewal" and sub is not None and sub["plan"] == pay["plan"]
              and sub["status"] != "canceled")
    if extend:
        conn.execute(
            """
            UPDATE subscriptions SET status = 'active', months = %(n)s, renew_attempts = 0,
                period_start = CASE WHEN period_end < now() THEN now() ELSE period_start END,
                period_end = greatest(period_end, now()) + %(d)s,
                auto_renew = auto_renew OR %(r)s,
                auto_renew_consent_at = CASE WHEN %(r)s THEN now() ELSE auto_renew_consent_at END,
                payment_method = coalesce(%(m)s, payment_method), updated_at = now()
            WHERE user_id = %(u)s
            """, args)
    else:
        conn.execute(
            """
            INSERT INTO subscriptions (user_id, plan, status, period_start, period_end, months,
                                       auto_renew, auto_renew_consent_at, payment_method)
            VALUES (%(u)s, %(p)s, 'active', now(), now() + %(d)s, %(n)s, %(r)s,
                    CASE WHEN %(r)s THEN now() END, %(m)s)
            ON CONFLICT (user_id) DO UPDATE SET plan = excluded.plan, status = 'active',
                period_start = now(), period_end = excluded.period_end, months = excluded.months,
                auto_renew = excluded.auto_renew,
                auto_renew_consent_at = excluded.auto_renew_consent_at,
                payment_method = coalesce(excluded.payment_method, subscriptions.payment_method),
                renew_attempts = 0, updated_at = now()
            """, args)
    conn.execute("UPDATE users SET plan = %s WHERE id = %s", (pay["plan"], pay["user_id"]))


def set_auto_renew(conn: psycopg.Connection, user: dict, on: bool, consent: bool = False) -> None:
    """Включить — только с согласием на списания и сохранённой картой. Выключить — всегда."""
    sub = subscription(conn, user["id"])
    if not on:
        if sub:
            conn.execute("UPDATE subscriptions SET auto_renew = false, updated_at = now()"
                         " WHERE user_id = %s", (user["id"],))
        return
    cur = current(conn, user)
    if cur["plan"]["period"] != "month" or sub is None:
        raise Refused("Автопродление — для тарифов на месяц и на 3 месяца.")
    if not consent:
        raise Refused("Чтобы включить автопродление, отметьте согласие на списания.")
    if not sub["payment_method"]:
        raise Refused("Карта не сохранена. Отметьте «продлевать автоматически» при следующей "
                      "оплате — тогда продление включится.")
    conn.execute("UPDATE subscriptions SET auto_renew = true, auto_renew_consent_at = now(),"
                 " updated_at = now() WHERE user_id = %s", (user["id"],))


def payment_history(conn: psycopg.Connection, user: dict, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT p.*, pl.title FROM payments p JOIN plans pl ON pl.code = p.plan"
        " WHERE p.user_id = %s ORDER BY p.id DESC LIMIT %s", (user["id"], limit)).fetchall()


# --- напоминания, автопродление, окончание срока -----------------------------------

def _mail(conn, user_id: int, subject: str, name: str, key: str, **ctx) -> None:
    from yaseo_app import mailer
    user = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if user and user["email_confirmed_at"]:
        mailer.notify(conn, user, subject, name, "billing", dedupe_key=key, **ctx)


def _renew_offer(conn, code: str) -> dict:
    p = plan(conn, code)
    return {"plan": p, "terms": terms_by_plan(conn).get(code, [])}


def remind_due(conn: psycopg.Connection) -> int:
    """Письмо за 3 дня до конца срока месячного тарифа. Одно на срок."""
    rows = conn.execute(
        "SELECT s.* FROM subscriptions s JOIN plans p ON p.code = s.plan"
        " WHERE p.period = 'month' AND s.status = 'active'"
        " AND s.period_end > now() AND s.period_end <= now() + %s", (REMIND_BEFORE,)).fetchall()
    for sub in rows:
        offer = _renew_offer(conn, sub["plan"])
        amount = term_price(conn, offer["plan"], sub["months"]) if sub["auto_renew"] else None
        _mail(conn, sub["user_id"],
              f"Тариф «{offer['plan']['title']}» заканчивается "
              f"{sub['period_end'].astimezone():%d.%m}",
              "expiring", f"expiring:{sub['user_id']}:{sub['period_end']:%Y-%m-%d}",
              sub=sub, amount=amount, **offer)
    return len(rows)


def renew_due(conn: psycopg.Connection, prov: Provider) -> dict:
    """Раз в минуту из планировщика: напомнить, продлить с согласия, закрыть истёкшие."""
    out = {"reminded": remind_due(conn), "renewed": 0, "failed": 0, "expired": 0}
    due = conn.execute(
        "SELECT s.*, p.period FROM subscriptions s JOIN plans p ON p.code = s.plan"
        " WHERE s.status IN ('active', 'past_due') AND s.period_end <= now()"
    ).fetchall()
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    for sub in due:
        renewable = sub["period"] == "month" and sub["auto_renew"] and sub["payment_method"]
        if not renewable or now >= sub["period_end"] + GRACE \
                or sub["renew_attempts"] >= MAX_RENEW_ATTEMPTS:
            if not renewable or now >= sub["period_end"] + GRACE:
                _expire(conn, sub)
                out["expired"] += 1
            continue
        if sub["renew_attempts"] and sub["updated_at"] > now - RETRY_EVERY:
            continue  # повтор списания — не чаще раза в сутки
        amount = term_price(conn, plan(conn, sub["plan"]), sub["months"])
        key = f"renew:{sub['user_id']}:{sub['period_end'].isoformat()}:{sub['renew_attempts']}"
        pay = conn.execute(
            "INSERT INTO payments (user_id, plan, amount_rub, purpose, provider, idempotence_key,"
            " months, auto_renew) VALUES (%s, %s, %s, 'renewal', %s, %s, %s, true)"
            " ON CONFLICT (idempotence_key) DO NOTHING RETURNING *",
            (sub["user_id"], sub["plan"], amount, prov.name, key, sub["months"]),
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
                " WHERE user_id = %s", (PERIOD * sub["months"], sub["user_id"]))
            out["renewed"] += 1
        else:
            conn.execute(
                "UPDATE subscriptions SET status = 'past_due', renew_attempts = renew_attempts + 1,"
                " updated_at = now() WHERE user_id = %s", (sub["user_id"],))
            out["failed"] += 1
    return out


def _expire(conn: psycopg.Connection, sub: dict) -> None:
    conn.execute("UPDATE subscriptions SET status = 'canceled', updated_at = now()"
                 " WHERE user_id = %s", (sub["user_id"],))
    conn.execute("UPDATE users SET plan = 'free' WHERE id = %s", (sub["user_id"],))
    if sub["period"] == "month":
        offer = _renew_offer(conn, sub["plan"])
        _mail(conn, sub["user_id"], f"Тариф «{offer['plan']['title']}» закончился", "expired",
              f"expired:{sub['user_id']}:{sub['period_end']:%Y-%m-%d}", sub=sub, **offer)


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
