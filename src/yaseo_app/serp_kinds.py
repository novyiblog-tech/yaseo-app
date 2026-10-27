"""Кто стоит в выдаче: агрегаторы и справочники или такие же сайты, как у клиента.

Список ведём вручную и коротко: только то, что чаще всего занимает верх выдачи по коммерческим
фразам в России. Неизвестный сайт считаем «обычным»: ошибиться в эту сторону безопаснее.
"""
from __future__ import annotations

# домен (без www) → тип
KINDS = {
    "avito.ru": "агрегатор", "cian.ru": "агрегатор", "domclick.ru": "агрегатор", "domofond.ru": "агрегатор",
    "yandex.ru": "агрегатор", "ya.ru": "агрегатор", "dzen.ru": "агрегатор", "youla.ru": "агрегатор",
    "profi.ru": "агрегатор", "uslugio.com": "агрегатор",
    "ozon.ru": "агрегатор", "wildberries.ru": "агрегатор", "market.yandex.ru": "агрегатор",
    "hh.ru": "агрегатор", "novostroy-m.ru": "агрегатор", "gdeetotdom.ru": "агрегатор",
    "2gis.ru": "справочник", "yell.ru": "справочник", "zoon.ru": "справочник", "flamp.ru": "справочник",
    "otzovik.com": "справочник", "irecommend.ru": "справочник", "spravochnik.ru": "справочник",
    "orgpage.ru": "справочник", "nashdom.rf": "справочник", "наш.дом.рф": "справочник",
    "wikipedia.org": "справочник", "youtube.com": "справочник", "vk.com": "справочник", "rutube.ru": "справочник",
}
ORDER = ("агрегатор", "справочник", "обычный")


def kind(domain: str) -> str:
    d = (domain or "").lower().removeprefix("www.")
    parts = d.split(".")
    for i in range(len(parts) - 1):          # krasnodar.cian.ru → cian.ru
        got = KINDS.get(".".join(parts[i:]))
        if got:
            return got
    return "обычный"


def summarize(items: list[dict]) -> dict | None:
    """Сколько мест в первых тройках заняли агрегаторы и справочники. None — нечего сказать."""
    seen = [d for it in items if "error" not in it for d in it.get("top3") or []]
    if not seen:
        return None
    marked = {}
    for d in seen:
        k = kind(d)
        if k != "обычный":
            marked.setdefault(d, k)
    share = sum(1 for d in seen if kind(d) != "обычный")
    names = [d for d, _ in sorted(marked.items(), key=lambda kv: -seen.count(kv[0]))][:3]
    return {"total": len(seen), "aggregators": share, "names": names, "kinds": marked}
