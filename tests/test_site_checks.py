"""Проверки кабинета: заглушка защиты вместо сайта, HTTPS и адреса-копии."""
import http.server
import threading
import unittest
from datetime import datetime, timedelta, timezone

from yaseo_app import score, site_checks

REAL = "<html><body><h1>Ремонт</h1>" + "<p>" + "слово " * 300 + "</p></body></html>"
CF = ("<html><head><title>Just a moment...</title></head><body>"
      "<div id='cf-browser-verification'>Checking your browser</div>"
      "<script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>")
SPA = "<html><body><div id=root></div><script src=/app.js></script><script>boot()</script></body></html>"


def page(status, html, words=None):
    return {"status": status, "html": html, "words": site_checks._text_words(html)
            if words is None else words, "error": None, "final": "http://x/"}


class ClassifyTest(unittest.TestCase):
    def test_cloudflare_challenge_is_stub(self):
        r = site_checks.classify_access(page(503, CF), page(200, REAL))
        self.assertEqual(r["verdict"], "stub")
        self.assertEqual(r["vendor"], "Cloudflare")

    def test_forbidden_for_bot_is_stub(self):
        r = site_checks.classify_access(page(403, "<html>nope</html>"), page(200, REAL))
        self.assertEqual(r["verdict"], "stub")

    def test_marker_in_long_real_page_is_not_stub(self):
        html = REAL.replace("<h1>", "<p>access denied — статья о доступе</p><h1>")
        self.assertEqual(site_checks.classify_access(page(200, html), page(200, html))["verdict"],
                         "real")

    def test_spa_is_empty(self):
        self.assertEqual(site_checks.classify_access(page(200, SPA), page(200, SPA))["verdict"],
                         "empty")

    def test_short_page_without_scripts_is_real(self):
        html = "<html><body><h1>Ремонт обуви</h1><p>Чиним обувь.</p></body></html>"
        self.assertEqual(site_checks.classify_access(page(200, html), page(200, html))["verdict"],
                         "real")

    def test_browser_gets_more(self):
        r = site_checks.classify_access(page(200, "<p>" + "a1 " * 50 + "</p>"), page(200, REAL))
        self.assertEqual(r["verdict"], "differs")

    def test_tls_classes(self):
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        cert = lambda d: {"ok": True, "not_after": (now + timedelta(days=d)).isoformat()}
        code = lambda info: [(i["severity"], i["code"]) for i in
                             site_checks.classify_tls(info, now, "https://x/")]
        self.assertEqual(code(cert(90)), [])
        self.assertEqual(code(cert(20)), [("minor", "tls-expiring")])
        self.assertEqual(code(cert(5)), [("major", "tls-expiring")])
        self.assertEqual(code(cert(-1)), [("critical", "tls-invalid")])
        self.assertEqual(code({"ok": False, "error": "self-signed"}), [("critical", "tls-invalid")])
        self.assertEqual(code({"ok": False, "error": "refused", "no_tls": True}),
                         [("critical", "no-https")])

    def test_stub_makes_score_grey(self):
        result = {"url": "http://x/",
                  "audit": {"pages_crawled": 3, "issues": [], "pages": []},
                  "geo": {"issues": [], "pages": [{"url": "http://x/"}], "bots": []},
                  "checks": {"access": {"verdict": "stub"}}}
        a = score.assess(result)
        self.assertIsNone(a.total)
        self.assertTrue(all(l.color == "grey" for l in a.lights))


class Handler(http.server.BaseHTTPRequestHandler):
    """Сайт за «защитой»: роботу — заглушка Cloudflare, браузеру — настоящая страница."""

    def do_GET(self):
        bot = "yaseo" in self.headers.get("User-Agent", "")
        body = (CF if bot else REAL).encode()
        self.send_response(503 if bot else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


class LiveStubTest(unittest.TestCase):
    """Положительный контроль на живом сервере: заглушка ловится, оценка серая."""

    def test_stub_site(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = site_checks.run(f"http://127.0.0.1:{srv.server_address[1]}/", allow_private=True)
        finally:
            srv.shutdown()
        self.assertEqual(r["access"]["verdict"], "stub")
        self.assertEqual(r["access"]["browser_status"], 200)
        self.assertIn("bot-stub", {i["code"] for i in r["issues"]})
        self.assertIn("no-https", {i["code"] for i in r["issues"]})

    def test_private_address_refused(self):
        from yaseo import net
        with self.assertRaises(net.UnsafeURL):
            site_checks.run("http://127.0.0.1:1/")
