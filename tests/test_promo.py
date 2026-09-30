"""Лендинг и промокоды: открытая регистрация, полная проверка по коду, тарифы 30.09.2026."""
import os
import re

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import accounts, billing, legal, promo, web
from yaseo_app.accounts import Refused

PASSWORD = "длинный-пароль-1"


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class PromoTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def signup(self, c, email, code="", site=""):
        r = c.post("/signup", data={"email": email, "password": PASSWORD, "consent": "yes",
                                    "offer": "yes", "promo": code, "site": site})
        self.conn.execute("UPDATE users SET email_confirmed_at = now() WHERE email = %s", (email,))
        return r

    def me(self, email="a@t.ru"):
        return self.conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()

    def test_code_format(self):
        code = promo.new_code()
        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        self.assertEqual(promo.normalize_code(code.lower().replace("-", " ")), code)

    def test_signup_is_open_without_code(self):
        with TestClient(self.app) as c:
            r = self.signup(c, "a@t.ru")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(billing.current(self.conn, self.me())["plan"]["code"], "free")

    def test_bad_code_stops_signup(self):
        with TestClient(self.app) as c:
            r = self.signup(c, "a@t.ru", "AAAA-BBBB")
            self.assertEqual(r.status_code, 400)
            self.assertIn("не подошёл", r.text)
        self.assertIsNone(self.me())

    def test_code_gives_full_check(self):
        [code] = promo.create(self.conn)
        with TestClient(self.app) as c:
            self.signup(c, "a@t.ru", code.lower())
        u = self.me()
        cur = billing.current(self.conn, u)
        self.assertEqual(cur["plan"]["code"], "promo")
        self.assertFalse(cur["sub"]["auto_renew"], "подарок не продлевается за деньги")
        site = accounts.add_site(self.conn, u, "http://shop.example", allow_private=True)
        jid = accounts.start_check(self.conn, u, site, "\n".join(f"q{i}" for i in range(9)), 500)
        job = self.conn.execute("SELECT params FROM jobs WHERE id = %s", (jid,)).fetchone()
        self.assertEqual(job["params"]["max_pages"], 30)
        self.assertEqual(len(job["params"]["queries"]), 5)
        self.assertEqual(job["params"]["limits"]["answers"], 3)
        self.conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (jid,))
        with self.assertRaises(Refused):
            accounts.start_check(self.conn, u, site)

    def test_single_use_code(self):
        [code] = promo.create(self.conn)
        with TestClient(self.app) as a, TestClient(self.app) as b:
            self.assertEqual(self.signup(a, "a@t.ru", code).status_code, 200)
            self.assertIn("не подошёл", self.signup(b, "b@t.ru", code).text)
        self.assertEqual(self.conn.execute("SELECT used FROM invites").fetchone()["used"], 1)

    def test_failed_signup_does_not_burn_code(self):
        [code] = promo.create(self.conn)
        self.user("taken@t.ru")
        with TestClient(self.app) as c:
            self.assertIn("уже зарегистрирован", self.signup(c, "taken@t.ru", code).text)
        self.assertEqual(self.conn.execute("SELECT used FROM invites").fetchone()["used"], 0)

    def test_expired_code(self):
        [code] = promo.create(self.conn, valid_days=1)
        self.conn.execute("UPDATE invites SET expires_at = now() - interval '1 second'")
        with self.assertRaises(Refused):
            promo.check(self.conn, code)

    def test_code_in_cabinet_once_and_only_on_free(self):
        [one, two] = promo.create(self.conn, 2)
        with TestClient(self.app) as c:
            self.signup(c, "a@t.ru")
            page = c.get("/billing").text
            self.assertIn("Промокод", page)
            r = c.post("/billing/promo", data={"promo": one, "csrf": csrf(page)})
            self.assertIn("Промокод применён", r.text)
            self.assertNotIn('action="/billing/promo"', r.text, "второй код не предлагаем")
        u = self.me()
        with self.assertRaises(Refused) as ctx:
            promo.redeem_in_cabinet(self.conn, u, two)
        self.assertIn("уже активирован", str(ctx.exception))
        paid = self.user("paid@t.ru", plan="free")
        self.conn.execute("INSERT INTO subscriptions (user_id, plan, status, period_start, period_end)"
                          " VALUES (%s, 'pro', 'active', now(), now() + interval '30 days')",
                          (paid["id"],))
        with self.assertRaises(Refused) as ctx:
            promo.redeem_in_cabinet(self.conn, paid, two)
        self.assertIn("бесплатном тарифе", str(ctx.exception))
        self.assertEqual(self.conn.execute("SELECT used FROM invites WHERE code = %s",
                                           (two,)).fetchone()["used"], 0)

    def test_code_can_grant_plan(self):
        [code] = promo.create(self.conn, plan="pro", days=30)
        with TestClient(self.app) as c:
            self.signup(c, "a@t.ru", code)
        self.assertEqual(billing.current(self.conn, self.me())["plan"]["code"], "pro")

    def test_old_invite_links_still_fill_code(self):
        with TestClient(self.app) as c:
            self.assertIn('value="ABCD-EFGH"', c.get("/signup?invite=ABCD-EFGH").text)
            self.assertIn('value="ABCD-EFGH"', c.get("/signup?promo=ABCD-EFGH").text)

    def test_site_from_landing_lands_in_cabinet(self):
        with TestClient(self.app) as c:
            r = self.signup(c, "a@t.ru", site="shop.example")
            self.assertIn("https://shop.example/", r.text)
            self.assertIn("/sites/", str(r.url))

    def test_landing_has_no_beta(self):
        with TestClient(self.app) as c:
            r = c.get("/")
        self.assertNotIn("бета", r.text.lower())
        self.assertIn('action="/signup"', r.text)
        self.assertIn("промокод", r.text.lower())
        self.assertNotIn("x-robots-tag", {k.lower() for k in r.headers})
        with TestClient(self.app) as c:
            self.assertEqual(c.post("/waitlist", data={"email": "w@t.ru"}).status_code, 404)

    def test_send_waitlist(self):
        for email, site in (("w1@t.ru", "a.example"), ("w2@t.ru", None)):
            self.conn.execute("INSERT INTO waitlist (email, site, consent_at) VALUES (%s, %s, now())",
                              (email, site))
        self.assertEqual(promo.send_waitlist(self.conn, 5), 2)
        self.assertEqual(promo.send_waitlist(self.conn, 5), 0, "дважды не пишем")
        mail = self.conn.execute("SELECT * FROM outbox WHERE to_email = 'w1@t.ru'").fetchone()
        code = re.search(r"promo=([A-Z0-9-]+)", mail["text"]).group(1)
        self.assertIn("site=a.example", mail["text"])
        self.assertIn(code, mail["html"])
        promo.check(self.conn, code)

    def test_cabinet_is_noindex_landing_is_not(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.get("/login").headers.get("x-robots-tag"), "noindex, nofollow")
            self.assertIsNone(c.get("/example").headers.get("x-robots-tag"))
            robots = c.get("/robots.txt").text
            self.assertIn("Disallow: /", robots)
            self.assertIn("Sitemap:", robots)
            self.assertIn("Пример на тестовом сайте", c.get("/example").text)


class PlansAndOfferTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def test_plans_from_sheet(self):
        """Таблица «yaseo — тарифы и настройки», 30.09.2026."""
        rows = {r["code"]: r for r in self.conn.execute("SELECT * FROM plans")}
        cols = ("price_rub", "sites", "audits_per_period", "max_pages", "queries_per_audit",
                "ai_checks", "tracked_queries", "steps_shown", "pdf", "weekly_digest",
                "white_label")
        want = {
            "free":   (0,     1,  1,  10,  0,  0,   0,    3,    False, False, False),
            "promo":  (0,     1,  1,  30,  5,  3,   0,    None, False, False, False),
            "once":   (1990,  1,  1,  50,  10, 5,   0,    None, True,  False, False),
            "start":  (2990,  2,  2,  50,  20, 10,  500,  None, True,  True,  False),
            "pro":    (4990,  5,  5,  100, 30, 50,  1500, None, True,  True,  True),
            "agency": (12900, 15, 20, 150, 50, 150, 3000, None, True,  True,  True),
        }
        for code, values in want.items():
            self.assertEqual(tuple(rows[code][c] for c in cols), values, code)
        self.assertEqual(rows["agency"]["title"], "Ультра")
        self.assertEqual([p["code"] for p in billing.plans(self.conn)],
                         ["free", "once", "start", "pro", "agency"], "промокод и бета не продаются")
        terms = {(t["plan"], t["months"]): t["price_rub"]
                 for t in self.conn.execute("SELECT * FROM plan_terms")}
        self.assertEqual(terms, {("once", 3): 4990, ("start", 3): 7990, ("pro", 3): 13490,
                                 ("agency", 3): 34490})

    def test_plans_migration_runs_once(self):
        """Правка цифр в базе после выкладки не затирается."""
        from yaseo_app import db
        self.conn.execute("UPDATE plans SET price_rub = 3490 WHERE code = 'start'")
        db.migrate(self.conn)
        self.assertEqual(billing.plan(self.conn, "start")["price_rub"], 3490)

    def test_ai_caps_raised(self):
        row = self.conn.execute("SELECT * FROM sources WHERE name = 'yandex-gen'").fetchone()
        self.assertEqual((row["per_user_daily"], row["service_daily_rub"]), (50, 1500))
        from yaseo_app import pipeline
        self.assertEqual(len(pipeline.clean_queries([f"q{i}" for i in range(70)])), 50)

    def test_free_check_has_no_phrases(self):
        u = self.user("f@t.ru")
        site = accounts.add_site(self.conn, u, "http://free.example", allow_private=True)
        jid = accounts.start_check(self.conn, u, site, "ремонт\nдизайн", max_pages=500)
        job = self.conn.execute("SELECT params FROM jobs WHERE id = %s", (jid,)).fetchone()
        self.assertEqual(job["params"]["queries"], [])
        self.assertEqual(job["params"]["max_pages"], 10)

    def test_offer_required_and_recorded(self):
        with TestClient(self.app) as c:
            r = c.post("/signup", data={"email": "o@t.ru", "password": PASSWORD, "consent": "yes"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("оферты", r.text)
            c.post("/signup", data={"email": "o@t.ru", "password": PASSWORD, "consent": "yes",
                                    "offer": "yes"})
        u = self.conn.execute("SELECT * FROM users").fetchone()
        self.assertEqual(u["offer_version"], legal.OFFER_VERSION)

    def test_offer_page_draft_until_requisites(self):
        with TestClient(self.app) as c:
            r = c.get("/legal/offer")
            self.assertIn("Черновик", r.text)
            self.assertIn("[ИНН]", r.text)
            self.assertIn("не продлевается и деньги не списываются", r.text)
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

    def test_landing_shows_prices_and_terms(self):
        with TestClient(self.app) as c:
            r = c.get("/")
        for text in ("2\xa0990\xa0₽", "4\xa0990\xa0₽", "12\xa0900\xa0₽", "7\xa0990\xa0₽ за 3 месяца", "Ультра",
                     "/legal/offer", "до 10 страниц"):
            self.assertIn(text, r.text)
