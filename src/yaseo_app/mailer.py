"""Письма: очередь в таблице outbox и отправитель.

Всё, что уходит пользователю, сначала ложится в outbox с ключом от повтора. Отправитель
забирает очередь: SMTP, если заданы YASEO_SMTP_*, иначе — файлы .eml в build/outbox
для просмотра на машине разработчика (ничего никуда не уходит).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import psycopg

MAX_ATTEMPTS = 5
DEV_DIR = Path(__file__).resolve().parents[2] / "build" / "outbox"


def queue(conn: psycopg.Connection, user: dict, subject: str, html: str, text: str,
          kind: str, dedupe_key: str | None = None) -> int | None:
    row = conn.execute(
        "INSERT INTO outbox (user_id, to_email, subject, html, text, kind, dedupe_key)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (user["id"], user["email"], subject, html, text, kind, dedupe_key),
    ).fetchone()
    return row["id"] if row else None


def _secret() -> bytes:
    s = os.environ.get("YASEO_SECRET")
    if not s:
        # На машине разработчика — постоянный ключ из файла, на сервере — только из окружения.
        path = Path(__file__).resolve().parents[2] / "build" / ".dev-secret"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(os.urandom(32).hex())
        s = path.read_text().strip()
    return s.encode()


def unsubscribe_token(user_id: int) -> str:
    return hmac.new(_secret(), f"unsubscribe:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]


def check_unsubscribe(user_id: int, token: str) -> bool:
    return hmac.compare_digest(unsubscribe_token(user_id), token or "")


class Sender:
    name = ""

    def send(self, msg: EmailMessage) -> None:
        raise NotImplementedError


class DevSender(Sender):
    """Кладёт письмо файлом .eml и .html рядом — открыть и посмотреть глазами."""
    name = "dev"

    def __init__(self, directory: Path = DEV_DIR):
        self.dir = directory

    def send(self, msg):
        self.dir.mkdir(parents=True, exist_ok=True)
        stem = msg["X-Outbox-Id"]
        (self.dir / f"{stem}.eml").write_bytes(bytes(msg))
        html = msg.get_body(("html",))
        if html is not None:
            (self.dir / f"{stem}.html").write_text(html.get_content(), encoding="utf-8")


class SmtpSender(Sender):
    name = "smtp"

    def __init__(self):
        self.host = os.environ["YASEO_SMTP_HOST"]
        self.port = int(os.environ.get("YASEO_SMTP_PORT", "587"))
        self.user = os.environ.get("YASEO_SMTP_USER")
        self.password = os.environ.get("YASEO_SMTP_PASSWORD")

    def send(self, msg):
        with smtplib.SMTP(self.host, self.port, timeout=30) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password or "")
            s.send_message(msg)


def sender() -> Sender:
    return SmtpSender() if os.environ.get("YASEO_SMTP_HOST") else DevSender()


def build(row: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr(("yaseo", os.environ.get("YASEO_MAIL_FROM", "noreply@yaseo.local")))
    msg["To"] = row["to_email"]
    msg["Subject"] = row["subject"]
    msg["Message-ID"] = make_msgid(domain="yaseo")
    msg["X-Outbox-Id"] = str(row["id"])
    if row["user_id"]:
        base = os.environ.get("YASEO_BASE_URL", "http://127.0.0.1:8000")
        link = (f"{base}/unsubscribe?u={row['user_id']}"
                f"&t={unsubscribe_token(row['user_id'])}")
        msg["List-Unsubscribe"] = f"<{link}>"
    msg.set_content(row["text"])
    msg.add_alternative(row["html"], subtype="html")
    return msg


def _deliver(conn: psycopg.Connection, mail_id: int, snd: Sender) -> bool | None:
    """Отправить одно письмо под блокировкой строки: планировщик и веб не отправят его
    дважды. None — письмо уже взял другой процесс или оно не в очереди."""
    with conn.transaction():
        row = conn.execute("SELECT * FROM outbox WHERE id = %s AND status = 'queued'"
                           " AND attempts < %s FOR UPDATE SKIP LOCKED",
                           (mail_id, MAX_ATTEMPTS)).fetchone()
        if row is None:
            return None
        try:
            snd.send(build(row))
        except Exception as exc:  # письмо ждёт следующего прохода
            conn.execute("UPDATE outbox SET attempts = attempts + 1, error = %s,"
                         " status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'queued' END"
                         " WHERE id = %s", (str(exc)[:500], MAX_ATTEMPTS, mail_id))
            return False
        conn.execute("UPDATE outbox SET status = 'sent', sent_at = now(), attempts = attempts + 1"
                     " WHERE id = %s", (mail_id,))
        return True


def send_one(conn: psycopg.Connection, mail_id: int | None, snd: Sender | None = None) -> bool:
    """Отправить сразу — для писем, которых человек ждёт у экрана. Не ушло — останется
    в очереди, и его заберёт планировщик."""
    if mail_id is None:
        return False
    return bool(_deliver(conn, mail_id, snd or sender()))


def send_pending(conn: psycopg.Connection, snd: Sender | None = None, limit: int = 50) -> dict:
    snd = snd or sender()
    out = {"sent": 0, "failed": 0}
    ids = conn.execute("SELECT id FROM outbox WHERE status = 'queued' AND attempts < %s"
                       " ORDER BY id LIMIT %s", (MAX_ATTEMPTS, limit)).fetchall()
    for r in ids:
        done = _deliver(conn, r["id"], snd)
        if done is True:
            out["sent"] += 1
        elif done is False:
            out["failed"] += 1
    return out
