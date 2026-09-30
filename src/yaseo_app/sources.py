"""Платные источники данных: один интерфейс, живые и виртуальные реализации.

Живой источник зовёт движок с ключами, переданными словарём, — окружение процесса не
трогается. Виртуальный отдаёт правдоподобные, но выдуманные данные той же формы:
на нём весь конвейер, лимиты и журнал проверяются без ключей и без трат. Результат
задачи несёт пометку `fake`, чтобы выдуманные данные не ушли клиенту как настоящие.

В кэш и в результат идёт только нужная отчёту выжимка, а не сырые ответы поставщика.
"""
from __future__ import annotations

import hashlib
import json

import psycopg
from psycopg.types.json import Jsonb

from yaseo_app.ledger import Meter

SEARCH_KEYS = ("YC_FOLDER_ID", "YANDEX_AI_STUDIO_API_KEY")


class SourceError(Exception):
    """Поставщик не ответил или ответил ошибкой. Раздел отчёта помечается, задача живёт."""


def host(value: str) -> str:
    value = (value or "").strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split(":", 1)[0]
    return value.removeprefix("www.")


def same_site(domain: str, ours: str) -> bool:
    d, o = host(domain), host(ours)
    return bool(d) and (d == o or d.endswith("." + o))


class DataSource:
    name = ""
    fake = False

    def units(self, params: dict) -> int:
        return 1

    def key(self, params: dict) -> str:
        return json.dumps(params, ensure_ascii=False, sort_keys=True)

    def fetch(self, params: dict) -> dict:  # pragma: no cover — интерфейс
        raise NotImplementedError


def get(conn: psycopg.Connection, meter: Meter, src: DataSource, params: dict) -> dict:
    """Кэш → резерв в журнале → вызов → кэш. Выключенный источник не отдаёт и кэш."""
    ttl = meter.check(src.name)["cache_ttl"]
    key = src.key(params)
    units = src.units(params)
    hit = conn.execute(
        "SELECT payload FROM cache WHERE source = %s AND key = %s AND fake = %s"
        " AND expires_at > now()",
        (src.name, key, src.fake),
    ).fetchone()
    if hit:
        meter.cached(src.name, units, key)
        return hit["payload"]

    spend_id = meter.reserve(src.name, units, key)
    try:
        data = src.fetch(params)
    except Exception as exc:
        meter.settle(spend_id, False, f"{key} · {type(exc).__name__}: {exc}"[:500])
        raise SourceError(str(exc)) from exc
    meter.settle(spend_id, True)
    conn.execute(
        "INSERT INTO cache (source, key, fake, payload, expires_at)"
        " VALUES (%s, %s, %s, %s, now() + %s)"
        " ON CONFLICT (source, key, fake) DO UPDATE"
        " SET payload = excluded.payload, fetched_at = now(), expires_at = excluded.expires_at",
        (src.name, key, src.fake, Jsonb(data), ttl),
    )
    return data


# --- живые источники ---------------------------------------------------------------

class LiveSerp(DataSource):
    """Выдача Яндекса. Ключ кэша не зависит от сайта: одна выдача годится всем."""
    name = "yandex-serp"

    def __init__(self, keys: dict):
        self.keys = keys

    def units(self, params: dict) -> int:
        from yaseo import yandex_serp
        return yandex_serp.pages_for(params["depth"])

    def key(self, params: dict) -> str:
        return super().key({k: params[k] for k in ("query", "region", "depth")})

    def fetch(self, params: dict) -> dict:
        from yaseo import yandex_serp
        res = yandex_serp.serp(params["query"], region=int(params["region"]),
                               n=params["depth"], env=self.keys)
        if res.error:
            raise SourceError(res.error)
        return {"found": res.found_all,
                "docs": [{"pos": d.organic_position, "domain": d.domain, "url": d.url}
                         for d in res.docs if not d.is_wizard]}


class LiveWordstat(DataSource):
    name = "wordstat"

    def __init__(self, keys: dict):
        self.keys = keys

    def fetch(self, params: dict) -> dict:
        from yaseo import wordstat, wordstat_client
        region = str(params["region"])
        raw = wordstat_client.top_requests(params["phrase"], regions=[region], num=50,
                                           env=self.keys)
        freq = wordstat._parse(params["phrase"], region, raw)
        if freq.error:
            raise SourceError(freq.error)
        return {"freq": freq.freq,
                "top": [{"phrase": e.phrase, "freq": e.freq}
                        for e in freq.expansions if e.kind == "top"][:20]}


