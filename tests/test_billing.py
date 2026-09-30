"""Деньги на виртуальном платёжном сервисе: покупка, отказ, повтор уведомления, лимиты,
продление, неудачное списание и падение до бесплатного тарифа."""
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import accounts, billing, history, web
from yaseo_app.accounts import Refused

PASSWORD = "длинный-пароль-1"


def signup(test, c, email):
    c.post("/signup", data={"email": email, "password": PASSWORD, "consent": "yes", "offer": "yes"})
    test.conn.execute("UPDATE users SET email_confirmed_at = now() WHERE email = %s", (email,))


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class BillingTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.prov = billing.FakeProvider(self.conn)
        self.u = self.user()

    def buy(self, plan="pro", pay=True, method="card_4242", months=1, auto_renew=False):
        url = billing.start_purchase(self.conn, self.u, plan, self.prov, "/r?p={payment}",
                                     months=months, auto_renew=auto_renew)
        pid = url.rsplit("/", 1)[1]
        self.prov.decide(pid, pay, method)
        return pid, billing.confirm(self.conn, self.prov, pid)

    def plan_now(self):
        return billing.current(self.conn, self.u)["plan"]["code"]

    def sub(self):
        return billing.subscription(self.conn, self.u["id"])

    def shift(self, days):
        """Сдвинуть срок и оплаты в прошлое: будто прошло столько дней."""
        d = timedelta(days=days)
        self.conn.execute("UPDATE subscriptions SET period_start = period_start - %s,"
                          " period_end = period_end - %s", (d, d))
        self.conn.execute("UPDATE payments SET paid_at = paid_at - %s", (d,))

    def mails(self, kind="billing"):
        return self.conn.execute("SELECT * FROM outbox WHERE kind = %s ORDER BY id",
                                 (kind,)).fetchall()

    def test_default_is_free(self):
        self.assertEqual(self.plan_now(), "free")

    def test_purchase_activates_plan_without_auto_renew(self):
        _, pay = self.buy()
        self.assertEqual(pay["status"], "succeeded")
        self.assertEqual(self.plan_now(), "pro")
        u = self.conn.execute("SELECT plan FROM users WHERE id = %s", (self.u["id"],)).fetchone()
        self.assertEqual(u["plan"], "pro", "журнал расхода видит тариф через users.plan")
        self.assertFalse(self.sub()["auto_renew"], "без отдельного согласия не продлеваем")

    def test_declined_payment_changes_nothing(self):
        _, pay = self.buy(pay=False)
        self.assertEqual(pay["status"], "canceled")
        self.assertEqual(self.plan_now(), "free")

    def test_repeated_notification_does_not_extend_twice(self):
        pid, _ = self.buy()
        end = self.sub()["period_end"]
        billing.confirm(self.conn, self.prov, pid)
        billing.confirm(self.conn, self.prov, pid)
        self.assertEqual(self.sub()["period_end"], end)

    def test_pending_is_not_activated(self):
        url = billing.start_purchase(self.conn, self.u, "pro", self.prov, "/r")
        pay = billing.confirm(self.conn, self.prov, url.rsplit("/", 1)[1])
        self.assertEqual(pay["status"], "pending")
        self.assertEqual(self.plan_now(), "free")

    def test_free_and_promo_cannot_be_bought(self):
        for code in ("free", "promo", "beta"):
            with self.assertRaises(Refused):
                billing.start_purchase(self.conn, self.u, code, self.prov, "/r")
        with self.assertRaises(Refused):
            billing.start_purchase(self.conn, self.u, "once", self.prov, "/r", months=3)

    def test_three_months_price_and_monthly_limits(self):
        _, pay = self.buy("start", months=3)
        self.assertEqual(pay["amount_rub"], 7990)
        sub = self.sub()
        self.assertEqual(sub["months"], 3)
        self.assertEqual((sub["period_end"] - sub["period_start"]).days, 90)
        site = accounts.add_site(self.conn, self.u, "http://a.example", allow_private=True)
        for _ in range(2):
            jid = accounts.start_check(self.conn, self.u, site)
            self.conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (jid,))
        with self.assertRaises(Refused):
            accounts.start_check(self.conn, self.u, site)
        # прошло 31 день: второе окно из трёх — лимиты новые, срок тот же
        self.shift(31)
        self.conn.execute("UPDATE jobs SET created_at = created_at - interval '31 days'")
        accounts.start_check(self.conn, self.u, site)
        self.assertEqual(self.plan_now(), "start")

    def test_renewal_adds_to_current_term(self):
        self.buy("pro")
        end = self.sub()["period_end"]
        _, pay = self.buy("pro", months=3)
        self.assertEqual(pay["purpose"], "renewal")
        self.assertEqual(pay["amount_rub"], 13490)
        self.assertEqual(self.sub()["period_end"], end + timedelta(days=90))

    def test_once_is_not_bought_twice(self):
        self.buy("once")
        with self.assertRaises(Refused):
            billing.start_purchase(self.conn, self.u, "once", self.prov, "/r")

    def test_upgrade_credits_unused_days(self):
        self.buy("start")            # 2 990 ₽ за 30 дней
        self.shift(10)               # осталось 19 полных дней и часть дня — 19 × 99,67 ₽
        q = billing.quote(self.conn, self.u, "pro")
        self.assertEqual((q["kind"], q["credit"], q["amount"]), ("upgrade", 1893, 4990 - 1893))
        _, pay = self.buy("pro")
        self.assertEqual((pay["purpose"], pay["amount_rub"], pay["credit_rub"]),
                         ("upgrade", 3097, 1893))
        sub = self.sub()
        self.assertEqual(sub["plan"], "pro")
        self.assertEqual((sub["period_end"] - sub["period_start"]).days, 30, "новый срок с оплаты")
        # зачёт после перехода считается от полной цены срока, а не от доплаты
        self.shift(15)
        self.assertEqual(billing.quote(self.conn, self.u, "agency")["credit"], 4990 * 14 // 30)

    def test_downgrade_waits_for_term_end(self):
        self.buy("pro")
        with self.assertRaises(Refused) as ctx:
            billing.start_purchase(self.conn, self.u, "start", self.prov, "/r")
        self.assertIn("когда закончится", str(ctx.exception))
        self.shift(31)
        billing.renew_due(self.conn, self.prov)
        self.buy("start")
        self.assertEqual(self.plan_now(), "start")

    def test_site_limit_by_plan(self):
        accounts.add_site(self.conn, self.u, "http://a.example", allow_private=True)
        with self.assertRaises(Refused):
            accounts.add_site(self.conn, self.u, "http://b.example", allow_private=True)
        self.buy("start")
        accounts.add_site(self.conn, self.u, "http://b.example", allow_private=True)

    def test_free_check_once_per_30_days_and_no_ai(self):
        site = accounts.add_site(self.conn, self.u, "http://a.example", allow_private=True)
        jid = accounts.start_check(self.conn, self.u, site, "\n".join(f"q{i}" for i in range(9)))
        job = self.conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone()
        self.assertEqual(len(job["params"]["queries"]), 0)
        self.assertEqual(job["params"]["limits"]["answers"], 0)
        self.conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (jid,))
        with self.assertRaises(Refused) as ctx:
            accounts.start_check(self.conn, self.u, site)
        self.assertIn("раз в 30 дней", str(ctx.exception))

    def test_ai_budget_comes_from_ledger(self):
        self.buy("start")  # 10 ответов нейросети
        for _ in range(7):
            self.conn.execute("INSERT INTO spend (user_id, source, units, price_rub, cost_rub)"
                              " VALUES (%s, 'yandex-gen', 1, 5.08, 5.08)", (self.u["id"],))
        self.assertEqual(billing.audit_allowance(self.conn, self.u)["answers"], 3)

    def test_term_ends_without_charge_with_letters(self):
        self.buy("pro")
        self.shift(28)              # до конца двое суток
        out = billing.renew_due(self.conn, self.prov)
        self.assertEqual(out["reminded"], 1)
        billing.renew_due(self.conn, self.prov)
        [mail] = self.mails()
        self.assertIn("заканчивается", mail["subject"])
        self.assertIn("не продлится", mail["text"])
        self.assertIn("13 490 ₽", mail["text"], "в письме — цена трёх месяцев")
        self.shift(3)
        out = billing.renew_due(self.conn, self.prov)
        self.assertEqual((out["renewed"], out["expired"]), (0, 1))
        self.assertEqual(self.plan_now(), "free")
        self.assertIn("закончился", self.mails()[-1]["subject"])
        n = self.conn.execute("SELECT count(*) AS n FROM payments WHERE purpose = 'renewal'").fetchone()["n"]
        self.assertEqual(n, 0, "без согласия ничего не списываем")

    def test_auto_renew_needs_consent(self):
        self.buy("pro")
        with self.assertRaises(Refused):
            billing.set_auto_renew(self.conn, self.u, True, consent=False)
        billing.set_auto_renew(self.conn, self.u, True, consent=True)
        self.assertTrue(self.sub()["auto_renew"])
        self.assertIsNotNone(self.sub()["auto_renew_consent_at"])

    def test_auto_renew_success(self):
        self.buy("pro", months=3, auto_renew=True)
        self.assertTrue(self.sub()["auto_renew"])
        self.shift(88)
        billing.renew_due(self.conn, self.prov)
        self.assertIn("спишем 13 490 ₽", self.mails()[-1]["text"])
        self.shift(3)
        self.assertEqual(billing.renew_due(self.conn, self.prov)["renewed"], 1)
        sub = self.sub()
        self.assertEqual(sub["status"], "active")
        self.assertEqual(self.plan_now(), "pro")
        pay = self.conn.execute("SELECT * FROM payments WHERE purpose = 'renewal'"
                                " AND status = 'succeeded'").fetchone()
        self.assertEqual((pay["amount_rub"], pay["months"]), (13490, 3))
        self.assertEqual(billing.renew_due(self.conn, self.prov)["renewed"], 0, "не дважды")

    def test_failed_renewal_grace_then_free(self):
        self.buy("pro", method="decline_card", auto_renew=True)
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '1 minute'")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["failed"], 1)
        self.assertEqual(self.plan_now(), "pro", "льгота: тариф ещё работает")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["failed"], 0, "не чаще раза в сутки")
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '4 days'")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["expired"], 1)
        self.assertEqual(self.plan_now(), "free")

    def test_auto_renew_off_expires(self):
        self.buy("pro", auto_renew=True)
        billing.set_auto_renew(self.conn, self.u, False)
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '1 minute'")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["expired"], 1)
        self.assertEqual(self.plan_now(), "free")

    def test_once_plan_is_not_renewed(self):
        self.buy("once", auto_renew=True)
        sub = self.sub()
        self.assertFalse(sub["auto_renew"], "разовый не продлевается даже с отметкой")
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '1 minute'")
        out = billing.renew_due(self.conn, self.prov)
        self.assertEqual((out["renewed"], out["expired"]), (0, 1))


class BillingWebTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def test_buy_through_pages(self):
        with TestClient(self.app) as c:
            signup(self, c, "a@t.ru")
            page = c.get("/billing").text
            self.assertIn("Проверка", page)
            self.assertIn("7 990 ₽ за 3 месяца", page)
            r = c.post("/billing/buy", data={"choice": "pro:3", "csrf": csrf(page)})
            self.assertIn("виртуальный платёжный сервис", r.text)
            self.assertIn("13\xa0490", r.text)
            r = c.post(str(r.url), data={"decision": "pay", "csrf": csrf(r.text)})
            self.assertIn("Оплачено", r.text)
            page = c.get("/billing").text
            self.assertIn("ваш тариф", page)
            self.assertIn("Сам не продлится", page)
            self.assertIn("Оплачено на 3 месяца", page)
            self.assertIn("Доплата с зачётом", page)  # «Ультра» на 3 месяца — с зачётом «Про»
            self.assertIn('value="agency:3"', page)
            self.assertNotIn('value="agency:1"', page, "остаток больше цены месяца")
            self.assertNotIn('value="start:1"', page, "тариф ниже — после конца срока")
            self.assertIn("Ультра", page)

    def test_webhook_trusts_provider_not_body(self):
        with TestClient(self.app) as c:
            signup(self, c, "b@t.ru")
            page = c.get("/billing").text
            r = c.post("/billing/buy", data={"plan": "pro", "csrf": csrf(page)},
                       follow_redirects=False)
            pid = r.headers["location"].rsplit("/", 1)[1]
            # подделка: «оплачено» в теле, а у сервиса платёж не оплачен
            c.post("/pay/webhook/fake", json={"object": {"id": pid, "status": "succeeded"}})
            row = self.conn.execute("SELECT status FROM payments WHERE provider_id = %s",
                                    (pid,)).fetchone()
            self.assertEqual(row["status"], "pending")

    def test_other_user_cannot_see_payment(self):
        with TestClient(self.app) as a, TestClient(self.app) as b:
            signup(self, a, "c@t.ru")
            signup(self, b, "d@t.ru")
            page = a.get("/billing").text
            r = a.post("/billing/buy", data={"plan": "pro", "csrf": csrf(page)},
                       follow_redirects=False)
            loc = r.headers["location"]
            self.assertEqual(b.get(loc).status_code, 404)
            pay = self.conn.execute("SELECT id FROM payments").fetchone()
            self.assertEqual(b.get(f"/billing/return?payment={pay['id']}").status_code, 404)


