"""Деньги на виртуальном платёжном сервисе: покупка, отказ, повтор уведомления, лимиты,
продление, неудачное списание и падение до бесплатного тарифа."""
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import accounts, billing, history, web
from yaseo_app.accounts import Refused

PASSWORD = "длинный-пароль-1"


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class BillingTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.prov = billing.FakeProvider(self.conn)
        self.u = self.user()

    def buy(self, plan="pro", pay=True, method="card_4242"):
        url = billing.start_purchase(self.conn, self.u, plan, self.prov, "/r?p={payment}")
        pid = url.rsplit("/", 1)[1]
        self.prov.decide(pid, pay, method)
        return pid, billing.confirm(self.conn, self.prov, pid)

    def plan_now(self):
        return billing.current(self.conn, self.u)["plan"]["code"]

    def test_default_is_free(self):
        self.assertEqual(self.plan_now(), "free")

    def test_purchase_activates_plan(self):
        _, pay = self.buy()
        self.assertEqual(pay["status"], "succeeded")
        self.assertEqual(self.plan_now(), "pro")
        u = self.conn.execute("SELECT plan FROM users WHERE id = %s", (self.u["id"],)).fetchone()
        self.assertEqual(u["plan"], "pro", "журнал расхода видит тариф через users.plan")

    def test_declined_payment_changes_nothing(self):
        _, pay = self.buy(pay=False)
        self.assertEqual(pay["status"], "canceled")
        self.assertEqual(self.plan_now(), "free")

    def test_repeated_notification_does_not_extend_twice(self):
        pid, _ = self.buy()
        end = billing.subscription(self.conn, self.u["id"])["period_end"]
        billing.confirm(self.conn, self.prov, pid)
        billing.confirm(self.conn, self.prov, pid)
        self.assertEqual(billing.subscription(self.conn, self.u["id"])["period_end"], end)

    def test_pending_is_not_activated(self):
        url = billing.start_purchase(self.conn, self.u, "pro", self.prov, "/r")
        pay = billing.confirm(self.conn, self.prov, url.rsplit("/", 1)[1])
        self.assertEqual(pay["status"], "pending")
        self.assertEqual(self.plan_now(), "free")

    def test_free_cannot_be_bought_and_same_plan_twice(self):
        with self.assertRaises(Refused):
            billing.start_purchase(self.conn, self.u, "free", self.prov, "/r")
        self.buy("start")
        with self.assertRaises(Refused):
            billing.start_purchase(self.conn, self.u, "start", self.prov, "/r")

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
        self.assertEqual(len(job["params"]["queries"]), 5)
        self.assertEqual(job["params"]["limits"]["answers"], 0)
        self.conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (jid,))
        with self.assertRaises(Refused) as ctx:
            accounts.start_check(self.conn, self.u, site)
        self.assertIn("раз в 30 дней", str(ctx.exception))

    def test_ai_budget_comes_from_ledger(self):
        self.buy("start")  # 10 проверок в нейросетях
        for _ in range(7):
            self.conn.execute("INSERT INTO spend (user_id, source, units, price_rub, cost_rub)"
                              " VALUES (%s, 'yandex-gen', 1, 5.08, 5.08)", (self.u["id"],))
        self.assertEqual(billing.audit_allowance(self.conn, self.u)["answers"], 3)

    def test_renewal_success(self):
        self.buy("pro")
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '1 minute'")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["renewed"], 1)
        sub = billing.subscription(self.conn, self.u["id"])
        self.assertEqual(sub["status"], "active")
        self.assertEqual(self.plan_now(), "pro")
        n = self.conn.execute("SELECT count(*) AS n FROM payments WHERE purpose = 'renewal'"
                              " AND status = 'succeeded'").fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(billing.renew_due(self.conn, self.prov)["renewed"], 0, "не дважды")

    def test_failed_renewal_grace_then_free(self):
        self.buy("pro", method="decline_card")
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '1 minute'")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["failed"], 1)
        self.assertEqual(self.plan_now(), "pro", "льгота: тариф ещё работает")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["failed"], 0, "не чаще раза в сутки")
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '4 days'")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["expired"], 1)
        self.assertEqual(self.plan_now(), "free")

    def test_auto_renew_off_expires(self):
        self.buy("pro")
        billing.set_auto_renew(self.conn, self.u, False)
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '1 minute'")
        self.assertEqual(billing.renew_due(self.conn, self.prov)["expired"], 1)
        self.assertEqual(self.plan_now(), "free")

    def test_once_plan_is_not_renewed(self):
        self.buy("once")
        sub = billing.subscription(self.conn, self.u["id"])
        self.assertFalse(sub["auto_renew"])
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
            r = c.post("/signup", data={"email": "a@t.ru", "password": PASSWORD, "consent": "yes"})
            page = c.get("/billing").text
            self.assertIn("Проверка", page)
            r = c.post("/billing/buy", data={"plan": "pro", "csrf": csrf(page)})
            self.assertIn("виртуальный платёжный сервис", r.text)
            r = c.post(str(r.url), data={"decision": "pay", "csrf": csrf(r.text)})
            self.assertIn("Оплачено", r.text)
            page = c.get("/billing").text
            self.assertIn("ваш тариф", page)
            self.assertIn("Продлится", page)

    def test_webhook_trusts_provider_not_body(self):
        with TestClient(self.app) as c:
            c.post("/signup", data={"email": "b@t.ru", "password": PASSWORD, "consent": "yes"})
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
            a.post("/signup", data={"email": "c@t.ru", "password": PASSWORD, "consent": "yes"})
            b.post("/signup", data={"email": "d@t.ru", "password": PASSWORD, "consent": "yes"})
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
