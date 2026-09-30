"""Конвейер аудита: бесплатная часть, потом платные разделы через журнал и кэш.

Бесплатная часть идёт первой: если сайт не открывается, деньги не тратятся.
Платный раздел, упёршийся в выключатель или потолок, не валит задачу — отчёт
собирается без него и говорит почему. Упёрлись в часовую квоту поставщика — задача
откладывается; при повторе уже купленное берётся из кэша бесплатно.
"""
from __future__ import annotations

from datetime import timedelta

import psycopg

from yaseo_app import free_audit, sources
from yaseo_app.ledger import Blocked, Meter, RateLimited

SCHEMA = 2
MAX_QUERIES = 30
SERP_DEPTH = 10
RUSSIA = 225
# Подряд столько отказов поставщика — раздел останавливается: скорее всего ключ
# или доступ, и каждый следующий вызов — деньги впустую.
MAX_ERRORS_IN_ROW = 3

# Порядок важен: Wordstat с часовой квотой первым — если отложимся, то до трат на выдачу.
SECTIONS = (("demand", "wordstat"), ("positions", "yandex-serp"), ("answers", "yandex-gen"))


class Postpone(Exception):
    def __init__(self, message: str, delay: timedelta):
        super().__init__(message)
        self.delay = delay


def clean_queries(raw) -> list[str]:
    seen, out = set(), []
    for q in raw or []:
        q = " ".join(str(q).split()).lower()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:MAX_QUERIES]


def _params(section: str, query: str, domain: str, region: int) -> dict:
    if section == "demand":
        return {"phrase": query, "region": region}
    if section == "positions":
        return {"query": query, "region": region, "depth": SERP_DEPTH, "domain": domain}
    return {"query": query, "domain": domain}


def _item(section: str, query: str, data: dict, domain: str) -> dict:
    if section == "demand":
        return {"query": query, "freq": data["freq"], "top": data["top"][:5]}
    if section == "positions":
        ours = [d["pos"] for d in data["docs"] if sources.same_site(d["domain"], domain)]
        return {"query": query, "position": min(ours) if ours else None,
                "top3": [d["domain"] for d in data["docs"][:3]]}
    cited = [s for s in data["sources"]
             if s["used"] is not False and sources.same_site(s["domain"], domain)]
    rivals = [s["domain"] for s in data["sources"]
              if s["used"] is not False and not sources.same_site(s["domain"], domain)]
    return {"query": query, "cited": bool(cited),
            "cited_position": cited[0]["position"] if cited else None,
            "mentioned": sources.host(domain) in data["text"].lower(),
            "rivals": list(dict.fromkeys(rivals))[:3]}


def _section(conn, meter, src, section, queries, domain, region) -> dict:
    if src is None:
        return {"status": "off", "reason": "источник не подключён", "items": []}
    if not queries:
        return {"status": "skipped", "reason": "нет запросов для проверки", "items": []}
    items, errors, in_row = [], 0, 0
    for q in queries:
        try:
            data = sources.get(conn, meter, src, _params(section, q, domain, region))
        except RateLimited as exc:
            raise Postpone(str(exc), exc.retry_after) from exc
        except Blocked as exc:
            return {"status": "partial" if items else "skipped", "reason": str(exc),
                    "items": items}
        except sources.SourceError as exc:
            items.append({"query": q, "error": str(exc)[:300]})
            errors += 1
            in_row += 1
            if in_row >= MAX_ERRORS_IN_ROW:
                return {"status": "failed", "items": items,
                        "reason": f"поставщик отказал {in_row} раза подряд, раздел остановлен"}
            continue
        in_row = 0
        items.append(_item(section, q, data, domain))
    status = "ok" if not errors else ("failed" if errors == len(items) else "partial")
    return {"status": status, "items": items}


def run_audit(conn: psycopg.Connection, job: dict, user: dict,
              srcs: dict[str, sources.DataSource], allow_private: bool = False) -> dict:
    p = job["params"]
    url = p["url"]
    free = free_audit.run_isolated(url, max_pages=int(p.get("max_pages", 20)),
                                   allow_private=allow_private)
    fake = any(s.fake for s in srcs.values())
    meter = Meter(conn, user["id"], user["plan"], job["id"], fake=fake)
    domain = sources.host(url)
    queries = clean_queries(p.get("queries"))
    region = int(p.get("region", RUSSIA))
    paid = {section: _section(conn, meter, srcs.get(name), section, queries, domain, region)
            for section, name in SECTIONS}
    return {"schema": SCHEMA, "url": url, "sources_mode": "fake" if fake else "live",
            "queries": queries, "region": region, "free": free, "paid": paid,
            "spend": meter.job_summary()}
