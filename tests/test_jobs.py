"""Очередь: порядок, отсутствие двойной выдачи, повторы, брошенные задачи."""
from datetime import timedelta

from pgtest import PgTestCase

from yaseo_app import db, jobs


class JobsTest(PgTestCase):
    def test_fifo_and_empty(self):
        u = self.user()
        a = jobs.enqueue(self.conn, u["id"], "audit", {"n": 1})
        b = jobs.enqueue(self.conn, u["id"], "audit", {"n": 2})
        self.assertEqual(jobs.claim(self.conn, "w1")["id"], a)
        self.assertEqual(jobs.claim(self.conn, "w1")["id"], b)
        self.assertIsNone(jobs.claim(self.conn, "w1"))

    def test_two_workers_never_get_same_job(self):
        u = self.user()
        for _ in range(20):
            jobs.enqueue(self.conn, u["id"], "audit", {})
        other = db.connect(self.dsn)
        try:
            got = []
            for i in range(20):
                c = self.conn if i % 2 else other
                got.append(jobs.claim(c, f"w{i % 2}")["id"])
        finally:
            other.close()
        self.assertEqual(len(got), len(set(got)))

    def test_skip_locked_under_open_transaction(self):
        u = self.user()
        a = jobs.enqueue(self.conn, u["id"], "audit", {})
        b = jobs.enqueue(self.conn, u["id"], "audit", {})
        other = db.connect(self.dsn)
        try:
            with other.transaction():
                other.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (a,))
                self.assertEqual(jobs.claim(self.conn, "w1")["id"], b)
        finally:
            other.close()

    def test_retry_with_backoff_then_failed(self):
        u = self.user()
        jid = jobs.enqueue(self.conn, u["id"], "audit", {}, max_attempts=2)
        job = jobs.claim(self.conn, "w1")
        self.assertEqual(jobs.fail(self.conn, job, "сбой"), "queued")
        self.assertIsNone(jobs.claim(self.conn, "w1"), "повтор раньше паузы")
        self.conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (jid,))
        job = jobs.claim(self.conn, "w1")
        self.assertEqual(job["attempts"], 2)
        self.assertEqual(jobs.fail(self.conn, job, "сбой"), "failed")

    def test_abandoned_job_is_reclaimed(self):
        u = self.user()
        jobs.enqueue(self.conn, u["id"], "audit", {})
        first = jobs.claim(self.conn, "w1", lease=timedelta(seconds=-1))  # аренда уже истекла
        second = jobs.claim(self.conn, "w2")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(jobs.LeaseLost):
            jobs.finish(self.conn, first, {"ok": True})
        jobs.finish(self.conn, second, {"ok": True})

    def test_abandoned_job_without_attempts_fails(self):
        u = self.user()
        jid = jobs.enqueue(self.conn, u["id"], "audit", {}, max_attempts=1)
        jobs.claim(self.conn, "w1", lease=timedelta(seconds=-1))
        self.assertIsNone(jobs.claim(self.conn, "w2"))
        row = self.conn.execute("SELECT status FROM jobs WHERE id = %s", (jid,)).fetchone()
        self.assertEqual(row["status"], "failed")

    def test_postpone_keeps_attempt(self):
        u = self.user()
        jid = jobs.enqueue(self.conn, u["id"], "audit", {}, max_attempts=1)
        job = jobs.claim(self.conn, "w1")
        jobs.postpone(self.conn, job, timedelta(0), "квота")
        again = jobs.claim(self.conn, "w1")
        self.assertEqual(again["id"], jid)
        self.assertEqual(again["attempts"], 1)
