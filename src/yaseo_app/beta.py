"""Закрытая бета: коды приглашений и лист ожидания.

    python -m yaseo_app.beta invite --count 32 --note "комментарии под постом 19.09"
    python -m yaseo_app.beta invite --count 5 --uses 3 --plan pro --days 30
    python -m yaseo_app.beta list
    python -m yaseo_app.beta waitlist
    python -m yaseo_app.beta send-invites --limit 10     # письма первым из листа ожидания

Режим беты включается переменной YASEO_BETA=1: тогда регистрация без кода закрыта,
а на лендинге вместо регистрации — «оставить почту».
"""
from __future__ import annotations

import argparse
import os
import secrets
from datetime import timedelta
from pathlib import Path

import psycopg
from jinja2 import Environment, FileSystemLoader, select_autoescape

from yaseo_app import db, mailer
from yaseo_app.accounts import EMAIL_RE, Refused, normalize_email

HERE = Path(__file__).parent
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O и 1/I — не путаются при вводе


def enabled() -> bool:
    return os.environ.get("YASEO_BETA") == "1"


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    c = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:]}" if len(c) == 8 else c


def create(conn: psycopg.Connection, count: int = 1, uses: int = 1, note: str | None = None,
           plan: str | None = None, days: int | None = None,
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
        raise Refused("Код приглашения не подошёл или уже использован.")
    if row["expires_at"] and conn.execute("SELECT now() > %s AS x",
                                          (row["expires_at"],)).fetchone()["x"]:
        raise Refused("Срок кода приглашения истёк.")
    return row


def redeem(conn: psycopg.Connection, user: dict, code: str) -> None:
    """Списать использование кода и выдать тариф, если код его даёт. Зовётся в той же
    транзакции, что и регистрация: код не уйдёт, если регистрация сорвалась."""
    inv = check(conn, code)
    got = conn.execute("UPDATE invites SET used = used + 1 WHERE code = %s AND used < max_uses"
                       " RETURNING *", (inv["code"],)).fetchone()
    if got is None:
        raise Refused("Код приглашения только что использовали.")
    conn.execute("UPDATE users SET invite_code = %s WHERE id = %s", (inv["code"], user["id"]))
    if inv["grant_plan"] and inv["grant_days"]:
        conn.execute(
            """
            INSERT INTO subscriptions (user_id, plan, status, period_start, period_end, auto_renew)
            VALUES (%s, %s, 'active', now(), now() + make_interval(days => %s), false)
            ON CONFLICT (user_id) DO NOTHING
            """, (user["id"], inv["grant_plan"], inv["grant_days"]))
        conn.execute("UPDATE users SET plan = %s WHERE id = %s", (inv["grant_plan"], user["id"]))


def join_waitlist(conn: psycopg.Connection, email: str, site: str | None, consent: bool,
                  source: str | None = None) -> None:
    email = normalize_email(email)
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise Refused("Проверьте адрес почты.")
    if not consent:
        raise Refused("Без согласия на обработку почты записать не можем.")
    conn.execute(
        "INSERT INTO waitlist (email, site, source, consent_at) VALUES (%s, %s, %s, now())"
        " ON CONFLICT (email) DO UPDATE SET site = coalesce(excluded.site, waitlist.site)",
        (email, (site or "")[:300] or None, (source or "")[:100] or None))


def send_invites(conn: psycopg.Connection, limit: int, uses: int = 1, plan: str | None = None,
                 days: int | None = None) -> int:
    """Письма с кодом первым из листа ожидания. Каждому — свой одноразовый код."""
    base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    rows = conn.execute("SELECT * FROM waitlist WHERE invited IS NULL ORDER BY id LIMIT %s",
                        (limit,)).fetchall()
    for w in rows:
        [code] = create(conn, 1, uses, note=f"лист ожидания #{w['id']}", plan=plan, days=days)
        link = f"{base}/signup?invite={code}" + (f"&site={w['site']}" if w["site"] else "")
        ctx = {"code": code, "link": link}
        html = env.get_template("email/invite.html.j2").render(**ctx)
        text = env.get_template("email/invite.txt.j2").render(**ctx)
        conn.execute("UPDATE waitlist SET invited = %s WHERE id = %s", (code, w["id"]))
        conn.execute(
            "INSERT INTO outbox (to_email, subject, html, text, kind, dedupe_key)"
            " VALUES (%s, %s, %s, %s, 'invite', %s) ON CONFLICT (dedupe_key) DO NOTHING",
            (w["email"], "yaSEO: приглашение в закрытую бету", html, text, f"invite:{w['id']}"))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Закрытая бета yaSEO")
    sub = parser.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("invite", help="выпустить коды")
    inv.add_argument("--count", type=int, default=1)
    inv.add_argument("--uses", type=int, default=1)
    inv.add_argument("--note")
    inv.add_argument("--plan", help="тариф, который даёт код (например pro)")
    inv.add_argument("--days", type=int, help="на сколько дней даётся тариф")
    inv.add_argument("--valid-days", type=int, default=60, help="срок жизни кода")
    sub.add_parser("list", help="коды и сколько использовано")
    sub.add_parser("waitlist", help="лист ожидания")
    si = sub.add_parser("send-invites", help="письма с кодами первым из листа ожидания")
    si.add_argument("--limit", type=int, required=True)
    si.add_argument("--plan")
    si.add_argument("--days", type=int)
    args = parser.parse_args(argv)

    conn = db.connect()
    db.migrate(conn)
    base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
    if args.cmd == "invite":
        for c in create(conn, args.count, args.uses, args.note, args.plan, args.days,
                        args.valid_days):
            print(f"{c}\t{base}/signup?invite={c}")
    elif args.cmd == "list":
        for r in conn.execute("SELECT * FROM invites ORDER BY created_at, code").fetchall():
            grant = f" {r['grant_plan']}×{r['grant_days']}д" if r["grant_plan"] else ""
            print(f"{r['code']}  {r['used']}/{r['max_uses']}{grant}  {r['note'] or ''}")
    elif args.cmd == "waitlist":
        for r in conn.execute("SELECT * FROM waitlist ORDER BY id").fetchall():
            print(f"{r['id']:>4}  {r['email']:32} {r['site'] or '':30} "
                  f"{'приглашён ' + r['invited'] if r['invited'] else ''}")
    elif args.cmd == "send-invites":
        print(f"писем поставлено: {send_invites(conn, args.limit, plan=args.plan, days=args.days)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
