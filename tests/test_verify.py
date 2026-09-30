"""Подтверждение почты, восстановление пароля, ограничения бесплатной проверки."""
import re

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import accounts, billing, monitor, verify, web
from yaseo_app.accounts import Refused

PASSWORD = "длинный-пароль-1"


def link(conn, kind):
    row = conn.execute("SELECT text FROM outbox WHERE kind = %s ORDER BY id DESC LIMIT 1",
                       (kind,)).fetchone()
    return re.search(r"(/(confirm|reset)\?t=[\w-]+)", row["text"]).group(1)


class VerifyTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def signup(self, c, email="a@t.ru"):
        return c.post("/signup", data={"email": email, "password": PASSWORD, "consent": "yes"})

    def test_unconfirmed_cannot_check_or_buy(self):
        u = accounts.signup(self.conn, "x@t.ru", PASSWORD, True)
        site = accounts.add_site(self.conn, u, "http://a.example", allow_private=True)
        for action in (lambda: accounts.start_check(self.conn, u, site),
                       lambda: billing.start_purchase(self.conn, u, "pro",
                                                      billing.FakeProvider(self.conn), "/r"),
                       lambda: monitor.add_queries(self.conn, u, site, "ремонт")):
            with self.assertRaises(Refused) as ctx:
                action()
            self.assertIn("подтвердите почту", str(ctx.exception))

    def test_confirm_flow(self):
        with TestClient(self.app) as c:
            r = self.signup(c)
            self.assertIn("Подтвердите почту", r.text)
            url = link(self.conn, "confirm")
            self.assertIn("Почта подтверждена", c.get(url).text)
            self.assertIn("не сработала", c.get(url).text, "ссылка одноразовая")
            self.assertNotIn("Подтвердите почту", c.get("/sites").text)

    def test_token_not_stored_plain(self):
        with TestClient(self.app) as c:
            self.signup(c)
        token = link(self.conn, "confirm").split("t=")[1]
        self.assertIsNone(self.conn.execute("SELECT 1 FROM email_tokens WHERE token_hash = %s",
                                            (token,)).fetchone())

    def test_expired_confirm(self):
        with TestClient(self.app) as c:
            self.signup(c)
            self.conn.execute("UPDATE email_tokens SET expires_at = now() - interval '1 second'")
            self.assertIn("не сработала", c.get(link(self.conn, "confirm")).text)

    def test_resend_is_throttled(self):
        u = accounts.signup(self.conn, "y@t.ru", PASSWORD, True)
        self.assertFalse(verify.send_confirmation(self.conn, u), "сразу после регистрации — нет")
        self.conn.execute("UPDATE email_tokens SET created_at = now() - interval '2 minutes'")
        self.assertTrue(verify.send_confirmation(self.conn, u))

    def test_reset_flow(self):
        with TestClient(self.app) as c, TestClient(self.app) as other:
            self.signup(c)
            other.post("/login", data={"email": "a@t.ru", "password": PASSWORD})
            self.assertEqual(other.get("/sites", follow_redirects=False).status_code, 200)
            r = c.post("/forgot", data={"email": "A@t.ru"})
            self.assertIn("Если такой адрес зарегистрирован", r.text)
            url = link(self.conn, "reset")
            r = c.post("/reset", data={"t": url.split("t=")[1], "password": "новый-пароль-22"})
            self.assertIn("Мои сайты", r.text)
            self.assertEqual(other.get("/sites", follow_redirects=False).status_code, 303,
                             "старые сессии закрыты")
            r = c.post("/reset", data={"t": url.split("t=")[1], "password": "ещё-один-пароль"})
            self.assertEqual(r.status_code, 400, "ссылка одноразовая")
            u = self.conn.execute("SELECT * FROM users").fetchone()
            self.assertTrue(accounts.check_password("новый-пароль-22", u["password_hash"]))
            self.assertIsNotNone(u["email_confirmed_at"])

    def test_forgot_unknown_email_same_answer(self):
        with TestClient(self.app) as c:
            r = c.post("/forgot", data={"email": "nobody@t.ru"})
            self.assertIn("Если такой адрес зарегистрирован", r.text)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM outbox").fetchone())

    def test_signups_per_ip(self):
        for i in range(verify.SIGNUPS_PER_IP_DAY):
            accounts.signup(self.conn, f"ip{i}@t.ru", PASSWORD, True, ip="10.0.0.7")
        with self.assertRaises(Refused):
            accounts.signup(self.conn, "ip-last@t.ru", PASSWORD, True, ip="10.0.0.7")
        accounts.signup(self.conn, "other-ip@t.ru", PASSWORD, True, ip="10.0.0.8")

    def test_free_check_once_per_domain_across_accounts(self):
        a, b = self.user("a@x"), self.user("b@x")
        sa = accounts.add_site(self.conn, a, "https://www.shop.example/", allow_private=True)
        sb = accounts.add_site(self.conn, b, "http://shop.example", allow_private=True)
        accounts.start_check(self.conn, a, sa)
        with self.assertRaises(Refused) as ctx:
            accounts.start_check(self.conn, b, sb)
        self.assertIn("этого сайта уже была", str(ctx.exception))
