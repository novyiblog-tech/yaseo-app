"""Скорость сайта на телефоне: бесплатный API Google PageSpeed Insights.

Один запрос на проверку, только главная страница. Ключ YASEO_PAGESPEED_KEY необязателен,
но без него лимит общий на всех и ответ 429 бывает часто — тогда раздел просто пропускаем,
остальной отчёт строится. Приватные адреса и виртуальный режим не трогаем: Google до них
не дотянется.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
TIMEOUT = 60

SLOW_BAD = 50     # ниже — серьёзно
SLOW_OK = 90      # ниже — можно лучше
SHIFT_BAD = 0.25  # сдвиг вёрстки выше — страница «прыгает»


def fetch(url: str, key: str | None = None, timeout: int = TIMEOUT) -> dict:
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if key:
        params["key"] = key
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "yaseo-pagespeed/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — фиксированный https-адрес Google
        return json.loads(resp.read().decode("utf-8"))


def parse(data: dict) -> dict:
    """Из ответа Google — только то, что показываем: оценка 0–100, секунды, сдвиг, блокировка."""
    lh = data.get("lighthouseResult") or {}
    perf = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    if perf is None:
        return {"status": "unavailable", "reason": "Google не вернул оценку скорости"}
    audits = lh.get("audits") or {}

    def num(name: str):
        return (audits.get(name) or {}).get("numericValue")

    lcp, cls, tbt = num("largest-contentful-paint"), num("cumulative-layout-shift"), num("total-blocking-time")
    return {"status": "ok", "strategy": "mobile", "score": round(perf * 100),
            "lcp_s": round(lcp / 1000, 1) if lcp is not None else None,
            "cls": round(cls, 2) if cls is not None else None,
            "tbt_ms": round(tbt) if tbt is not None else None}


def check(url: str, key: str | None = None, fetcher=fetch) -> dict:
    """Результат замера; любой сбой превращается в status=unavailable, проверку не роняем."""
    try:
        return parse(fetcher(url, key or os.environ.get("YASEO_PAGESPEED_KEY") or None))
    except Exception as exc:  # сеть, 429, таймаут, не тот формат
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:200]}


def issues(res: dict, url: str) -> list[dict]:
    """Находки в формате движка: серьёзность, код, адрес, доказательство, что сделать."""
    if res.get("status") != "ok":
        return []
    out = []
    score = res["score"]
    if score < SLOW_OK:
        lcp = f", главное содержимое появляется за {res['lcp_s']} с".replace(".", ",") if res.get("lcp_s") else ""
        out.append({"severity": "major" if score < SLOW_BAD else "minor", "code": "slow-mobile", "url": url,
                    "evidence": f"оценка скорости на телефоне {score} из 100{lcp}",
                    "fix": "Сжать картинки, убрать лишние скрипты, включить кэш и сжатие на сервере."})
    if res.get("cls") is not None and res["cls"] > SHIFT_BAD:
        out.append({"severity": "major", "code": "layout-shift", "url": url,
                    "evidence": f"страница сдвигается при загрузке: показатель {res['cls']} (норма до 0,1)".replace(".", ","),
                    "fix": "Задать размеры картинкам и рекламным блокам, не вставлять блоки выше уже показанного."})
    return out
