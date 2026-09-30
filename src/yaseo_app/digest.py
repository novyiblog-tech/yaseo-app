"""Письмо раз в неделю: что изменилось на сайтах за семь дней.

Пишем, только если есть что сказать: сдвиги позиций, новые проверки, сертификат
на исходе. Пустое письмо не отправляется — оно учит не открывать следующие.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import psycopg
from jinja2 import Environment, FileSystemLoader, select_autoescape

from yaseo_app import history, mailer, monitor

HERE = Path(__file__).parent
TLS_WARN_DAYS = 21


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(HERE / "templates"),
                       autoescape=select_autoescape(["html", "j2"]))


def build(conn: psycopg.Connection, user: dict, today: date) -> dict | None:
    since = today - timedelta(days=7)
    sites = []
    for site in conn.execute("SELECT * FROM sites WHERE user_id = %s ORDER BY id",
                             (user["id"],)).fetchall():
        moves = monitor.changes(conn, site["id"], today)
        up = [m for m in moves if m["delta"] > 0]
        down = [m for m in moves if m["delta"] < 0]
        done = conn.execute(
            "SELECT * FROM jobs WHERE site_id = %s AND kind = 'audit' AND status = 'done'"
            " AND finished_at >= %s ORDER BY id DESC LIMIT 1", (site["id"], since)).fetchone()
        check = None
        if done:
            prev = conn.execute(
                "SELECT * FROM jobs WHERE site_id = %s AND kind = 'audit' AND status = 'done'"
                " AND id < %s ORDER BY id DESC LIMIT 1", (site["id"], done["id"])).fetchone()
            check = {"id": done["id"], "score": done["score"]}
            if prev:
                d = history.compare(prev["result"], done["result"])
                check.update(delta=d["score_delta"], fixed=d["fixed_count"], new=d["new_count"])
        tls = None
        last = conn.execute(
            "SELECT result->'free'->'checks'->'https'->'tls' AS tls FROM jobs WHERE site_id = %s"
            " AND status = 'done' AND kind = 'audit' ORDER BY id DESC LIMIT 1",
            (site["id"],)).fetchone()
        if last and last["tls"] and last["tls"].get("ok"):
            left = (date.fromisoformat(last["tls"]["not_after"][:10]) - today).days
            if left <= TLS_WARN_DAYS:
                tls = {"days": left, "until": last["tls"]["not_after"][:10]}
        if moves or check or tls:
            sites.append({"url": site["url"], "id": site["id"], "up": up, "down": down,
                          "moves": sorted(moves, key=lambda m: -abs(m["delta"]))[:8],
                          "check": check, "tls": tls})
    if not sites:
        return None
    return {"sites": sites, "since": since, "today": today}


def render(user: dict, data: dict) -> tuple[str, str, str]:
    base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
    ctx = dict(data, base=base, user=user,
               unsubscribe=f"{base}/unsubscribe?u={user['id']}"
                           f"&t={mailer.unsubscribe_token(user['id'])}")
    env = _env()
    html = env.get_template("email/digest.html.j2").render(**ctx)
    text = env.get_template("email/digest.txt.j2").render(**ctx)
    ups = sum(len(s["up"]) for s in data["sites"])
    downs = sum(len(s["down"]) for s in data["sites"])
    # Без склонений по числу: «позиции: выросли 1, просели 4» читается при любом числе.
    parts = [f"выросли {ups}"] * bool(ups) + [f"просели {downs}"] * bool(downs)
    subject = "yaSEO за неделю — " + ("позиции: " + ", ".join(parts) if parts
                                      else "что нового на сайтах")
    return subject, html, text


def queue_weekly(conn: psycopg.Connection, today: date) -> int:
    """Поставить письма недели всем, кто их не отключил. Повтор в ту же неделю — ничего."""
    year, week, _ = today.isocalendar()
    n = 0
    for user in conn.execute("SELECT * FROM users WHERE weekly_digest AND email_confirmed_at IS NOT NULL").fetchall():
        data = build(conn, user, today)
        if data is None:
            continue
        subject, html, text = render(user, data)
        if mailer.queue(conn, user, subject, html, text, "digest",
                        dedupe_key=f"digest:{user['id']}:{year}-W{week:02d}"):
            n += 1
    return n
