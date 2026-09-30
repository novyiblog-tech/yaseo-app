"""Наблюдение: запросы по тарифу, ежедневный съём без повторов, письмо недели, отписка."""
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import accounts, billing, digest, mailer, monitor, scheduler, sources, web, worker
from yaseo_app.accounts import Refused

DAY = date(2026, 9, 30)


class MonitorTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.u = self.user()
        self.site = accounts.add_site(self.conn, self.u, "http://shop.example", allow_private=True)

    def upgrade(self, plan="start"):
        prov = billing.FakeProvider(self.conn)
        url = billing.start_purchase(self.conn, self.u, plan, prov, "/r")
        pid = url.rsplit("/", 1)[1]
        prov.decide(pid, True)
        billing.confirm(self.conn, prov, pid)
        self.u = self.conn.execute("SELECT * FROM users WHERE id = %s", (self.u["id"],)).fetchone()

    def run_all(self):
        while worker.run_once(self.conn, "w", sources.build("fake")):
            pass

    def test_free_plan_cannot_track(self):
        with self.assertRaises(Refused):
            monitor.add_queries(self.conn, self.u, self.site, "ремонт")

    def test_quota(self):
        self.upgrade()
        self.conn.execute("UPDATE plans SET tracked_queries = 3 WHERE code = 'start'")
        self.assertEqual(monitor.add_queries(self.conn, self.u, self.site, "а1\nб2\nа1"), 2)
        with self.assertRaises(Refused):
            monitor.add_queries(self.conn, self.u, self.site, "в3\nг4")
        monitor.add_queries(self.conn, self.u, self.site, "в3")

    def test_daily_run_once_per_day(self):
        self.upgrade()
        monitor.add_queries(self.conn, self.u, self.site, "ремонт квартир\nдизайн интерьера")
        self.assertEqual(monitor.schedule_daily(self.conn, DAY), 1)
        self.assertEqual(monitor.schedule_daily(self.conn, DAY), 0, "повтор за тот же день")
        self.run_all()
        rows = self.conn.execute("SELECT * FROM positions WHERE day = %s", (DAY,)).fetchall()
        self.assertEqual(len(rows), 2)
        spent = self.conn.execute("SELECT count(*) AS n FROM spend WHERE source = 'yandex-serp'"
                                  " AND NOT cached").fetchone()["n"]
        self.assertEqual(spent, 2)
        self.assertEqual(monitor.schedule_daily(self.conn, DAY + timedelta(days=1)), 1)

    def test_downgrade_limits_what_is_checked(self):
        self.upgrade()
        monitor.add_queries(self.conn, self.u, self.site, "\n".join(f"q{i}" for i in range(5)))
        self.conn.execute("UPDATE plans SET tracked_queries = 2 WHERE code = 'start'")
        monitor.schedule_daily(self.conn, DAY)
        self.run_all()
        n = self.conn.execute("SELECT count(*) AS n FROM positions").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_positions_do_not_block_audit_or_show_in_history(self):
        self.upgrade()
        monitor.add_queries(self.conn, self.u, self.site, "ремонт")
        monitor.schedule_daily(self.conn, DAY)
        self.assertEqual(accounts.site_jobs(self.conn, self.site["id"]), [])
        accounts.start_check(self.conn, self.u, self.site)

    def test_week_delta_and_digest(self):
        self.upgrade()
        monitor.add_queries(self.conn, self.u, self.site, "ремонт\nдизайн\nотделка")
        for q, before, after in (("ремонт", 8, 3), ("дизайн", 2, 6), ("отделка", 4, 4)):
            for day, pos in ((DAY - timedelta(days=7), before), (DAY, after)):
                self.conn.execute("INSERT INTO positions (site_id, query, region, day, position)"
                                  " VALUES (%s, %s, 225, %s, %s)", (self.site["id"], q, day, pos))
        table = {r["query"]: r["delta"] for r in monitor.site_table(self.conn, self.site["id"], DAY)}
        self.assertEqual(table, {"ремонт": 5, "дизайн": -4, "отделка": 0})
        self.assertEqual(digest.queue_weekly(self.conn, DAY), 1)
        self.assertEqual(digest.queue_weekly(self.conn, DAY), 0, "одно письмо в неделю")
        mail = self.conn.execute("SELECT * FROM outbox").fetchone()
        self.assertIn("позиции: выросли 1, просели 1", mail["subject"])
        self.assertIn("ремонт", mail["html"])
        self.assertIn("/unsubscribe?u=", mail["text"])

    def test_nothing_changed_no_letter(self):
        self.assertEqual(digest.queue_weekly(self.conn, DAY), 0)

    def test_digest_off(self):
        self.upgrade()
        self.conn.execute("INSERT INTO positions (site_id, query, region, day, position) VALUES"
                          " (%s, 'x', 225, %s, 5), (%s, 'x', 225, %s, 1)",
                          (self.site["id"], DAY - timedelta(days=7), self.site["id"], DAY))
        monitor.add_queries(self.conn, self.u, self.site, "x")
        self.conn.execute("UPDATE users SET weekly_digest = false")
        self.assertEqual(digest.queue_weekly(self.conn, DAY), 0)

    def test_mail_sent_once_dev_sender(self):
        mailer.queue(self.conn, self.u, "тема", "<p>привет</p>", "привет", "test", "k1")
        mailer.queue(self.conn, self.u, "тема", "<p>привет</p>", "привет", "test", "k1")
        with tempfile.TemporaryDirectory() as d:
            out = mailer.send_pending(self.conn, mailer.DevSender(Path(d)))
            self.assertEqual(out["sent"], 1)
            self.assertEqual(len(list(Path(d).glob("*.eml"))), 1)
        self.assertEqual(mailer.send_pending(self.conn, mailer.DevSender(Path(d)))["sent"], 0)

    def test_failing_sender_retries_then_gives_up(self):
        class Broken(mailer.Sender):
            def send(self, msg):
                raise OSError("smtp down")
        mailer.queue(self.conn, self.u, "т", "<p>x</p>", "x", "test")
        for _ in range(mailer.MAX_ATTEMPTS):
            mailer.send_pending(self.conn, Broken())
        row = self.conn.execute("SELECT status, attempts FROM outbox").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("failed", mailer.MAX_ATTEMPTS))

    def test_scheduler_windows(self):
        self.upgrade()
        monitor.add_queries(self.conn, self.u, self.site, "ремонт")
        night = datetime(2026, 9, 30, 2, 0)   # среда, до трёх ночи
        self.assertNotIn("positions", scheduler.tick(self.conn, night))
        out = scheduler.tick(self.conn, datetime(2026, 9, 30, 3, 5))
        self.assertEqual(out["positions"], 1)
        self.assertNotIn("digests", out)
        monday = datetime(2026, 10, 5, 9, 30)
        self.assertIn("digests", scheduler.tick(self.conn, monday))


class UnsubscribeTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=False, secure_cookies=False)

    def test_signed_link_only(self):
        u = self.user()
        with TestClient(self.app) as c:
            self.assertEqual(c.get(f"/unsubscribe?u={u['id']}&t=bad").status_code, 404)
            r = c.get(f"/unsubscribe?u={u['id']}&t={mailer.unsubscribe_token(u['id'])}")
            self.assertIn("Вы отписаны", r.text)
            self.assertEqual(c.get("/dev/outbox", follow_redirects=False).status_code, 303)
        row = self.conn.execute("SELECT weekly_digest FROM users").fetchone()
        self.assertFalse(row["weekly_digest"])