class LiveYandexGen(DataSource):
    """Ответ YandexGPT поверх Поиска. В кэше — источники ответа и начало текста;
    цитируют ли наш сайт, считается после кэша, по домену задачи."""
    name = "yandex-gen"

    def __init__(self, keys: dict):
        self.keys = keys

    def key(self, params: dict) -> str:
        return super().key({"query": params["query"]})

    def fetch(self, params: dict) -> dict:
        from yaseo.geo import providers
        p = providers.get("yandex")
        p._pace()
        try:
            raw = p.call(params["query"], self.keys)
        except providers.ProviderError as exc:
            raise SourceError(str(exc)) from exc
        ans = p.parse(raw, params["query"], params.get("domain", ""))
        if ans.error:
            raise SourceError(ans.error)
        return {"text": ans.text[:providers.EXCERPT_CHARS],
                "sources": [{"domain": s.domain, "url": s.url, "used": s.used,
                             "position": s.position} for s in ans.sources]}


# --- виртуальные источники ----------------------------------------------------------

RIVALS = ("avito.ru", "2gis.ru", "yell.ru", "zoon.ru", "otzovik.com", "pikabu.ru",
          "vc.ru", "dzen.ru", "profi.ru", "irecommend.ru", "tinkoff.ru", "rbc.ru")


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


class FakeSerp(LiveSerp):
    fake = True

    def __init__(self):
        super().__init__({})

    def key(self, params: dict) -> str:
        return DataSource.key(self, params)

    def fetch(self, params: dict) -> dict:
        s = _seed("serp", params["query"], params["region"])
        ours = _seed("pos", params["query"], params["domain"]) % 16  # 0 или >depth — нет в топе
        docs, rivals = [], list(RIVALS)
        for pos in range(1, params["depth"] + 1):
            if pos == ours:
                docs.append({"pos": pos, "domain": host(params["domain"]),
                             "url": f"http://{host(params['domain'])}/"})
            else:
                d = rivals[(s + pos) % len(rivals)]
                docs.append({"pos": pos, "domain": d, "url": f"https://{d}/p{pos}"})
        return {"found": 1000 + s % 90000, "docs": docs}


class FakeWordstat(LiveWordstat):
    fake = True

    def __init__(self):
        super().__init__({})

    def fetch(self, params: dict) -> dict:
        s = _seed("ws", params["phrase"], params["region"])
        base = 50 + s % 5000
        tails = ("цена", "купить", "отзывы", "недорого", "рядом", "официальный сайт")
        return {"freq": base,
                "top": [{"phrase": f"{params['phrase']} {t}", "freq": base // (i + 2)}
                        for i, t in enumerate(tails)]}


class FakeYandexGen(LiveYandexGen):
    fake = True

    def __init__(self):
        super().__init__({})

    def key(self, params: dict) -> str:
        return DataSource.key(self, {"query": params["query"], "domain": params["domain"]})

    def fetch(self, params: dict) -> dict:
        s = _seed("gen", params["query"], params["domain"])
        srcs = [{"domain": RIVALS[(s + i) % len(RIVALS)],
                 "url": f"https://{RIVALS[(s + i) % len(RIVALS)]}/a", "used": True,
                 "position": i + 1} for i in range(4)]
        if s % 3 == 0:
            srcs.insert(1, {"domain": host(params["domain"]), "url": params["domain"],
                            "used": True, "position": 2})
        return {"text": f"Виртуальный ответ на «{params['query']}».", "sources": srcs}


def build(mode: str, keys: dict | None = None) -> dict[str, DataSource]:
    """mode: fake — виртуальные данные, live — настоящие поставщики по ключам."""
    if mode == "fake":
        return {s.name: s for s in (FakeSerp(), FakeWordstat(), FakeYandexGen())}
    if mode != "live":
        raise ValueError(f"режим источников «{mode}»: нужен fake или live")
    keys = keys or {}
    missing = [k for k in SEARCH_KEYS if not keys.get(k)]
    if missing:
        raise ValueError("для живых источников нет ключей: " + ", ".join(missing))
    return {s.name: s for s in (LiveSerp(keys), LiveWordstat(keys), LiveYandexGen(keys))}
