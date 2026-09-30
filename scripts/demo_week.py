"""Машина разработчика: дорисовать две недели выдуманных позиций и собрать письмо недели.

    uv run python scripts/demo_week.py

Только для виртуального режима: скрипт отказывается работать, если YASEO_SOURCES=live.
Позиции прошлых дней — случайное блуждание; сегодняшние не трогаются, если уже сняты.
"""
import os
import random
from datetime import timedelta

from yaseo_app import db, digest, mailer, monitor


def main() -> int:
    if os.environ.get("YASEO_SOURCES", "fake") != "fake":
        print("только в виртуальном режиме")
        return 1
    conn = db.connect()
    db.migrate(conn)
    today = monitor.msk_today(conn)
    rows = conn.execute("SELECT * FROM tracked_queries").fetchall()
    if not rows:
        print("нет запросов под наблюдением — добавьте на странице сайта")
        return 1
    rnd = random.Random(42)
    for t in rows:
        pos = rnd.randint(2, 11)
        for back in range(14, -1, -1):
            pos = min(11, max(1, pos + rnd.choice((-2, -1, 0, 0, 1, 2))))
            conn.execute(
                "INSERT INTO positions (site_id, query, region, day, position, top3)"
                " VALUES (%s, %s, %s, %s, %s, '[]') ON CONFLICT DO NOTHING",
                (t["site_id"], t["query"], t["region"], today - timedelta(days=back),
                 None if pos > 10 else pos))
    print(f"история позиций: {len(rows)} запросов × 15 дней")
    year, week, _ = today.isocalendar()
    for user in conn.execute("SELECT * FROM users WHERE weekly_digest").fetchall():
        data = digest.build(conn, user, today)
        if data:
            subject, html, text = digest.render(user, data)
            mailer.queue(conn, user, subject, html, text, "digest",
                         dedupe_key=f"digest-demo:{user['id']}:{today}")
            print(f"письмо для {user['email']}: {subject}")
    print(mailer.send_pending(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
