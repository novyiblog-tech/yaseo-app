"""Сборка отчёта: один HTML для экрана и для PDF.

Вход — результат free_audit и оценка score.assess(). Стили вшиваются в файл, чтобы отчёт
открывался и печатался одним файлом без внешних зависимостей, кроме шрифта.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from yaseo_app import glossary, score, serp_kinds

HERE = Path(__file__).parent
DESIGN = HERE / "design"

VERDICTS = {
    "green": "Основа в порядке. Ниже — что довести до конца.",
    "yellow": "Есть что чинить, но ничего непоправимого. Начните с первого шага.",
    "red": "Сайт мешает сам себе. Первые шаги ниже дадут больше всего.",
    "grey": "Оценить не удалось: сайт не отдал страницы роботу.",
}


def _short(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return path if len(path) <= 60 else path[:57] + "…"


def plural(n, one: str, few: str, many: str) -> str:
    """1 шаг, 2 шага, 5 шагов."""
    n = abs(int(n or 0)) % 100
    if 11 <= n <= 19 or n % 10 == 0 or n % 10 >= 5:
        return many
    return one if n % 10 == 1 else few


def _pages_word(n: int) -> str:
    n = abs(int(n)) % 100
    last = n % 10
    if 11 <= n <= 19 or last == 0 or last >= 5:
        return "страниц"
    return "страница" if last == 1 else "страницы"


def _nbsp_numbers(html: str) -> str:
    """Разряды числа не переносятся между строками."""
    return re.sub(r"(?<=\d) (?=\d{3}\b)", " ", html)


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    env.filters["short"] = _short
    env.filters["pages_word"] = _pages_word
    env.filters["plural"] = plural
    env.filters["num"] = lambda v: f"{v:,}".replace(",", "\u00a0") if isinstance(v, int) else v
    return env


def css() -> str:
    return (DESIGN / "tokens.css").read_text(encoding="utf-8") + "\n" + \
        (DESIGN / "report.css").read_text(encoding="utf-8")


def _issue_rows(result: dict) -> list[dict]:
    roles = {b["token"].lower(): b["role"] for b in result["geo"].get("bots") or []}
    rows = []
    for i in result["audit"].get("issues") or []:
        e = glossary.tech_entry(i["code"])
        rows.append({**i, "title": e.title if e else i["code"]})
    for i in result["geo"].get("issues") or []:
        token = i["code"].removeprefix("robots-blocks-") if i["code"].startswith("robots-blocks-") else ""
        e = glossary.ai_entry(i["code"], roles.get(token))
        rows.append({**i, "title": e.title if e else i["code"]})
    order = {"critical": 0, "major": 1, "minor": 2}
    rows.sort(key=lambda r: (order.get(r["severity"], 9), r["title"], r["url"]))
    return rows


def _ai_brief(result: dict, paid: dict | None) -> dict:
    """Раздел «Нейросети» человеческим языком: можно ли читать сайт, называют ли вас,
    что исправить. Технические таблицы остаются ниже, для разработчика."""
    from collections import Counter

    geo = result["geo"]
    bots = geo.get("bots") or []
    issues = geo.get("issues") or []
    blocked = [f'{b["owner"]} ({b["token"]})' for b in bots if b.get("role") == "search" and not b.get("allowed")]
    home_down = any(i["code"] == "page-unavailable" and i["severity"] == "critical" for i in issues)
    if home_down or not (geo.get("pages") or []):
        readable = ("red", "Нейросети не смогли открыть главную страницу сайта, поэтому оценить доступ не удалось.")
    elif blocked:
        readable = ("red", "Сайт закрыт для нейросетей: " + ", ".join(blocked) +
                    ". Они не смогут показывать его в своих ответах.")
    else:
        readable = ("green", "Нейросети могут читать ваш сайт: доступ им открыт.")

    mention = None
    items = [i for i in ((paid or {}).get("answers") or {}).get("items") or [] if "error" not in i]
    if items:
        cited = sum(1 for i in items if i.get("cited"))
        n = len(items)
        if cited:
            mention = (("green" if cited == n else "yellow"),
                       f"Нейросеть Яндекса назвала ваш сайт в {cited} из {n} ответов.")
        else:
            top = [d for d, _ in Counter(d for i in items for d in i.get("rivals") or []).most_common(3)]
            tail = " Чаще всего вместо вас она называет: " + ", ".join(top) + "." if top else ""
            mention = ("red", f"По всем {n} фразам нейросеть Яндекса ни разу не назвала ваш сайт." + tail
                       if n > 1 else "По вашей фразе нейросеть Яндекса не назвала ваш сайт." + tail)

    roles = {b["token"].lower(): b["role"] for b in bots}
    order = {"critical": 0, "major": 1, "minor": 2}
    groups: dict[str, dict] = {}
    for i in issues:
        token = i["code"].removeprefix("robots-blocks-") if i["code"].startswith("robots-blocks-") else ""
        e = glossary.ai_entry(i["code"], roles.get(token))
        if e is None:
            continue
        g = groups.setdefault(i["code"], {"entry": e, "severity": i["severity"], "count": 0})
        g["count"] += 1
        if order.get(i["severity"], 9) < order.get(g["severity"], 9):
            g["severity"] = i["severity"]
    fixes = sorted(groups.values(), key=lambda g: (order.get(g["severity"], 9), -g["count"]))[:4]
    return {"readable": readable, "mention": mention,
            "fixes": [{"title": g["entry"].title, "problem": g["entry"].problem,
                       "action": g["entry"].action, "effect": g["entry"].effect,
                       "count": g["count"]} for g in fixes]}


def _serp_note(paid: dict | None) -> dict | None:
    items = (((paid or {}).get("positions") or {}).get("items")) or []
    return serp_kinds.summarize(items)


def render(result: dict, assessment: score.Assessment | None = None,
           kind: str = "Бесплатная проверка", max_pages: int = 20,
           back_url: str | None = None, brand: dict | None = None,
           expand: bool = False, paid: dict | None = None, rivals: list | None = None,
           region_label: str | None = None) -> str:
    """expand — раскрыть все выпадающие списки (для PDF). brand — свой логотип и контакты вместо знака yaseo: {"name", "contacts", "logo"},
    logo — data:-адрес картинки."""
    a = assessment or score.assess(result)
    color = score.color_of(a.total)
    collected = datetime.fromisoformat(result["collected_at"]).strftime("%d.%m.%Y %H:%M UTC")
    html = _env().get_template("report.html.j2").render(
        css=css(),
        expand=expand,
        host=urlsplit(result["url"]).hostname or result["url"],
        kind=kind,
        collected_at=collected,
        pages_crawled=result["audit"].get("pages_crawled", 0),
        total=a.total,
        color=color,
        back_url=back_url,
        verdict_text=VERDICTS[color],
        lights=a.lights,
        steps=a.steps,
        geo=result["geo"],
        pages=result["audit"].get("pages") or [],
        issues=_issue_rows(result),
        engine=result.get("engine", ""),
        max_pages=max_pages,
        brand=brand,
        paid=paid,
        rivals=rivals or [],
        region_label=region_label,
        ai=_ai_brief(result, paid),
        speed=(result.get("checks") or {}).get("speed"),
        serp_note=_serp_note(paid),
        serp_kind=serp_kinds.kind,
    )
    return _nbsp_numbers(html)


def write(result: dict, out: Path, **kw) -> Path:
    out.write_text(render(result, **kw), encoding="utf-8")
    return out
