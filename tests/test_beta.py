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
        return c.post("/signup", data={"email": email, "password": PASSWORD, "consent": "yes", "offer": "yes",
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
                                          "consent": "yes", "offer": "yes"})
            self.assertIn("Заявка принята", r.text)
            c.post("/waitlist", data={"email": "w@t.ru", "consent": "yes", "offer": "yes"})
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


class BetaPlanAndOfferTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def test_pages_capped_by_plan(self):
        """Бета обходит до 30 страниц, даже если попросить больше."""
        from yaseo_app import accounts
        [code] = beta.create(self.conn)
        accounts.signup(self.conn, "p@t.ru", PASSWORD, True, invite=code)
        self.conn.execute("UPDATE users SET email_confirmed_at = now()")
        u = self.conn.execute("SELECT * FROM users").fetchone()
        site = accounts.add_site(self.conn, u, "http://pages.example", allow_private=True)
        jid = accounts.start_check(self.conn, u, site, max_pages=500)
        job = self.conn.execute("SELECT params FROM jobs WHERE id = %s", (jid,)).fetchone()
        self.assertEqual(job["params"]["max_pages"], 30)
        caps = {r["code"]: r["max_pages"] for r in self.conn.execute("SELECT code, max_pages FROM plans")}
        self.assertEqual([caps[c] for c in ("free", "beta", "once", "start", "pro", "agency")],
                         [30, 30, 100, 100, 150, 200])

    def test_beta_plan_five_checks_in_30_days(self):
        from yaseo_app import accounts
        [code] = beta.create(self.conn)   # по умолчанию код даёт тариф «Бета»
        u = accounts.signup(self.conn, "b@t.ru", PASSWORD, True, invite=code)
        self.conn.execute("UPDATE users SET email_confirmed_at = now()")
        u = self.conn.execute("SELECT * FROM users").fetchone()
        self.assertEqual(billing.current(self.conn, u)["plan"]["code"], "beta")
        site = accounts.add_site(self.conn, u, "http://shop.example", allow_private=True)
        for _ in range(3):
            jid = accounts.start_check(self.conn, u, site)
            self.conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (jid,))
        with self.assertRaises(Refused) as ctx:
            accounts.start_check(self.conn, u, site)
        self.assertIn("3 проверки", str(ctx.exception))
        # окно скользящее: самая старая проверка вышла за 30 дней — можно снова
        self.conn.execute("UPDATE jobs SET created_at = now() - interval '31 days'"
                          " WHERE id = (SELECT min(id) FROM jobs)")
        accounts.start_check(self.conn, u, site)

    def test_beta_plan_not_for_sale(self):
        self.assertNotIn("beta", {p["code"] for p in billing.plans(self.conn)})

    def test_offer_required_and_recorded(self):
        with TestClient(self.app) as c:
            r = c.post("/signup", data={"email": "o@t.ru", "password": PASSWORD, "consent": "yes"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("оферты", r.text)
            c.post("/signup", data={"email": "o@t.ru", "password": PASSWORD, "consent": "yes",
                                    "offer": "yes"})
        u = self.conn.execute("SELECT * FROM users").fetchone()
        self.assertEqual(u["offer_version"], "30.09.2026")

    def test_offer_page_draft_until_requisites(self):
        with TestClient(self.app) as c:
            r = c.get("/legal/offer")
            self.assertIn("Черновик", r.text)
            self.assertIn("[ИНН]", r.text)
            env = {"YASEO_OPERATOR": "ИП Проверочный П. П.", "YASEO_OPERATOR_INN": "231000000000",
                   "YASEO_OPERATOR_OGRNIP": "300000000000000", "YASEO_OPERATOR_ADDRESS": "Краснодар",
                   "YASEO_SUPPORT_EMAIL": "help@yaseo.example", "YASEO_SITE_DOMAIN": "yaseo.example",
                   "YASEO_VAT_NOTE": "НДС не облагается"}
            os.environ.update(env)
            try:
                r = c.get("/legal/offer")
            finally:
                for k in env:
                    os.environ.pop(k)
            self.assertNotIn("Черновик", r.text)
            self.assertIn("ИНН 231000000000", r.text)

    def test_real_payments_blocked_without_requisites(self):
        os.environ["YASEO_PAYMENTS"] = "yookassa"
        try:
            with self.assertRaises(ValueError) as ctx:
                billing.provider(self.conn)
        finally:
            os.environ.pop("YASEO_PAYMENTS")
        self.assertIn("реквизиты оферты", str(ctx.exception))

    def test_landing_shows_prices(self):
        with TestClient(self.app) as c:
            r = c.get("/")
        self.assertIn("4 990 ₽", r.text)
        self.assertIn("/legal/offer", r.text)
