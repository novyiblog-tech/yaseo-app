"""Логика кабинета без веба: пользователи, сессии, сайты, запуск проверок.

Страницы (web.py) только зовут эти функции — если фронт уедет на Next.js, логика останется.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import psycopg

from yaseo_app import jobs, pipeline

SESSION_TTL = timedelta(days=30)
PASSWORD_MIN = 10
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILS = 8
MAX_SITES = 20
MAX_ACTIVE_JOBS = 3
MAX_PAGES = 50
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


class Refused(Exception):
    """Понятный пользователю отказ: текст показывается в форме как есть."""


# --- пароли и сессии ------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt, dk = stored.split("$")
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(got.hex(), dk)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def signup(conn: psycopg.Connection, email: str, password: str, consent: bool) -> dict:
    email = normalize_email(email)
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise Refused("Проверьте адрес почты.")
    if len(password or "") < PASSWORD_MIN:
        raise Refused(f"Пароль — не короче {PASSWORD_MIN} знаков.")
    if not consent:
        raise Refused("Без согласия на обработку персональных данных зарегистрировать нельзя.")
    row = conn.execute(
        "INSERT INTO users (email, password_hash, consent_at) VALUES (%s, %s, now())"
        " ON CONFLICT (email) DO NOTHING RETURNING *",
        (email, hash_password(password)),
    ).fetchone()
    if row is None:
        raise Refused("Этот адрес уже зарегистрирован — войдите.")
    return row


def login(conn: psycopg.Connection, email: str, password: str) -> dict:
    email = normalize_email(email)
    fails = conn.execute(
        "SELECT count(*) AS n FROM login_attempts WHERE email = %s AND NOT ok AND at > now() - %s",
        (email, LOGIN_WINDOW),
    ).fetchone()["n"]
    if fails >= LOGIN_MAX_FAILS:
        raise Refused("Слишком много неудачных попыток. Попробуйте через 15 минут.")
    user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
    ok = bool(user) and check_password(password or "", user["password_hash"])
    if not user:
        hash_password(password or "")  # время ответа не выдаёт, есть ли такой адрес
    conn.execute("INSERT INTO login_attempts (email, ok) VALUES (%s, %s)", (email, ok))
    if not ok:
        raise Refused("Неверная почта или пароль.")
    return user


def open_session(conn: psycopg.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf, expires_at)"
        " VALUES (%s, %s, %s, now() + %s)",
        (_token_hash(token), user_id, secrets.token_urlsafe(24), SESSION_TTL),
    )
    return token


def session_user(conn: psycopg.Connection, token: str | None) -> dict | None:
    """Пользователь сессии с полем csrf, или None."""
    if not token:
        return None
    return conn.execute(
        "SELECT u.*, s.csrf FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = %s AND s.expires_at > now()",
        (_token_hash(token),),
    ).fetchone()


def close_session(conn: psycopg.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))


# --- сайты и проверки -----------------------------------------------------------

def normalize_site_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise Refused("Укажите адрес сайта.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise Refused("Нужен адрес вида example.ru или https://example.ru.")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Refused("Нужен адрес вида example.ru или https://example.ru.")
    host = parts.hostname.encode("idna").decode("ascii") if not parts.hostname.isascii() \
        else parts.hostname
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))


def add_site(conn: psycopg.Connection, user: dict, raw_url: str,
             allow_private: bool = False) -> dict:
    from yaseo import net
    url = normalize_site_url(raw_url)
    try:
        net.check_url(url, allow_private=True if allow_private else None)
    except net.UnsafeURL:
        raise Refused("Этот адрес проверить нельзя: он ведёт во внутреннюю сеть.")
    count = conn.execute("SELECT count(*) AS n FROM sites WHERE user_id = %s",
                         (user["id"],)).fetchone()["n"]
    if count >= MAX_SITES:
        raise Refused(f"В кабинете не больше {MAX_SITES} сайтов.")
    row = conn.execute(
        "INSERT INTO sites (user_id, url) VALUES (%s, %s)"
        " ON CONFLICT (user_id, url) DO UPDATE SET url = excluded.url RETURNING *",
        (user["id"], url),
    ).fetchone()
    return row


def list_sites(conn: psycopg.Connection, user: dict) -> list[dict]:
    return conn.execute(
        """
        SELECT s.*, j.id AS last_job_id, j.status AS last_status, j.finished_at AS last_at
        FROM sites s
        LEFT JOIN LATERAL (
            SELECT id, status, finished_at FROM jobs WHERE site_id = s.id
            ORDER BY id DESC LIMIT 1
        ) j ON true
        WHERE s.user_id = %s ORDER BY s.id
        """,
        (user["id"],),
    ).fetchall()


def get_site(conn: psycopg.Connection, user: dict, site_id: int) -> dict | None:
    """Чужой сайт не отличается от несуществующего."""
    return conn.execute("SELECT * FROM sites WHERE id = %s AND user_id = %s",
                        (site_id, user["id"])).fetchone()


def site_jobs(conn: psycopg.Connection, site_id: int, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT id, status, attempts, error, created_at, finished_at, run_after,"
        " (result->'spend'->>'cost_rub')::numeric AS cost_rub,"
        " result->>'sources_mode' AS sources_mode"
        " FROM jobs WHERE site_id = %s ORDER BY id DESC LIMIT %s",
        (site_id, limit),
    ).fetchall()


def parse_queries(text: str) -> list[str]:
    return pipeline.clean_queries(re.split(r"[\n,;]+", text or ""))


def start_check(conn: psycopg.Connection, user: dict, site: dict, queries_text: str = "",
                max_pages: int = 20) -> int:
    active = conn.execute(
        "SELECT count(*) FILTER (WHERE site_id = %s) AS here, count(*) AS total FROM jobs"
        " WHERE user_id = %s AND status IN ('queued', 'running')",
        (site["id"], user["id"]),
    ).fetchone()
    if active["here"]:
        raise Refused("Проверка этого сайта уже идёт.")
    if active["total"] >= MAX_ACTIVE_JOBS:
        raise Refused(f"Одновременно идёт не больше {MAX_ACTIVE_JOBS} проверок.")
    params = {"url": site["url"], "max_pages": max(1, min(int(max_pages), MAX_PAGES)),
              "queries": parse_queries(queries_text)}
    return jobs.enqueue(conn, user["id"], "audit", params, site_id=site["id"])


def get_job(conn: psycopg.Connection, user: dict, job_id: int) -> dict | None:
    return conn.execute("SELECT * FROM jobs WHERE id = %s AND user_id = %s",
                        (job_id, user["id"])).fetchone()
