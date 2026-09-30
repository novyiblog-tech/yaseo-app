"""Наблюдение за позициями: запросы сайта, ежедневный съём, история по дням.

Съём ставится планировщиком раз в сутки ночью по Москве — отложенные запросы ночью
дешевле (PLAN.md §3). Одна задача на сайт в день: повторная постановка — пустая операция.
Сколько запросов снимать, решает тариф на момент съёма: упал тариф — снимаем меньше,
запросы сверх лимита остаются в списке и ждут.
"""
from __future__ import annotations

from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb

from yaseo_app import billing, history, jobs, pipeline, sources
from yaseo_app.accounts import Refused, parse_queries
from yaseo_app.ledger import Blocked, Meter

DEPTH = 10  # глубина съёма; больше — дороже в разы (страница выдачи — одно обращение)
RUSSIA = 225


def msk_today(conn: psycopg.Connection) -> date:
    return conn.execute("SELECT (now() AT TIME ZONE 'Europe/Moscow')::date AS d").fetchone()["d"]


# --- запросы под наблюдением ----------------------------------------------------------

def tracked(conn: psycopg.Connection, site_id: int) -> list[dict]:
    return conn.execute("SELECT * FROM tracked_queries WHERE site_id = %s ORDER BY id",
                        (site_id,)).fetchall()


def quota(conn: psycopg.Connection, user: dict) -> dict:
    limit = billing.current(conn, user)["plan"]["tracked_queries"] or 0
    used = conn.execute("SELECT count(*) AS n FROM tracked_queries WHERE user_id = %s",
                        (user["id"],)).fetchone()["n"]
    return {"limit": limit, "used": used, "left": max(0, limit - used)}


