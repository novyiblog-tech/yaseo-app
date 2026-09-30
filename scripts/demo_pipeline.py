"""Прогон конвейера на тестовом сайте с виртуальными источниками: ни ключей, ни трат.

    uv run python scripts/demo_pipeline.py [запрос ...]

База — YASEO_APP_DSN (по умолчанию dbname=yaseo_app, создаётся сама). Запустите дважды:
второй прогон берёт всё из кэша, и в журнале видно, сколько кэш сэкономил.
"""
import functools
import http.server
import sys
import threading
from pathlib import Path

import psycopg

from yaseo_app import db, jobs, sources, worker

SITE = Path(__file__).resolve().parent.parent / "tests" / "site"
QUERIES = ["ремонт квартир", "дизайн интерьера", "отделка под ключ", "ремонт ванной"]


def ensure_db() -> None:
    try:
        db.connect().close()
    except psycopg.OperationalError:
        name = db.dsn().split("dbname=", 1)[1].split()[0]
        with psycopg.connect("dbname=postgres", autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{name}"')


def main() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    ensure_db()
    conn = db.connect()
    db.migrate(conn)
    user = conn.execute(
        "INSERT INTO users (email) VALUES ('demo@local') ON CONFLICT (email)"
        " DO UPDATE SET email = excluded.email RETURNING *").fetchone()
    jid = jobs.enqueue(conn, user["id"], "audit",
                       {"url": url, "max_pages": 10, "queries": sys.argv[1:] or QUERIES})
    while worker.run_once(conn, "demo", sources.build("fake"), allow_private=True):
        pass
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone()
    server.shutdown()

    print(f"задача {jid}: {job['status']}" + (f" — {job['error']}" if job["error"] else ""))
    if not job["result"]:
        return 1
    r = job["result"]
    audit = r["free"]["audit"]
    print(f"сайт {url}: страниц {audit['pages_crawled']}, находок {len(audit['issues'])}")
    for name, sec in r["paid"].items():
        print(f"\n[{name}] {sec['status']}" + (f" — {sec['reason']}" if sec.get("reason") else ""))
        for it in sec["items"]:
            print("  ", {k: v for k, v in it.items() if k != "top"})
    s = r["spend"]
    print(f"\nрасход задачи ({'виртуальный' if s['fake'] else 'настоящий'}): {s['cost_rub']:.2f} ₽")
    for src, v in s["by_source"].items():
        print(f"   {src:12} обращений {v['calls']:>3}, из кэша {v['cache_hits']:>3},"
              f" {v['cost_rub']:.2f} ₽, кэш сэкономил {v['saved_rub']:.2f} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
