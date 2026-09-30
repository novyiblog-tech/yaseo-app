"""Промокоды: полная проверка бесплатно — по коду при регистрации или в кабинете.

    python -m yaseo_app.promo create --count 32 --note "комментарии под постом 19.09"
    python -m yaseo_app.promo create --uses 100 --note "пост в Threads"   # один код на многих
    python -m yaseo_app.promo create --count 5 --plan pro --days 30        # тариф на срок
    python -m yaseo_app.promo list
    python -m yaseo_app.promo waitlist
    python -m yaseo_app.promo send-waitlist --limit 10   # письма с кодом тем, кто ждал беты

Сергей, 30.09.2026: закрытой беты больше нет, регистрация открыта. Без промокода —
урезанная проверка (тариф free), по промокоду — полная (тариф promo). Код даёт тариф
только кабинету на бесплатном тарифе и только один раз на кабинет.
"""
from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

import psycopg
from jinja2 import Environment, FileSystemLoader, select_autoescape

from yaseo_app import db
from yaseo_app.accounts import Refused

HERE = Path(__file__).parent
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O и 1/I — не путаются при вводе
PROMO_PLAN, PROMO_DAYS = "promo", 30


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    c = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:]}" if len(c) == 8 else c


def create(conn: psycopg.Connection, count: int = 1, uses: int = 1, note: str | None = None,
           plan: str = PROMO_PLAN, days: int = PROMO_DAYS,
           valid_days: int | None = 60) -> list[str]:
    codes = []
    for _ in range(count):
        code = new_code()
        conn.execute(
            "INSERT INTO invites (code, note, max_uses, grant_plan, grant_days, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL"
            " ELSE now() + make_interval(days => %s::int) END)",
            (code, note, uses, plan, days, valid_days, valid_days))
        codes.append(code)
    return codes


def check(conn: psycopg.Connection, code: str) -> dict:
    row = conn.execute("SELECT * FROM invites WHERE code = %s", (normalize_code(code),)).fetchone()
    if row is None or row["used"] >= row["max_uses"]:
        raise Refused("Промокод не подошёл или уже использован.")
    if row["expires_at"] and conn.execute("SELECT now() > %s AS x",
                                          (row["expires_at"],)).fetchone()["x"]:
        raise Refused("Срок промокода истёк.")
    return row


def redeem(conn: psycopg.Connection, user: dict, code: str) -> dict:
    """Списать использование кода и выдать тариф. При регистрации зовётся в той же
    транзакции: код не уйдёт, если регистрация сорвалась. Возвращает выданный тариф."""
    from yaseo_app import billing
    inv = check(conn, code)
    if user.get("invite_code"):
        raise Refused("Промокод в этом кабинете уже активирован — второй не добавить.")
    cur = billing.current(conn, user)
    if cur["plan"]["code"] != "free":
        raise Refused(f"Промокод действует на бесплатном тарифе, а у вас «{cur['plan']['title']}».")
    got = conn.execute("UPDATE invites SET used = used + 1 WHERE code = %s AND used < max_uses"
                       " RETURNING *", (inv["code"],)).fetchone()
    if got is None:
        raise Refused("Промокод только что использовали.")
    conn.execute("UPDATE users SET invite_code = %s WHERE id = %s", (inv["code"], user["id"]))
    plan = billing.plan(conn, inv["grant_plan"] or PROMO_PLAN)
    conn.execute(
        """
        INSERT INTO subscriptions (user_id, plan, status, period_start, period_end, auto_renew,
                                   renew_attempts, months)
        VALUES (%(u)s, %(p)s, 'active', now(), now() + make_interval(days => %(d)s), false, 0, 1)
        ON CONFLICT (user_id) DO UPDATE SET plan = excluded.plan, status = 'active',
            period_start = now(), period_end = excluded.period_end, auto_renew = false,
            renew_attempts = 0, months = 1, updated_at = now()
        """, {"u": user["id"], "p": plan["code"], "d": inv["grant_days"] or PROMO_DAYS})
    conn.execute("UPDATE users SET plan = %s WHERE id = %s", (plan["code"], user["id"]))
    return plan


def redeem_in_cabinet(conn: psycopg.Connection, user: dict, code: str) -> dict:
    if not (code or "").strip():
        raise Refused("Впишите промокод.")
    with conn.transaction():
        return redeem(conn, user, code)


def send_waitlist(conn: psycopg.Connection, limit: int) -> int:
    """Письма с одноразовым промокодом тем, кто оставил почту во время беты."""
    base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    rows = conn.execute("SELECT * FROM waitlist WHERE invited IS NULL ORDER BY id LIMIT %s",
                        (limit,)).fetchall()
    for w in rows:
        [code] = create(conn, 1, note=f"лист ожидания #{w['id']}")
        link = f"{base}/signup?promo={code}" + (f"&site={w['site']}" if w["site"] else "")
        ctx = {"code": code, "link": link}
        html = env.get_template("email/promo.html.j2").render(**ctx)
        text = env.get_template("email/promo.txt.j2").render(**ctx)
        conn.execute("UPDATE waitlist SET invited = %s WHERE id = %s", (code, w["id"]))
        conn.execute(
            "INSERT INTO outbox (to_email, subject, html, text, kind, dedupe_key)"
            " VALUES (%s, %s, %s, %s, 'promo', %s) ON CONFLICT (dedupe_key) DO NOTHING",
            (w["email"], "yaseo открылся: промокод на полную проверку", html, text,
             f"invite:{w['id']}"))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Промокоды yaseo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cr = sub.add_parser("create", help="выпустить коды")
    cr.add_argument("--count", type=int, default=1)
    cr.add_argument("--uses", type=int, default=1, help="сколько кабинетов примут один код")
    cr.add_argument("--note")
    cr.add_argument("--plan", default=PROMO_PLAN, help="тариф, который даёт код")
    cr.add_argument("--days", type=int, default=PROMO_DAYS, help="на сколько дней даётся тариф")
    cr.add_argument("--valid-days", type=int, default=60, help="срок жизни кода")
    sub.add_parser("list", help="коды и сколько использовано")
    sub.add_parser("waitlist", help="лист ожидания беты")
    sw = sub.add_parser("send-waitlist", help="письма с промокодом первым из листа ожидания")
    sw.add_argument("--limit", type=int, required=True)
    args = parser.parse_args(argv)

    conn = db.connect()
    db.migrate(conn)
    base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
    if args.cmd == "create":
        for c in create(conn, args.count, args.uses, args.note, args.plan, args.days,
                        args.valid_days):
            print(f"{c}\t{base}/signup?promo={c}")
    elif args.cmd == "list":
        for r in conn.execute("SELECT * FROM invites ORDER BY created_at, code").fetchall():
            grant = f" {r['grant_plan']}×{r['grant_days']}д" if r["grant_plan"] else ""
            print(f"{r['code']}  {r['used']}/{r['max_uses']}{grant}  {r['note'] or ''}")
    elif args.cmd == "waitlist":
        for r in conn.execute("SELECT * FROM waitlist ORDER BY id").fetchall():
            print(f"{r['id']:>4}  {r['email']:32} {r['site'] or '':30} "
                  f"{'код ' + r['invited'] if r['invited'] else ''}")
    elif args.cmd == "send-waitlist":
        print(f"писем поставлено: {send_waitlist(conn, args.limit)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
