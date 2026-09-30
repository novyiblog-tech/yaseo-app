"""Бесплатный аудит на локальном сайте с заведомыми дефектами. Сеть наружу не нужна."""
import functools
import http.server
import os
import threading
import unittest
from pathlib import Path

from yaseo_app import free_audit

SITE = Path(__file__).parent / "site"


class FreeAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
        handler.log_message = lambda *a, **k: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.result = free_audit.run_isolated(cls.url, max_pages=10, allow_private=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def codes(self):
        return {i["code"] for i in self.result["audit"]["issues"]}

    def test_pages_crawled(self):
        self.assertGreaterEqual(self.result["audit"]["pages_crawled"], 3)

    def test_planted_defects_found(self):
        for code in ("broken-internal-link", "multiple-h1", "missing-description"):
            self.assertIn(code, self.codes())

    def test_every_issue_has_evidence(self):
        for issue in self.result["audit"]["issues"]:
            self.assertTrue(issue["evidence"], issue["code"])

    def test_no_paid_calls(self):
        self.assertEqual(self.result["paid_calls"], 0)

    def test_geo_section_present(self):
        self.assertIn("bots", self.result["geo"])

    def test_private_address_refused_by_default(self):
        with self.assertRaises(free_audit.AuditFailed):
            free_audit.run_isolated(self.url, max_pages=1)

    def test_keys_do_not_reach_worker(self):
        os.environ["YANDEX_AI_STUDIO_API_KEY"] = "test-value"
        try:
            env = free_audit.isolated_env(Path("/tmp/x"))
        finally:
            del os.environ["YANDEX_AI_STUDIO_API_KEY"]
        self.assertNotIn("YANDEX_AI_STUDIO_API_KEY", env)


if __name__ == "__main__":
    unittest.main()
