"""Яндекс Вебмастер и Метрика в кабинете: вход через Яндекс ID владельца сайта.

Данные Вебмастера и Метрики не храним — берём при открытии страницы и только показываем
владельцу (условия Яндекса, docs/LEGAL-YANDEX.md). Хранится лишь доступ: токен Яндекс ID.

Нужно приложение на oauth.yandex.ru с правами «Яндекс.Вебмастер: получение информации
о сайтах» и «Яндекс.Метрика: получение статистики», адрес возврата
<YASEO_BASE_URL>/settings/yandex/callback. Ключи — YASEO_YANDEX_CLIENT_ID и
YASEO_YANDEX_CLIENT_SECRET. YASEO_YANDEX_OAUTH=fake — виртуальный Яндекс для разработки
и тестов: вход без перехода и данные-образец.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

import psycopg

AUTHORIZE = "https://oauth.yandex.ru/authorize"
TOKEN = "https://oauth.yandex.ru/token"
LOGIN_INFO = "https://login.yandex.ru/info?format=json"
WEBMASTER = "https://api.webmaster.yandex.net/v4"
METRIKA = "https://api-metrika.yandex.net"
TIMEOUT = 15
DAYS = 28
LAG_DAYS = 2  # Вебмастер сводит данные с отставанием


class YandexError(Exception):
    pass


def fake() -> bool:
    return os.environ.get("YASEO_YANDEX_OAUTH") == "fake"


def configured() -> bool:
    return fake() or bool(os.environ.get("YASEO_YANDEX_CLIENT_ID")
                          and os.environ.get("YASEO_YANDEX_CLIENT_SECRET"))


def authorize_url(state: str, redirect_uri: str) -> str:
    if fake():
        return f"{redirect_uri}?code=fake&state={urllib.parse.quote(state)}"
    return AUTHORIZE + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": os.environ["YASEO_YANDEX_CLIENT_ID"],
        "redirect_uri": redirect_uri, "state": state, "force_confirm": "yes"})


def _request(url: str, token: str | None = None, data: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body)
    if token:
        req.add_header("Authorization", f"OAuth {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise YandexError(f"{e.code}: {e.read(300).decode('utf-8', 'replace')}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise YandexError(str(getattr(e, "reason", e))) from e


def _grant(data: dict) -> dict:
    return _request(TOKEN, data={**data, "client_id": os.environ["YASEO_YANDEX_CLIENT_ID"],
                                 "client_secret": os.environ["YASEO_YANDEX_CLIENT_SECRET"]})


def connect(conn: psycopg.Connection, user: dict, code: str) -> str:
    """Обменять код на токен и запомнить доступ. Возвращает логин на Яндексе."""
    if fake():
        got, login = {"access_token": "fake", "refresh_token": "fake", "expires_in": 3600}, "fake-login"
    else:
        got = _grant({"grant_type": "authorization_code", "code": code})
        login = _request(LOGIN_INFO, got["access_token"]).get("login") or ""
    conn.execute(
        """
        INSERT INTO yandex_links (user_id, access_token, refresh_token, expires_at, login)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s), %s)
        ON CONFLICT (user_id) DO UPDATE SET access_token = excluded.access_token,
            refresh_token = excluded.refresh_token, expires_at = excluded.expires_at,
            login = excluded.login, created_at = now()
        """, (user["id"], got["access_token"], got.get("refresh_token"),
              int(got.get("expires_in") or 0), login))
    return login


def link(conn: psycopg.Connection, user: dict) -> dict | None:
    return conn.execute("SELECT user_id, login, created_at, expires_at FROM yandex_links"
                        " WHERE user_id = %s", (user["id"],)).fetchone()


def disconnect(conn: psycopg.Connection, user: dict) -> None:
    conn.execute("DELETE FROM yandex_links WHERE user_id = %s", (user["id"],))


def _token(conn: psycopg.Connection, user: dict) -> str:
    row = conn.execute("SELECT *, expires_at < now() + interval '1 day' AS stale"
                       " FROM yandex_links WHERE user_id = %s", (user["id"],)).fetchone()
    if row is None:
        raise YandexError("Яндекс не подключён.")
    if row["stale"] and row["refresh_token"] and not fake():
        got = _grant({"grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
        conn.execute("UPDATE yandex_links SET access_token = %s, refresh_token = %s,"
                     " expires_at = now() + make_interval(secs => %s) WHERE user_id = %s",
                     (got["access_token"], got.get("refresh_token") or row["refresh_token"],
                      int(got.get("expires_in") or 0), user["id"]))
        return got["access_token"]
    return row["access_token"]


def _same(url: str, domain: str) -> bool:
    host = urllib.parse.urlsplit(url if "://" in url else "https://" + url).hostname or ""
    return host.lower().removeprefix("www.") == domain


def _webmaster(token: str, domain: str) -> dict:
    uid = _request(f"{WEBMASTER}/user", token)["user_id"]
    hosts = _request(f"{WEBMASTER}/user/{uid}/hosts", token).get("hosts") or []
    host = next((h for h in hosts if _same(h.get("ascii_host_url") or "", domain)), None)
    if host is None:
        return {"found": False}
    # Данные лежат у главного зеркала: у неглавного ответ пустой, а не отказ.
    hid = (host.get("main_mirror") or {}).get("host_id") or host["host_id"]
    path = f"{WEBMASTER}/user/{uid}/hosts/{urllib.parse.quote(hid, safe='')}"
    summary = _request(f"{path}/summary", token)
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=DAYS - 1)
    q = _request(f"{path}/search-queries/popular?order_by=TOTAL_SHOWS"
                 "&query_indicator=TOTAL_SHOWS&query_indicator=TOTAL_CLICKS"
                 "&query_indicator=AVG_SHOW_POSITION"
                 f"&date_from={start}&date_to={end}&limit=20", token)
    queries = [{"query": x.get("query_text"),
                "shows": int((x.get("indicators") or {}).get("TOTAL_SHOWS") or 0),
                "clicks": int((x.get("indicators") or {}).get("TOTAL_CLICKS") or 0),
                "position": (x.get("indicators") or {}).get("AVG_SHOW_POSITION")}
               for x in q.get("queries") or []]
    return {"found": True, "verified": host.get("verified", True),
            "in_search": summary.get("searchable_pages_count"),
            "excluded": summary.get("excluded_pages_count"),
            "problems": summary.get("site_problems") or {},
            "queries": queries, "from": start, "to": end}


def _metrika(token: str, domain: str) -> dict:
    counters = _request(f"{METRIKA}/management/v1/counters", token).get("counters") or []
    counter = next((c for c in counters if _same(c.get("site") or
                                                 (c.get("site2") or {}).get("site") or "", domain)),
                   None)
    if counter is None:
        return {"found": False}
    params = urllib.parse.urlencode({
        "ids": counter["id"], "metrics": "ym:s:visits,ym:s:bounceRate",
        "dimensions": "ym:s:lastTrafficSource", "date1": f"{DAYS}daysAgo", "date2": "yesterday"})
    data = _request(f"{METRIKA}/stat/v1/data?{params}", token)
    by_source = {}
    for row in data.get("data") or []:
        dim = (row.get("dimensions") or [{}])[0]
        by_source[dim.get("id") or dim.get("name")] = {
            "name": dim.get("name"), "visits": int((row.get("metrics") or [0])[0] or 0),
            "bounce": (row.get("metrics") or [0, None])[1]}
    total = sum(v["visits"] for v in by_source.values())
    return {"found": True, "counter": counter["id"], "total": total,
            "organic": by_source.get("organic"), "sources": sorted(
                by_source.values(), key=lambda v: -v["visits"])[:6]}


def _fake_data(domain: str) -> dict:
    end = date.today() - timedelta(days=LAG_DAYS)
    return {
        "webmaster": {"found": True, "verified": True, "in_search": 124, "excluded": 18,
                      "problems": {"POSSIBLE_PROBLEM": 1},
                      "queries": [{"query": f"образец запроса {i}", "shows": 400 - i * 30,
                                   "clicks": 30 - i * 2, "position": 3.5 + i} for i in range(5)],
                      "from": end - timedelta(days=DAYS - 1), "to": end},
        "metrika": {"found": True, "counter": 1, "total": 1840,
                    "organic": {"name": "Переходы из поисковых систем", "visits": 1120, "bounce": 21.4},
                    "sources": [{"name": "Переходы из поисковых систем", "visits": 1120, "bounce": 21.4},
                                {"name": "Прямые заходы", "visits": 520, "bounce": 30.1}]},
    }


def site_data(conn: psycopg.Connection, user: dict, site_url: str) -> dict:
    """Вебмастер и Метрика по сайту — на лету, без записи в базу."""
    from yaseo_app import sources
    domain = sources.host(site_url)
    if fake():
        return _fake_data(domain)
    token = _token(conn, user)
    out: dict = {}
    for key, fn in (("webmaster", _webmaster), ("metrika", _metrika)):
        try:
            out[key] = fn(token, domain)
        except (YandexError, KeyError) as exc:
            out[key] = {"error": str(exc)[:300]}
    return out
