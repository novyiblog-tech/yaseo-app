"""Лендинг и закрытая бета: коды приглашений, лист ожидания, сайт с лендинга в кабинет."""
import os
import re

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import beta, billing, web
from yaseo_app.accounts import Refused

PASSWORD = "длинный-пароль-1"


class BetaTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def setUp(self):
        super().setUp()
        os.environ["YASEO_BETA"] = "1"

    def tearDown(self):
        os.environ.pop("YASEO_BETA", None)

    def signup(self, c, email, invite="", site=""):
        return c.post("/signup", data={"email": email, "password": PASSWORD, "consent": "yes",
                                       "invite": invite, "site": site})

    def test_code_format(self):
        code = beta.new_code()
        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        self.assertEqual(beta.normalize_code(code.lower().replace("-", " ")), code)

    def test_signup_requires_code_in_beta(self):
        with TestClient(self.app) as c:
            r = self.signup(c, "a@t.ru")
            self.assertEqual(r.status_code, 400)
            self.assertIn("закрытая бета", r.text)
            r = self.signup(c, "a@t.ru", invite="AAAA-BBBB")
            self.assertIn("не подошёл", r.text)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM users").fetchone())

    def test_single_use_code(self):
        [code] = beta.create(self.conn)
        with TestClient(self.app) as a, TestClient(self.app) as b:
            self.assertEqual(self.signup(a, "a@t.ru", code.lower()).status_code, 200)
            r = self.signup(b, "b@t.ru", code)
            self.assertIn("не подошёл", r.text)
        row = self.conn.execute("SELECT used FROM invites").fetchone()
        self.assertEqual(row["used"], 1)

    def test_failed_signup_does_not_burn_code(self):
        [code] = beta.create(self.conn)
        self.user("taken@t.ru")
        with TestClient(self.app) as c:
            self.assertIn("уже зарегистрирован", self.signup(c, "taken@t.ru", code).text)
        self.assertEqual(self.conn.execute("SELECT used FROM invites").fetchone()["used"], 0)

    def test_expired_code(self):
        [code] = beta.create(self.conn, valid_days=1)
        self.conn.execute("UPDATE invites SET expires_at = now() - interval '1 second'")
        with self.assertRaises(Refused):
            beta.check(self.conn, code)

    def test_code_can_grant_plan(self):
        [code] = beta.create(self.conn, plan="pro", days=30)
        with TestClient(self.app) as c:
            self.signup(c, "a@t.ru", code)
        u = self.conn.execute("SELECT * FROM users").fetchone()
        cur = billing.current(self.conn, u)
        self.assertEqual(cur["plan"]["code"], "pro")
        self.assertFalse(cur["sub"]["auto_renew"], "подарок не продлевается за деньги")

    def test_site_from_landing_lands_in_cabinet(self):
        os.environ.pop("YASEO_BETA")
        with TestClient(self.app) as c:
            r = self.signup(c, "a@t.ru", site="shop.example")
            self.assertIn("https://shop.example/", r.text)
            self.assertIn("/sites/", str(r.url))

    def test_landing_and_waitlist(self):
        with TestClient(self.app) as c:
            r = c.get("/")
            self.assertIn("Закрытая бета", r.text)
            self.assertNotIn("x-robots-tag", {k.lower() for k in r.headers})
            self.assertEqual(c.post("/waitlist", data={"email": "w@t.ru"}).status_code, 400)
            r = c.post("/waitlist", data={"email": "W@t.ru", "site": "shop.example",
                                          "consent": "yes"})
            self.assertIn("Заявка принята", r.text)
            c.post("/waitlist", data={"email": "w@t.ru", "consent": "yes"})
        rows = self.conn.execute("SELECT * FROM waitlist").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "shop.example", "повтор без сайта его не стирает")

    def test_send_invites_from_waitlist(self):
        beta.join_waitlist(self.conn, "w1@t.ru", "a.example", True)
        beta.join_waitlist(self.conn, "w2@t.ru", None, True)
        self.assertEqual(beta.send_invites(self.conn, 5), 2)
        self.assertEqual(beta.send_invites(self.conn, 5), 0, "дважды не приглашаем")
        mail = self.conn.execute("SELECT * FROM outbox WHERE to_email = 'w1@t.ru'").fetchone()
        code = re.search(r"invite=([A-Z0-9-]+)", mail["text"]).group(1)
        self.assertIn("site=a.example", mail["text"])
        beta.check(self.conn, code)

    def test_cabinet_is_noindex_landing_is_not(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.get("/login").headers.get("x-robots-tag"), "noindex, nofollow")
            self.assertIsNone(c.get("/example").headers.get("x-robots-tag"))
            robots = c.get("/robots.txt").text
            self.assertIn("Disallow: /", robots)
            self.assertIn("Sitemap:", robots)
            self.assertIn("Пример на тестовом сайте", c.get("/example").text)
