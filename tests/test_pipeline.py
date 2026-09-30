"""Конвейер целиком на тестовом сайте с виртуальными источниками: ни ключей, ни трат."""
import functools
import secrets
import http.server
import threading
from pathlib import Path

from pgtest import PgTestCase

from yaseo_app import jobs, pipeline, sources, worker

SITE = Path(__file__).parent / "site"
QUERIES = ["Ремонт квартир", "ремонт квартир ", "дизайн интерьера", "отделка под ключ"]


class PipelineTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
        handler.log_message = lambda *a, **k: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        super().tearDownClass()

    def run_job(self, queries=QUERIES, plan="free"):
        u = self.user(f"{plan}-{secrets.token_hex(3)}@t", plan)
        jid = jobs.enqueue(self.conn, u["id"], "audit",
                           {"url": self.url, "max_pages": 5, "queries": queries})
        worker.run_once(self.conn, "w-test", sources.build("fake"), allow_private=True)
        return self.conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone()

    def test_full_run(self):
        job = self.run_job()
        self.assertEqual(job["status"], "done", job["error"])
        r = job["result"]
        self.assertEqual(r["sources_mode"], "fake")
        self.assertEqual(len(r["queries"]), 3, "дубли запросов схлопываются")
        self.assertIn("broken-internal-link", {i["code"] for i in r["free"]["audit"]["issues"]})
        for section in ("demand", "positions", "answers"):
            self.assertEqual(r["paid"][section]["status"], "ok", section)
            self.assertEqual(len(r["paid"][section]["items"]), 3)
        by = r["spend"]["by_source"]
        self.assertEqual(by["wordstat"]["calls"], 3)
        self.assertEqual(by["yandex-serp"]["units"], 3)
        self.assertAlmostEqual(r["spend"]["cost_rub"], 3 * 0.02 + 3 * 0.0305 + 3 * 5.08, 4)

    def test_second_run_is_served_from_cache(self):
        self.run_job()
        job = self.run_job()
        self.assertEqual(job["result"]["spend"]["cost_rub"], 0)
        self.assertGreater(job["result"]["spend"]["by_source"]["yandex-gen"]["saved_rub"], 15)

    def test_blocked_source_skips_section_not_job(self):
        self.set_source("yandex-gen", enabled=False)
        job = self.run_job()
        self.assertEqual(job["status"], "done")
        gen = job["result"]["paid"]["answers"]
        self.assertEqual(gen["status"], "skipped")
        self.assertIn("выключен", gen["reason"])
        self.assertEqual(job["result"]["paid"]["positions"]["status"], "ok")

    def test_user_limit_gives_partial_section(self):
        self.set_source("yandex-gen", per_user_daily=2)
        job = self.run_job()
        gen = job["result"]["paid"]["answers"]
        self.assertEqual(gen["status"], "partial")
        self.assertEqual(len(gen["items"]), 2)

    def test_wordstat_quota_postpones_job(self):
        self.set_source("wordstat", rate_per_hour=2)
        job = self.run_job()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempts"], 0)
        self.assertIn("квота", job["error"])
        spent = self.conn.execute("SELECT count(*) AS n FROM spend WHERE source <> 'wordstat'")
        self.assertEqual(spent.fetchone()["n"], 0, "до выдачи и нейросетей не дошли")

    def test_dead_site_costs_nothing(self):
        u = self.user()
        jid = jobs.enqueue(self.conn, u["id"], "audit",
                           {"url": "http://127.0.0.1:9/", "queries": QUERIES})
        worker.run_once(self.conn, "w-test", sources.build("fake"), allow_private=True)
        job = self.conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone()
        self.assertIn(job["status"], ("queued", "failed"))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM spend").fetchone())

    def test_provider_failures_stop_section(self):
        class Broken(sources.FakeYandexGen):
            def fetch(self, params):
                raise RuntimeError("401 ключ отозван")
        srcs = sources.build("fake")
        srcs["yandex-gen"] = Broken()
        u = self.user()
        jid = jobs.enqueue(self.conn, u["id"], "audit",
                           {"url": self.url, "max_pages": 3,
                            "queries": [f"запрос {i}" for i in range(10)]})
        worker.run_once(self.conn, "w-test", srcs, allow_private=True)
        r = self.conn.execute("SELECT result FROM jobs WHERE id = %s", (jid,)).fetchone()["result"]
        self.assertEqual(r["paid"]["answers"]["status"], "failed")
        self.assertEqual(r["spend"]["by_source"]["yandex-gen"]["calls"],
                         pipeline.MAX_ERRORS_IN_ROW)