class HistoryTest(PgTestCase):
    def result(self, issues, positions):
        return {"free": {"url": "http://x/", "collected_at": "2026-09-30T00:00:00+00:00",
                         "audit": {"pages_crawled": 3, "pages": [], "issues": issues},
                         "geo": {"issues": [], "bots": []}},
                "paid": {"positions": {"items": [{"query": q, "position": p}
                                                 for q, p in positions.items()]}}}

    def test_compare(self):
        i = lambda code, url: {"code": code, "url": url, "severity": "major",
                               "evidence": "e", "fix": "f"}
        prev = self.result([i("missing-description", "/a"), i("multiple-h1", "/a"),
                            i("multiple-h1", "/b")], {"ремонт": 8, "дизайн": None})
        cur = self.result([i("multiple-h1", "/a"), i("missing-title", "/c")],
                          {"ремонт": 3, "дизайн": 5})
        d = history.compare(prev, cur)
        self.assertEqual((d["fixed_count"], d["new_count"], d["kept_count"]), (2, 1, 1))
        self.assertEqual({m["query"]: m["delta"] for m in d["positions"]},
                         {"ремонт": 5, "дизайн": 94})

    def test_sparkline(self):
        self.assertEqual(history.sparkline([50]), "")
        self.assertIn("<polyline", history.sparkline([10, None, 40, 60]))
