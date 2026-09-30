"""Кабинет через HTTP: регистрация, вход, сайты, запуск, результат, чужие данные."""
import functools
import http.server
import re
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import accounts, sources, web, worker

SITE = Path(__file__).parent / "site"
PASSWORD = "длинный-пароль-1"


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class WebTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
        handler.log_message = lambda *a, **k: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.conn.execute("TRUNCATE sessions, login_attempts")
        self.client = TestClient(self.app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def signup(self, client=None, email="a@test.ru"):
        c = client or self.client
        r = c.post("/signup", data={"email": email, "password": PASSWORD, "consent": "yes"})
        self.assertEqual(r.status_code, 200, r.text[:300])
        return c

    def test_anonymous_goes_to_login(self):
        r = self.client.get("/sites", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_signup_requires_consent_and_long_password(self):
        r = self.client.post("/signup", data={"email": "a@test.ru", "password": PASSWORD})
        self.assertEqual(r.status_code, 400)
        self.assertIn("согласия", r.text)
        r = self.client.post("/signup", data={"email": "a@test.ru", "password": "short",
                                              "consent": "yes"})
        self.assertEqual(r.status_code, 400)

    def test_password_is_not_stored_plain(self):
        self.signup()
        row = self.conn.execute("SELECT password_hash, consent_at FROM users").fetchone()
        self.assertNotIn(PASSWORD, row["password_hash"])
        self.assertIsNotNone(row["consent_at"])

    def test_login_logout(self):
        self.signup()
        page = self.client.get("/sites").text
        self.client.post("/logout", data={"csrf": csrf(page)})
        self.assertEqual(self.client.get("/sites", follow_redirects=False).status_code, 303)
        r = self.client.post("/login", data={"email": "A@test.ru ", "password": PASSWORD})
        self.assertIn("Сайты", r.text)

    def test_wrong_password_and_lockout(self):
        self.signup()
        self.client.cookies.clear()
        for _ in range(accounts.LOGIN_MAX_FAILS):
            r = self.client.post("/login", data={"email": "a@test.ru", "password": "не тот"})
            self.assertIn("Неверная почта или пароль", r.text)
        r = self.client.post("/login", data={"email": "a@test.ru", "password": PASSWORD})
        self.assertIn("Слишком много", r.text)

    def test_session_token_not_stored_plain(self):
        self.signup()
        token = self.client.cookies.get(web.COOKIE)
        row = self.conn.execute("SELECT token_hash FROM sessions").fetchone()
        self.assertNotEqual(row["token_hash"], token)

    def test_csrf_required(self):
        self.signup()
        r = self.client.post("/sites", data={"url": self.url, "csrf": "чужой"})
        self.assertEqual(r.status_code, 403)

    def test_private_address_refused_in_production_mode(self):
        app = web.create_app(self.dsn, allow_private=False, secure_cookies=False)
        with TestClient(app) as c:
            self.signup(c, "b@test.ru")
            page = c.get("/sites").text
            r = c.post("/sites", data={"url": "http://127.0.0.1:8080", "csrf": csrf(page)})
            self.assertEqual(r.status_code, 400)
            self.assertIn("внутреннюю сеть", r.text)

    def test_full_flow_on_virtual_site(self):
        self.signup()
        page = self.client.get("/sites").text
        r = self.client.post("/sites", data={"url": self.url, "csrf": csrf(page)})
        self.assertIn("Новая проверка", r.text)
        site_url = str(r.url)
        r = self.client.post(site_url + "/run", data={
            "queries": "ремонт квартир\nдизайн интерьера", "max_pages": 5, "csrf": csrf(r.text)})
        self.assertIn("в очереди", r.text)
        self.assertIn('http-equiv="refresh"', r.text)
        job_url = str(r.url)

        r2 = self.client.post(site_url + "/run", data={"csrf": csrf(r.text)})
        self.assertIn("уже идёт", r2.text)

        worker.run_once(self.conn, "w-test", sources.build("fake"), allow_private=True)
        r = self.client.get(job_url)
        self.assertIn("Оценка сайта из 100", r.text)
        self.assertIn("виртуальные", r.text)
        self.assertIn("ремонт квартир", r.text)
        self.assertNotIn('http-equiv="refresh"', r.text)
        rep = self.client.get(job_url + "/report")
        self.assertEqual(rep.status_code, 200)
        self.assertIn('data-theme="light"', rep.text)

    def test_other_users_data_is_invisible(self):
        self.signup()
        page = self.client.get("/sites").text
        r = self.client.post("/sites", data={"url": self.url, "csrf": csrf(page)})
        site_url = str(r.url)
        r = self.client.post(site_url + "/run", data={"csrf": csrf(r.text)})
        job_url = str(r.url)

        with TestClient(self.app) as other:
            self.signup(other, "b@test.ru")
            for u in (site_url, job_url, job_url + "/report"):
                self.assertEqual(other.get(u).status_code, 404, u)
            self.assertNotIn(self.url, other.get("/sites").text)

    def test_security_headers(self):
        r = self.client.get("/login")
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_url_normalization(self):
        self.assertEqual(accounts.normalize_site_url("Example.ru"), "https://example.ru/")
        self.assertEqual(accounts.normalize_site_url("http://user:pw@сайт.рф:8080/a?b=1"),
                         "http://xn--80aswg.xn--p1ai:8080/a")
        with self.assertRaises(accounts.Refused):
            accounts.normalize_site_url("ftp://x.ru")
