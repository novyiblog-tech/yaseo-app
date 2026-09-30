"""Автопроверки по расписанию и срочные письма.

Автопроверка: раз в неделю или месяц сайт проверяется сам, с фразами последней ручной
проверки, и тратит проверки тарифа. Кончились проверки — пропускаем до следующего дня.
Результат приходит письмом.

Срочные письма (кому они доступны по тарифу и не выключены в настройках):
- сайт не открывается два часа подряд — письмо; открылся снова — письмо;
- сертификат HTTPS истекает через 14 дней и меньше или не проходит проверку;
- место по фразе упало сильно: вылетело из десятки или опустилось на 5 мест и больше.

Одно письмо на событие: ключи в outbox не дают повторить его в следующий проход.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg

from yaseo_app import accounts, billing, jobs, mailer, prefs

AUTO_HOUR = 6                 # автопроверки — с 06:00 по Москве
DOWN_AFTER = 2                # столько неудачных заходов подряд — «не открывается»
TLS_ALERT_DAYS = 14
DROP_PLACES = 5
SCHEDULE_DAYS = {"week": 7, "month": 30}


# --- автопроверки -----------------------------------------------------------------

def queue_autochecks(conn: psycopg.Connection, today: date) -> int:
    """Поставить автопроверки сайтам, у которых подошёл срок. Одна попытка в день на сайт."""
    n = 0
    due = conn.execute(
        "UPDATE sites SET schedule_tried = %s WHERE schedule <> 'off'"
        " AND schedule_tried IS DISTINCT FROM %s RETURNING *", (today, today)).fetchall()
    for site in due:  # одна попытка в день на сайт
        user = conn.execute("SELECT * FROM users WHERE id = %s AND email_confirmed_at IS NOT NULL",
                            (site["user_id"],)).fetchone()
        if user is None:
            continue
        every = SCHEDULE_DAYS.get(prefs.effective(conn, user, site)["schedule"])
        if not every:
            continue
        last = conn.execute(
            "SELECT max(created_at) AS t FROM jobs WHERE site_id = %s AND kind = 'audit'"
            " AND status <> 'failed'", (site["id"],)).fetchone()["t"]
        if last and last > datetime.now(timezone.utc) - timedelta(days=every):
            continue
        try:
            accounts.start_check(conn, user, site, "\n".join(site["schedule_queries"] or []),
                                 auto=True)
            n += 1
        except accounts.Refused:
            continue  # проверки тарифа кончились или проверка уже идёт — завтра
    return n


def after_audit(conn: psycopg.Connection, job: dict) -> None:
    """Автопроверка готова — письмо с оценкой и сравнением с прошлой."""
    if not (job.get("params") or {}).get("auto"):
        return
    done = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    if done is None or done["status"] != "done":
        return
    user = conn.execute("SELECT * FROM users WHERE id = %s", (done["user_id"],)).fetchone()
    prev = accounts.previous_done(conn, done)
    delta = None
    if prev and prev["score"] is not None and done["score"] is not None:
        delta = done["score"] - prev["score"]
    host = urlsplit(done["params"]["url"]).hostname
    mailer.notify(conn, user, f"Автопроверка {host}: оценка {done['score']} из 100",
                  "autocheck", "autocheck", dedupe_key=f"autocheck:{done['id']}",
                  job=done, host=host, delta=delta)


# --- здоровье сайта ------------------------------------------------------------------

def _alert_users(conn: psycopg.Connection) -> dict[int, dict]:
    out = {}
    for u in conn.execute("SELECT * FROM users WHERE alerts AND owner_id IS NULL"
                          " AND email_confirmed_at IS NOT NULL").fetchall():
        if billing.has(conn, u, "alerts"):
            out[u["id"]] = u
    return out


def queue_health(conn: psycopg.Connection, now: datetime) -> int:
    """Раз в час — проверка, открывается ли сайт и что с сертификатом. Задача на сайт."""
    # Отработанные проверки доступности не копим: итог уже в site_health.
    conn.execute("DELETE FROM jobs WHERE kind = 'health' AND status IN ('done', 'failed')"
                 " AND created_at < now() - interval '2 days'")
    users = _alert_users(conn)
    n = 0
    for site in conn.execute("SELECT * FROM sites ORDER BY id").fetchall():
        if site["user_id"] not in users:
            continue
        if jobs.enqueue(conn, site["user_id"], "health", {}, site_id=site["id"], max_attempts=1,
                        dedupe_key=f"health:{site['id']}:{now:%Y-%m-%d-%H}"):
            n += 1
    return n


def run_health(conn: psycopg.Connection, job: dict, allow_private: bool = False) -> dict:
    from yaseo_app import site_checks
    site = conn.execute("SELECT * FROM sites WHERE id = %s", (job["site_id"],)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = %s", (job["user_id"],)).fetchone()
    if site is None:
        return {"skipped": "сайт удалён"}
    got = site_checks.fetch(site["url"], site_checks.BROWSER_UA, allow_private, False)
    ok = got["status"] is not None and got["status"] < 500
    why = got["error"] or (f"сервер ответил {got['status']}" if not ok else None)
    tls = None
    parts = urlsplit(site["url"])
    if parts.scheme == "https" and parts.hostname:
        tls = site_checks.tls_info(parts.hostname, parts.port or 443)
    tls_until = date.fromisoformat(tls["not_after"][:10]) if tls and tls.get("ok") else None
    prev = conn.execute("SELECT * FROM site_health WHERE site_id = %s", (site["id"],)).fetchone()
    fails = 0 if ok else (prev["fails"] if prev else 0) + 1
    down_since = prev["down_since"] if prev else None
    host = parts.hostname
    if not ok and fails >= DOWN_AFTER and down_since is None:
        down_since = datetime.now(timezone.utc)
        mailer.notify(conn, user, f"Сайт {host} не открывается", "alert_down", "alert",
                      dedupe_key=f"alert:down:{site['id']}:{down_since:%Y-%m-%d-%H}",
                      site=site, host=host, why=why)
    if ok and down_since is not None:
        mailer.notify(conn, user, f"Сайт {host} снова открывается", "alert_up", "alert",
                      dedupe_key=f"alert:up:{site['id']}:{down_since:%Y-%m-%d-%H}",
                      site=site, host=host, since=down_since)
        down_since = None
    if tls_until and (tls_until - date.today()).days <= TLS_ALERT_DAYS:
        mailer.notify(conn, user, f"Сертификат {host} истекает {tls_until:%d.%m.%Y}", "alert_tls",
                      "alert", dedupe_key=f"alert:tls:{site['id']}:{tls_until}",
                      site=site, host=host, until=tls_until, error=None)
    elif tls and not tls.get("ok") and not tls.get("no_tls") and ok:
        mailer.notify(conn, user, f"Сертификат {host} не проходит проверку", "alert_tls",
                      "alert", dedupe_key=f"alert:tls-bad:{site['id']}:{date.today()}",
                      site=site, host=host, until=None, error=tls.get("error"))
    conn.execute(
        """
        INSERT INTO site_health (site_id, checked_at, ok, fails, error, tls_until, down_since)
        VALUES (%s, now(), %s, %s, %s, %s, %s)
        ON CONFLICT (site_id) DO UPDATE SET checked_at = now(), ok = excluded.ok,
            fails = excluded.fails, error = excluded.error, tls_until = excluded.tls_until,
            down_since = excluded.down_since
        """, (site["id"], ok, fails, why, tls_until, down_since))
    return {"ok": ok, "fails": fails, "error": why, "tls_until": str(tls_until or "")}


# --- падение мест ------------------------------------------------------------------

def drops(conn: psycopg.Connection, site_id: int, day: date) -> list[dict]:
    """Сильные падения за сутки: из десятки — вон, или на DROP_PLACES мест и больше.
    Сравниваем с последним съёмом за три дня до этого."""
    rows = conn.execute(
        "SELECT t.query, t.region, p.position AS now,"
        " (SELECT position FROM positions q WHERE q.site_id = t.site_id AND q.query = t.query"
        "   AND q.region = t.region AND q.day < %(d)s AND q.day >= %(d)s - 3"
        "   ORDER BY q.day DESC LIMIT 1) AS before,"
        " (SELECT count(*) FROM positions q WHERE q.site_id = t.site_id AND q.query = t.query"
        "   AND q.region = t.region AND q.day < %(d)s AND q.day >= %(d)s - 3) AS had"
        " FROM tracked_queries t JOIN positions p ON p.site_id = t.site_id"
        "   AND p.query = t.query AND p.region = t.region AND p.day = %(d)s"
        " WHERE t.site_id = %(s)s ORDER BY t.id", {"d": day, "s": site_id}).fetchall()
    out = []
    for r in rows:
        if not r["had"] or r["before"] is None:
            continue
        if r["now"] is None or r["now"] - r["before"] >= DROP_PLACES:
            out.append({"query": r["query"], "before": r["before"], "now": r["now"]})
    return out


def check_positions(conn: psycopg.Connection, user: dict, site: dict, day: date) -> None:
    if not user.get("alerts") or not billing.has(conn, user, "alerts"):
        return
    fell = drops(conn, site["id"], day)
    if not fell:
        return
    host = urlsplit(site["url"]).hostname
    mailer.notify(conn, user, f"{host}: места в Яндексе упали", "alert_positions", "alert",
                  dedupe_key=f"alert:pos:{site['id']}:{day}", site=site, host=host,
                  drops=fell, day=day, region=prefs.region_name(
                      prefs.effective(conn, user, site)["region"]))

