"""Подтверждение почты и восстановление пароля.

Без подтверждённой почты кабинет открывается, но проверки, наблюдение, покупка
и письма недели не работают: иначе сервис тратил бы деньги и слал письма на адрес,
который человек не доказал своим. Восстановление пароля по ссылке тоже доказывает
адрес — после него почта считается подтверждённой.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta
from pathlib import Path

import psycopg
from jinja2 import Environment, FileSystemLoader, select_autoescape

from yaseo_app import mailer
from yaseo_app.accounts import PASSWORD_MIN, Refused, hash_password, normalize_email

HERE = Path(__file__).parent
TTL = {"confirm": timedelta(days=3), "reset": timedelta(hours=1)}
RESEND_GAP = timedelta(seconds=60)
PER_DAY = 5
SIGNUPS_PER_IP_DAY = 5


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _base() -> str:
    return os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")


def _render(name: str, **ctx) -> tuple[str, str]:
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    return (env.get_template(f"email/{name}.html.j2").render(**ctx),
            env.get_template(f"email/{name}.txt.j2").render(**ctx))


def confirmed(user: dict) -> bool:
    return user.get("email_confirmed_at") is not None


def require_confirmed(user: dict) -> None:
    if not confirmed(user):
        raise Refused("Сначала подтвердите почту — ссылка в письме, которое мы отправили "
                      "при регистрации.")


def check_signup_ip(conn: psycopg.Connection, ip: str | None) -> None:
    if not ip:
        return
    n = conn.execute("SELECT count(*) AS n FROM users WHERE signup_ip = %s"
                     " AND created_at > now() - interval '1 day'", (ip,)).fetchone()["n"]
    if n >= SIGNUPS_PER_IP_DAY:
        raise Refused("С этого адреса сегодня уже зарегистрировано несколько кабинетов. "
                      "Попробуйте завтра.")


def _issue(conn: psycopg.Connection, user: dict, purpose: str) -> str | None:
    """Новый токен или None, если письма этого вида шлём слишком часто."""
    recent = conn.execute(
        "SELECT count(*) AS n, max(created_at) > now() - %s AS too_soon FROM email_tokens"
        " WHERE user_id = %s AND purpose = %s AND created_at > now() - interval '1 day'",
        (RESEND_GAP, user["id"], purpose)).fetchone()
    if recent["too_soon"] or recent["n"] >= PER_DAY:
        return None
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO email_tokens (token_hash, user_id, purpose, expires_at)"
                 " VALUES (%s, %s, %s, now() + %s)",
                 (_hash(token), user["id"], purpose, TTL[purpose]))
    return token


def send_confirmation(conn: psycopg.Connection, user: dict) -> bool:
    if confirmed(user):
        return False
    token = _issue(conn, user, "confirm")
    if token is None:
        return False
    html, text = _render("confirm", link=f"{_base()}/confirm?t={token}")
    mid = mailer.queue(conn, user, "yaSEO: подтвердите почту", html, text, "confirm")
    mailer.send_one(conn, mid)
    return True


def confirm(conn: psycopg.Connection, token: str) -> dict | None:
    row = _use(conn, token, "confirm")
    if row is None:
        return None
    return conn.execute("UPDATE users SET email_confirmed_at = coalesce(email_confirmed_at, now())"
                        " WHERE id = %s RETURNING *", (row["user_id"],)).fetchone()


def _use(conn: psycopg.Connection, token: str, purpose: str) -> dict | None:
    """Токен одноразовый: второй переход по той же ссылке ничего не делает."""
    return conn.execute(
        "UPDATE email_tokens SET used_at = now() WHERE token_hash = %s AND purpose = %s"
        " AND used_at IS NULL AND expires_at > now() RETURNING *",
        (_hash(token or ""), purpose)).fetchone()


def request_reset(conn: psycopg.Connection, email: str) -> None:
    """Ответ одинаковый, есть такой адрес или нет, — по нему нельзя узнать, кто зарегистрирован."""
    user = conn.execute("SELECT * FROM users WHERE email = %s",
                        (normalize_email(email),)).fetchone()
    if user is None:
        return
    token = _issue(conn, user, "reset")
    if token is None:
        return
    html, text = _render("reset", link=f"{_base()}/reset?t={token}")
    mid = mailer.queue(conn, user, "yaSEO: новый пароль", html, text, "reset")
    mailer.send_one(conn, mid)


def reset_valid(conn: psycopg.Connection, token: str) -> bool:
    return conn.execute("SELECT 1 FROM email_tokens WHERE token_hash = %s AND purpose = 'reset'"
                        " AND used_at IS NULL AND expires_at > now()",
                        (_hash(token or ""),)).fetchone() is not None


def reset(conn: psycopg.Connection, token: str, password: str) -> dict:
    if len(password or "") < PASSWORD_MIN:
        raise Refused(f"Пароль — не короче {PASSWORD_MIN} знаков.")
    with conn.transaction():
        row = _use(conn, token, "reset")
        if row is None:
            raise Refused("Ссылка устарела или уже использована. Запросите новую.")
        user = conn.execute(
            "UPDATE users SET password_hash = %s,"
            " email_confirmed_at = coalesce(email_confirmed_at, now())"
            " WHERE id = %s RETURNING *", (hash_password(password), row["user_id"])).fetchone()
        # Новый пароль выкидывает все открытые сессии: если пароль украли, вор выйдет.
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
        conn.execute("DELETE FROM login_attempts WHERE email = %s", (user["email"],))
    return user
