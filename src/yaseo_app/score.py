"""Оценка сайта: одно число, три светофора и шаги «что сделать первым».

Вход — результат free_audit.collect(). Числа считаются только из измеренного: каждая
находка движка несёт цитату-доказательство, справочные строки в оценку не входят.

Светофоры:
- техника — обход сайта (audit.issues);
- нейросети — готовность к ИИ-поиску (geo.issues);
- Яндекс — видимость в выдаче; в бесплатном аудите не измеряется и остаётся серым.

Общая оценка — среднее измеренных светофоров с весами. Неизмеренный светофор
в среднее не входит и в отчёте так и подписывается.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from yaseo_app import glossary

# Вес находки. Одна критическая стоит десяти мелких.
WEIGHT = {"critical": 10.0, "major": 3.0, "minor": 1.0}

# Штраф «на страницу», при котором оценка техники падает до нуля:
# страница, у которой всё плохо, набирает примерно столько.
TECH_FULL_PENALTY_PER_PAGE = 10.0

# Готовность к ИИ-поиску проверяется на 3–4 страницах, находок мало,
# поэтому штраф считается на сайт целиком.
AI_FULL_PENALTY = 25.0

LIGHT_GREEN = 80
LIGHT_YELLOW = 50

# Доля светофора в общей оценке; пересчитывается по тем, что измерены.
SHARE = {"tech": 0.4, "yandex": 0.3, "ai": 0.3}


@dataclass
class Light:
    key: str
    name: str
    score: int | None          # 0–100, None — не измерено
    color: str                 # green | yellow | red | grey
    counts: dict[str, int]     # находок по severity
    note: str = ""


@dataclass
class Step:
    """Один шаг «что сделать первым»: группа одинаковых находок."""
    code: str
    area: str                  # tech | ai
    severity: str
    title: str
    problem: str
    action: str
    who: str
    time: str
    effect: str
    pages: int                 # на скольких страницах
    urls: list[str]
    evidence: str              # доказательство с первой страницы
    weight: float = 0.0


@dataclass
class Assessment:
    total: int | None
    lights: list[Light]
    steps: list[Step]
    measured: list[str] = field(default_factory=list)



def _pages(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19 or n % 10 == 0 or n % 10 >= 5:
        return "страниц"
    return "страница" if n % 10 == 1 else "страницы"

def color_of(score: int | None) -> str:
    if score is None:
        return "grey"
    if score >= LIGHT_GREEN:
        return "green"
    if score >= LIGHT_YELLOW:
        return "yellow"
    return "red"


def _counts(issues: list[dict]) -> dict[str, int]:
    counts = {"critical": 0, "major": 0, "minor": 0}
    for issue in issues:
        if issue["code"] in glossary.INFORMATIONAL:
            continue
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    return counts


def _penalty(issues: list[dict]) -> float:
    return sum(WEIGHT.get(i["severity"], 1.0) for i in issues
               if i["code"] not in glossary.INFORMATIONAL)


def tech_light(audit: dict) -> Light:
    pages = int(audit.get("pages_crawled") or 0)
    issues = audit.get("issues") or []
    if audit.get("error") or pages == 0:
        return Light("tech", "Техника", None, "grey", _counts(issues),
                     audit.get("error") or "сайт не обошёлся")
    ratio = _penalty(issues) / (pages * TECH_FULL_PENALTY_PER_PAGE)
    score = round(100 * max(0.0, 1.0 - ratio))
    return Light("tech", "Техника", score, color_of(score), _counts(issues),
                 f"проверено: {pages} {_pages(pages)}")


def ai_light(geo: dict) -> Light:
    issues = geo.get("issues") or []
    pages = geo.get("pages") or []
    home_down = any(i["code"] == "page-unavailable" and i["severity"] == "critical"
                    for i in issues)
    if not pages or home_down:
        return Light("ai", "Нейросети", None, "grey", _counts(issues),
                     "главная страница не открылась")
    ratio = _penalty(issues) / AI_FULL_PENALTY
    score = round(100 * max(0.0, 1.0 - ratio))
    return Light("ai", "Нейросети", score, color_of(score), _counts(issues),
                 f"проверено: {len(pages)} {_pages(len(pages))}")


def yandex_light(result: dict) -> Light:
    return Light("yandex", "Яндекс", None, "grey", {"critical": 0, "major": 0, "minor": 0},
                 "в бесплатной проверке не измеряется")


def total_score(lights: list[Light]) -> int | None:
    measured = [(SHARE[l.key], l.score) for l in lights if l.score is not None]
    if not measured:
        return None
    weight = sum(w for w, _ in measured)
    return round(sum(w * s for w, s in measured) / weight)


def _bot_roles(geo: dict) -> dict[str, str]:
    return {b["token"].lower(): b["role"] for b in geo.get("bots") or []}


def build_steps(result: dict, limit: int = 7) -> list[Step]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for issue in result["audit"].get("issues") or []:
        if issue["code"] not in glossary.INFORMATIONAL:
            groups[("tech", issue["code"])].append(issue)
    for issue in result["geo"].get("issues") or []:
        groups[("ai", issue["code"])].append(issue)

    roles = _bot_roles(result["geo"])
    steps: list[Step] = []
    for (area, code), issues in groups.items():
        first = issues[0]
        if area == "tech":
            entry = glossary.tech_entry(code)
        else:
            token = code.removeprefix("robots-blocks-") if code.startswith("robots-blocks-") else ""
            entry = glossary.ai_entry(code, roles.get(token))
        if entry is None:
            # Код, которого словарь не знает: показываем как есть, тест словаря это поймает.
            entry = glossary.Entry(code, first["evidence"], first["fix"],
                                   glossary.DEV, "не оценено", "")
        urls = sorted({i["url"] for i in issues})
        weight = sum(WEIGHT.get(i["severity"], 1.0) for i in issues)
        steps.append(Step(code, area, first["severity"], entry.title, entry.problem,
                          entry.action, entry.who, entry.time, entry.effect,
                          len(urls), urls, first["evidence"], weight))

    order = {"critical": 0, "major": 1, "minor": 2}
    steps.sort(key=lambda s: (order.get(s.severity, 9), -s.weight, s.code))
    return steps[:limit]


STUB_NOTE = "сайт показал роботу заглушку защиты — оценка была бы недостоверной"


def assess(result: dict, limit: int = 7) -> Assessment:
    lights = [tech_light(result["audit"]), yandex_light(result), ai_light(result["geo"])]
    access = (result.get("checks") or {}).get("access") or {}
    if access.get("verdict") == "stub":
        # Обход видел заглушку, а не сайт: находки по страницам — про заглушку.
        for l in lights:
            if l.score is not None:
                l.score, l.color, l.note = None, "grey", STUB_NOTE
    return Assessment(total_score(lights), lights, build_steps(result, limit),
                      [l.key for l in lights if l.score is not None])
