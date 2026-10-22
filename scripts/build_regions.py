"""Список регионов для настроек сайта из дерева регионов Вордстата (getRegionsTree, бесплатно).

Пишет src/yaseo_app/regions.json: [[код, название], ...]. Название региона — как у Яндекса,
у города — с областью после запятой («Феодосия, Республика Крым»). Запуск: ключи в .env,
    set -a; . ./.env; set +a; .venv/bin/python scripts/build_regions.py
"""
import json
import pathlib

from yaseo import wordstat_client

OUT = pathlib.Path(__file__).resolve().parent.parent / "src" / "yaseo_app" / "regions.json"


#: Федеральные округа в дереве Вордстата: подписью «город, область» они не служат.
DISTRICTS = {"Центр", "Северо-Запад", "Поволжье", "Юг", "Сибирь", "Дальний Восток",
             "Северный Кавказ", "Урал"}


def flatten(tree: dict) -> list[list]:
    root = tree["regions"][0]
    rows: dict[int, str] = {int(root["id"]): root["label"]}

    def walk(node: dict, area: str | None) -> None:
        for ch in node.get("children") or []:
            code = int(ch["id"])
            if code not in rows:
                rows[code] = ch["label"] if area is None else f'{ch["label"]}, {area}'
            if ch["label"] in DISTRICTS:
                walk(ch, None)
            else:
                walk(ch, area or ch["label"])

    walk(root, None)
    return [[c, n] for c, n in sorted(rows.items(), key=lambda r: r[1])]


if __name__ == "__main__":
    data = flatten(wordstat_client.regions_tree())
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(len(data), "регионов →", OUT)
