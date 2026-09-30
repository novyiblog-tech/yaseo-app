"""Доступ коллегам: второй и третий человек в том же кабинете (тариф «Ультра»).

Коллега — отдельная строка users со своим паролем и owner_id владельца. После входа
сессия работает от кабинета владельца: те же сайты, проверки, тариф. Оплата, настройки
кабинета и список коллег — только у владельца. Тариф владельца упал ниже «Ультра» —
коллеги не входят, пока его не вернут.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import psycopg

from yaseo_app import billing, mailer
from yaseo_app.accounts import EMAIL_RE, PASSWORD_MIN, Refused, hash_password, normalize_email

MAX_MEMBERS = 2
INVITE_TTL = timedelta(days=7)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def members(conn: psycopg.Connection, owner: dict) -> list[dict]:
    return conn.execute("SELECT id, email, email_confirmed_at, created_at FROM users"
                        " WHERE owner_id = %s ORDER BY id", (owner["id"],)).fetchall()


def invite(conn: psycopg.Connection, owner: dict, email: str) -> dict:
    if owner.get("owner_id") or owner.get("actor_id"):
        raise Refused("Приглашать коллег может только владелец кабинета.")
    if not billing.has(conn, owner, "team"):
        raise Refused("Доступ коллегам — в тарифе «Ультра».")
    email = normalize_email(email)
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise Refused("Проверьте адрес почты.")
    if len(members(conn, owner)) >= MAX_MEMBERS:
        raise Refused(f"В кабинете — владелец и до {MAX_MEMBERS} коллег.")
    with conn.transaction():
        row = conn.execute("INSERT INTO users (email, owner_id, weekly_digest, alerts)"
                           " VALUES (%s, %s, false, false) ON CONFLICT (email) DO NOTHING"
                           " RETURNING *", (email, owner["id"])).fetchone()
        if row is None:
            raise Refused("Этот адрес уже зарегистрирован в yaseo. Нужен адрес без своего кабинета.")
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO email_tokens (token_hash, user_id, purpose, expires_at)"
                     " VALUES (%s, %s, 'team', now() + %s)", (_hash(token), row["id"], INVITE_TTL))
    mailer.notify(conn, row, "Приглашение в кабинет yaseo", "team_invite", "team",
                  dedupe_key=f"team:{row['id']}", owner=owner,
                  link=f"{mailer.base_url()}/team/join?t={token}")
    return row


def remove(conn: psycopg.Connection, owner: dict, member_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = %s AND owner_id = %s", (member_id, owner["id"]))


def pending(conn: psycopg.Connection, token: str) -> dict | None:
    return conn.execute(
        "SELECT u.* FROM email_tokens t JOIN users u ON u.id = t.user_id"
        " WHERE t.token_hash = %s AND t.purpose = 'team' AND t.used_at IS NULL"
        " AND t.expires_at > now()", (_hash(token or ""),)).fetchone()


def join(conn: psycopg.Connection, token: str, password: str) -> dict:
    """Коллега задаёт пароль по ссылке из письма: почта этим и подтверждена."""
    if len(password or "") < PASSWORD_MIN:
        raise Refused(f"Пароль — не короче {PASSWORD_MIN} знаков.")
    with conn.transaction():
        user = pending(conn, token)
        if user is None:
            raise Refused("Ссылка устарела или уже использована. Попросите владельца кабинета "
                          "пригласить вас ещё раз.")
        conn.execute("UPDATE email_tokens SET used_at = now() WHERE token_hash = %s",
                     (_hash(token),))
        return conn.execute(
            "UPDATE users SET password_hash = %s, email_confirmed_at = now(), consent_at = now()"
            " WHERE id = %s RETURNING *", (hash_password(password), user["id"])).fetchone()


def as_account(conn: psycopg.Connection, user: dict) -> dict | None:
    """Сессия коллеги работает от кабинета владельца: подменяем строку пользователя на
    владельца и помним, кто вошёл (actor_*). Доступ выключен — None."""
    if not user.get("owner_id"):
        return user
    owner = conn.execute("SELECT * FROM users WHERE id = %s", (user["owner_id"],)).fetchone()
    if owner is None or not billing.has(conn, owner, "team"):
        return None
    return {**owner, "csrf": user.get("csrf"), "actor_id": user["id"],
            "actor_email": user["email"]}
