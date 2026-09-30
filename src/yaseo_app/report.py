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

from yaseo_app import glossary, score

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


def render(result: dict, assessment: score.Assessment | None = None,
           kind: str = "Бесплатная проверка", max_pages: int = 20,
           back_url: str | None = None, brand: dict | None = None) -> str:
    """brand — свой логотип и контакты вместо знака yaseo: {"name", "contacts", "logo"},
    logo — data:-адрес картинки."""
    a = assessment or score.assess(result)
    color = score.color_of(a.total)
    collected = datetime.fromisoformat(result["collected_at"]).strftime("%d.%m.%Y %H:%M UTC")
    html = _env().get_template("report.html.j2").render(
        css=css(),
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
    )
    return _nbsp_numbers(html)


def write(result: dict, out: Path, **kw) -> Path:
    out.write_text(render(result, **kw), encoding="utf-8")
    return out
