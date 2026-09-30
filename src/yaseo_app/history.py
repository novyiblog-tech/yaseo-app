"""История проверок: итог одной проверки, сравнение двух, мини-график для списка сайтов."""
from __future__ import annotations

from yaseo_app import glossary, score


def summarize(result: dict) -> dict:
    a = score.assess(result["free"])
    return {"score": a.total, "lights": {l.key: l.score for l in a.lights}}


def _issues(result: dict) -> dict[tuple[str, str], dict]:
    free = result["free"]
    out = {}
    for i in free["audit"].get("issues") or []:
        e = glossary.tech_entry(i["code"])
        out[(i["code"], i["url"])] = {**i, "title": e.title if e else i["code"]}
    for i in free["geo"].get("issues") or []:
        e = glossary.ai_entry(i["code"])
        out[(i["code"], i["url"])] = {**i, "title": e.title if e else i["code"]}
    return out


def _grouped(items: list[dict]) -> list[dict]:
    """Одинаковые находки на разных страницах — одной строкой с числом страниц."""
    groups: dict[str, dict] = {}
    order = {"critical": 0, "major": 1, "minor": 2}
    for i in items:
        g = groups.setdefault(i["code"], {"code": i["code"], "title": i["title"],
                                          "severity": i["severity"], "pages": 0})
        g["pages"] += 1
    return sorted(groups.values(), key=lambda g: (order.get(g["severity"], 9), -g["pages"]))


def compare(prev: dict, cur: dict) -> dict:
    """Что изменилось с прошлой проверки: находки по паре «код + страница», позиции по запросу."""
    before, after = _issues(prev), _issues(cur)
    fixed = [before[k] for k in before.keys() - after.keys()]
    new = [after[k] for k in after.keys() - before.keys()]

    def positions(r):
        sec = (r.get("paid") or {}).get("positions") or {}
        return {i["query"]: i.get("position") for i in sec.get("items", []) if "error" not in i}

    pb, pa = positions(prev), positions(cur)
    moves = []
    for q in pa.keys() & pb.keys():
        b, a = pb[q], pa[q]
        if b != a:
            # «выше» — меньше номер места; выпасть из топа — хуже любого места
            delta = (b or 99) - (a or 99)
            moves.append({"query": q, "before": b, "after": a, "delta": delta})
    moves.sort(key=lambda m: -abs(m["delta"]))
    sb, sa = summarize(prev)["score"], summarize(cur)["score"]
    return {"fixed": _grouped(fixed), "new": _grouped(new),
            "fixed_count": len(fixed), "new_count": len(new),
            "kept_count": len(after.keys() & before.keys()),
            "score_before": sb, "score_after": sa,
            "score_delta": None if sb is None or sa is None else sa - sb,
            "positions": moves}


def sparkline(scores: list[int | None], width: int = 120, height: int = 28) -> str:
    """SVG-линия оценок, старые слева. Строится из наших чисел, пользовательского текста нет."""
    pts = [s for s in scores if s is not None]
    if len(pts) < 2:
        return ""
    step = width / (len(pts) - 1)
    coords = [f"{i * step:.1f},{height - 2 - (v / 100) * (height - 4):.1f}"
              for i, v in enumerate(pts)]
    last_x, last_y = coords[-1].split(",")
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" aria-hidden="true"><polyline fill="none" '
            f'stroke="currentColor" stroke-width="1.5" points="{" ".join(coords)}"/>'
            f'<circle cx="{last_x}" cy="{last_y}" r="2.5" fill="var(--accent)"/></svg>')