def add_queries(conn: psycopg.Connection, user: dict, site: dict, text: str,
                region: int = RUSSIA) -> int:
    q = quota(conn, user)
    if q["limit"] == 0:
        raise Refused("Наблюдение за позициями — в тарифах «Старт» и выше.")
    wanted = parse_queries(text)
    if not wanted:
        raise Refused("Впишите запросы, по одному в строке.")
    existing = {r["query"] for r in tracked(conn, site["id"])}
    fresh = [w for w in wanted if w not in existing]
    if len(fresh) > q["left"]:
        raise Refused(f"По тарифу под наблюдением до {q['limit']} запросов, свободно "
                      f"{q['left']}. Уберите лишние или перейдите на тариф выше.")
    for w in fresh:
        conn.execute("INSERT INTO tracked_queries (user_id, site_id, query, region)"
                     " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                     (user["id"], site["id"], w, region))
    return len(fresh)


def remove_query(conn: psycopg.Connection, user: dict, query_id: int) -> None:
    conn.execute("DELETE FROM tracked_queries WHERE id = %s AND user_id = %s",
                 (query_id, user["id"]))


# --- ежедневный съём ----------------------------------------------------------------

def schedule_daily(conn: psycopg.Connection, day: date | None = None) -> int:
    """Поставить съём на сегодня всем сайтам с запросами. Возвращает, сколько поставлено."""
    day = day or msk_today(conn)
    rows = conn.execute(
        "SELECT DISTINCT t.site_id, t.user_id FROM tracked_queries t"
        " JOIN sites s ON s.id = t.site_id").fetchall()
    n = 0
    for r in rows:
        if jobs.enqueue(conn, r["user_id"], "positions", {"day": day.isoformat()},
                        site_id=r["site_id"], dedupe_key=f"positions:{r['site_id']}:{day}"):
            n += 1
    return n


def _allowed(conn: psycopg.Connection, user: dict, site_id: int) -> list[dict]:
    """Запросы сайта в пределах тарифа: по всему кабинету, в порядке добавления."""
    limit = quota(conn, user)["limit"]
    return conn.execute(
        "SELECT * FROM (SELECT t.*, row_number() OVER (ORDER BY id) AS rn"
        " FROM tracked_queries t WHERE user_id = %s) x WHERE site_id = %s AND rn <= %s"
        " ORDER BY id", (user["id"], site_id, limit)).fetchall()


def run_positions(conn: psycopg.Connection, job: dict, user: dict,
                  srcs: dict[str, sources.DataSource]) -> dict:
    site = conn.execute("SELECT * FROM sites WHERE id = %s", (job["site_id"],)).fetchone()
    if site is None:
        return {"checked": 0, "reason": "сайт удалён"}
    day = date.fromisoformat(job["params"]["day"])
    src = srcs.get("yandex-serp")
    queries = _allowed(conn, user, site["id"])
    fake = any(s.fake for s in srcs.values())
    meter = Meter(conn, user["id"], user["plan"], job["id"], fake=fake)
    domain = sources.host(site["url"])
    checked, errors, reason = 0, 0, None
    for q in queries:
        params = {"query": q["query"], "region": q["region"], "depth": DEPTH, "domain": domain}
        try:
            data = sources.get(conn, meter, src, params)
        except Blocked as exc:
            reason = str(exc)
            break
        except sources.SourceError:
            errors += 1
            if errors >= pipeline.MAX_ERRORS_IN_ROW and not checked:
                reason = "поставщик не отвечает"
                break
            continue
        ours = [d for d in data["docs"] if sources.same_site(d["domain"], domain)]
        best = min(ours, key=lambda d: d["pos"]) if ours else None
        conn.execute(
            "INSERT INTO positions (site_id, query, region, day, position, url, top3)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (site_id, query, region, day) DO UPDATE SET position = excluded.position,"
            " url = excluded.url, top3 = excluded.top3, checked_at = now()",
            (site["id"], q["query"], q["region"], day, best["pos"] if best else None,
             best["url"] if best else None,
             Jsonb([d["domain"] for d in data["docs"][:3]])))
        checked += 1
    return {"day": day.isoformat(), "checked": checked, "errors": errors,
            "skipped": len(queries) - checked - errors, "reason": reason,
            "sources_mode": "fake" if fake else "live", "spend": meter.job_summary()}


# --- показ ----------------------------------------------------------------------------

def _as_score(pos: int | None) -> int:
    """Место → число для графика, где выше — лучше: 1-е место 100, вне топа 0."""
    return 0 if pos is None else max(0, (DEPTH + 1 - pos) * 100 // DEPTH)


def site_table(conn: psycopg.Connection, site_id: int, today: date, days: int = 30) -> list[dict]:
    """По каждому запросу: место сегодня, неделю назад, изменение и график за месяц."""
    rows = conn.execute(
        "SELECT query, region, day, position FROM positions WHERE site_id = %s AND day > %s"
        " ORDER BY day", (site_id, today - timedelta(days=days))).fetchall()
    series: dict[tuple, dict] = {}
    for r in rows:
        series.setdefault((r["query"], r["region"]), {})[r["day"]] = r["position"]
    out = []
    for t in tracked(conn, site_id):
        s = series.get((t["query"], t["region"]), {})
        last_day = max(s) if s else None
        now = s.get(last_day) if last_day else None
        week = s.get(last_day - timedelta(days=7)) if last_day else None
        has_week = last_day is not None and (last_day - timedelta(days=7)) in s
        delta = ((week or DEPTH + 1) - (now or DEPTH + 1)) if has_week else None
        out.append({"id": t["id"], "query": t["query"], "position": now, "day": last_day,
                    "week_ago": week, "delta": delta,
                    "spark": history.sparkline([_as_score(s[d]) for d in sorted(s)])})
    return out


def changes(conn: psycopg.Connection, site_id: int, today: date) -> list[dict]:
    """Сдвиги за неделю для письма: последний съём против съёма неделей раньше."""
    return [r for r in site_table(conn, site_id, today, days=15) if r["delta"]]
