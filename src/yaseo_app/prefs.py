"""Настройки сайта: город, автопроверка, разделы сайта вне проверки, бережный обход,
какие разделы отчёта собирать, конкуренты.

Каждая настройка доступна с тарифа из таблицы features. Сохранённое значение не
стирается, когда тариф падает, — просто перестаёт действовать (effective), а при
возврате тарифа включается снова.
"""
from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from yaseo_app import billing, sources
from yaseo_app.accounts import Refused
from yaseo_app.free_audit import excluded  # noqa: F401 — проверка адреса живёт рядом с обходом

RUSSIA = 225
# Коды регионов геобазы Яндекса (параметр lr выдачи и регион Вордстата). До включения
# живых источников сверить с деревом регионов Вордстата (getRegionsTree).
REGIONS = (
    (225, "Вся Россия"), (213, "Москва"), (2, "Санкт-Петербург"), (65, "Новосибирск"),
    (54, "Екатеринбург"), (43, "Казань"), (47, "Нижний Новгород"), (35, "Краснодар"),
    (56, "Челябинск"), (51, "Самара"), (172, "Уфа"), (39, "Ростов-на-Дону"), (66, "Омск"),
    (62, "Красноярск"), (193, "Воронеж"), (50, "Пермь"), (38, "Волгоград"), (194, "Саратов"),
    (55, "Тюмень"), (44, "Ижевск"), (63, "Иркутск"), (76, "Хабаровск"), (75, "Владивосток"),
    (16, "Ярославль"), (22, "Калининград"), (15, "Тула"), (239, "Сочи"),
)
REGION_NAMES = dict(REGIONS)
SCHEDULES = {"off": "не проверять сам", "week": "раз в неделю", "month": "раз в месяц"}
SECTIONS = ("demand", "positions", "answers")
MAX_EXCLUDE, MAX_RIVALS = 20, 5


def region_name(code: int) -> str:
    return REGION_NAMES.get(code, f"регион {code}")


def effective(conn: psycopg.Connection, user: dict, site: dict) -> dict:
    """Настройки, которые действуют сейчас: недоступное по тарифу — как по умолчанию."""
    f = billing.features(conn, user)
    sections = site.get("sections") or {}
    return {
        "region": site["region"] if f["region"] else RUSSIA,
        "schedule": site["schedule"] if f["schedule"] else "off",
        "exclude": list(site["exclude"] or []) if f["exclude"] else [],
        "gentle": bool(site["gentle"]) and f["gentle"],
        "off": [s for s in SECTIONS if f["sections"] and sections.get(s) is False],
        "rivals": list(site["rivals"] or []) if f["rivals"] else [],
    }


def parse_exclude(text: str, site_url: str) -> list[str]:
    """Строки вида /cart, /catalog/*/filter, *?sort=*. Полный адрес своего сайта
    превращается в путь. Главную исключить нельзя — без неё нечего обходить."""
    from urllib.parse import urlsplit
    own = sources.host(site_url)
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "://" in line:
            parts = urlsplit(line)
            if sources.host(line) != own:
                raise Refused(f"«{line}» — адрес другого сайта.")
            line = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if not line.startswith(("/", "*")) or len(line) > 200:
            raise Refused(f"«{line[:60]}» — нужен путь от корня сайта, например /cart или /catalog/*/filter.")
        if line in ("/", "/*", "*"):
            raise Refused("Весь сайт исключить нельзя.")
        if line not in out:
            out.append(line)
    if len(out) > MAX_EXCLUDE:
        raise Refused(f"Не больше {MAX_EXCLUDE} исключений.")
    return out


def parse_rivals(text: str, site_url: str) -> list[str]:
    own = sources.host(site_url)
    out: list[str] = []
    for raw in (text or "").replace(",", "\n").splitlines():
        line = raw.strip().lower()
        if not line:
            continue
        domain = sources.host(line if "://" in line else "https://" + line)
        if not domain or "." not in domain:
            raise Refused(f"«{raw.strip()[:60]}» — не похоже на адрес сайта.")
        if sources.same_site(domain, own):
            continue
        if domain not in out:
            out.append(domain)
    if len(out) > MAX_RIVALS:
        raise Refused(f"Конкурентов — не больше {MAX_RIVALS}.")
    return out


def save_site(conn: psycopg.Connection, user: dict, site: dict, form: dict) -> None:
    """Сохранить то, что разрешено тарифом. Поля недоступных настроек не трогаем."""
    f = billing.features(conn, user)
    sets: dict = {}
    if f["region"] and "region" in form:
        try:
            region = int(form["region"])
        except (TypeError, ValueError):
            region = -1
        if region not in REGION_NAMES:
            raise Refused("Выберите город из списка.")
        sets["region"] = region
    if f["schedule"] and "schedule" in form:
        if form["schedule"] not in SCHEDULES:
            raise Refused("Выберите, как часто проверять.")
        sets["schedule"] = form["schedule"]
    if f["exclude"] and "exclude" in form:
        sets["exclude"] = Jsonb(parse_exclude(form["exclude"], site["url"]))
    if f["gentle"]:
        sets["gentle"] = form.get("gentle") == "yes"
    if f["sections"]:
        chosen = {s: form.get(f"sec_{s}") == "yes" for s in SECTIONS}
        sets["sections"] = Jsonb(chosen)
    if f["rivals"] and "rivals" in form:
        sets["rivals"] = Jsonb(parse_rivals(form["rivals"], site["url"]))
    if not sets:
        return
    with conn.transaction():
        if "region" in sets and sets["region"] != site["region"]:
            # Фразы под наблюдением переезжают в новый город: там своя выдача и своя история.
            conn.execute("UPDATE tracked_queries SET region = %s WHERE site_id = %s",
                         (sets["region"], site["id"]))
        cols = ", ".join(f"{k} = %s" for k in sets)
        conn.execute(f"UPDATE sites SET {cols} WHERE id = %s AND user_id = %s",
                     (*sets.values(), site["id"], user["id"]))


# --- логотип и контакты в отчёте --------------------------------------------------

MAX_LOGO = 300_000


def _logo_type(data: bytes) -> str | None:
    """Только растровые форматы по сигнатуре файла: SVG может нести скрипт."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_brand(conn: psycopg.Connection, user: dict, name: str, contacts: str,
               logo: bytes | None = None, remove_logo: bool = False) -> None:
    if not billing.has(conn, user, "brand"):
        raise Refused("Свой логотип в отчёте — в тарифах «Про» и «Ультра».")
    name, contacts = (name or "").strip(), (contacts or "").strip()
    if len(name) > 80:
        raise Refused("Название — не длиннее 80 знаков.")
    if len(contacts) > 300:
        raise Refused("Контакты — не длиннее 300 знаков.")
    sets = {"brand_name": name or None, "brand_contacts": contacts or None}
    if logo:
        if len(logo) > MAX_LOGO:
            raise Refused("Логотип — не больше 300 КБ.")
        kind = _logo_type(logo)
        if kind is None:
            raise Refused("Логотип — картинка PNG, JPEG или WebP.")
        sets.update(brand_logo=logo, brand_logo_type=kind)
    elif remove_logo:
        sets.update(brand_logo=None, brand_logo_type=None)
    cols = ", ".join(f"{k} = %s" for k in sets)
    conn.execute(f"UPDATE users SET {cols} WHERE id = %s", (*sets.values(), user["id"]))


def brand(conn: psycopg.Connection, user: dict) -> dict | None:
    """Бренд для отчёта: None — отчёт со знаком yaseo (не задан или не в тарифе)."""
    import base64
    row = conn.execute("SELECT brand_name, brand_contacts, brand_logo, brand_logo_type"
                       " FROM users WHERE id = %s", (user["id"],)).fetchone()
    if not row or not (row["brand_name"] or row["brand_logo"]) \
            or not billing.has(conn, user, "brand"):
        return None
    logo = None
    if row["brand_logo"]:
        logo = (f"data:{row['brand_logo_type']};base64,"
                + base64.b64encode(bytes(row["brand_logo"])).decode())
    return {"name": row["brand_name"], "contacts": row["brand_contacts"], "logo": logo}


# --- ссылка на отчёт без входа ----------------------------------------------------

def share(conn: psycopg.Connection, user: dict, job: dict) -> str:
    import secrets
    if not billing.has(conn, user, "share"):
        raise Refused("Ссылка на отчёт без входа — в тарифах от «Старт».")
    if job["status"] != "done":
        raise Refused("Поделиться можно готовым отчётом.")
    if job.get("share_token"):
        return job["share_token"]
    token = secrets.token_urlsafe(18)
    conn.execute("UPDATE jobs SET share_token = %s WHERE id = %s AND user_id = %s",
                 (token, job["id"], user["id"]))
    return token


def unshare(conn: psycopg.Connection, user: dict, job: dict) -> None:
    conn.execute("UPDATE jobs SET share_token = NULL WHERE id = %s AND user_id = %s",
                 (job["id"], user["id"]))


def shared(conn: psycopg.Connection, token: str) -> tuple[dict, dict] | None:
    """Проверка и её владелец по ссылке. Тариф упал ниже «Старт» — ссылка не работает."""
    if not token or len(token) > 64:
        return None
    job = conn.execute("SELECT * FROM jobs WHERE share_token = %s AND status = 'done'"
                       " AND kind = 'audit'", (token,)).fetchone()
    if job is None:
        return None
    owner = conn.execute("SELECT * FROM users WHERE id = %s", (job["user_id"],)).fetchone()
    if not billing.has(conn, owner, "share"):
        return None
    return job, owner
