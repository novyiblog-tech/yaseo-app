"""Журнал расхода: цена, выключатели, лимиты, часовая квота, кэш."""
from pgtest import PgTestCase

from yaseo_app import ledger, sources
from yaseo_app.ledger import Meter


class Counting(sources.FakeWordstat):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def fetch(self, params):
        self.calls += 1
        return super().fetch(params)


class LedgerTest(PgTestCase):
    def meter(self, user, fake=False, plan="free"):
        return Meter(self.conn, user["id"], plan, fake=fake)

    def spent(self):
        return self.conn.execute("SELECT count(*) AS n, coalesce(sum(cost_rub), 0) AS rub"
                                 " FROM spend WHERE NOT cached").fetchone()

    def test_reserve_records_price(self):
        m = self.meter(self.user())
        sid = m.reserve("yandex-serp", 3, "q")
        m.settle(sid, True)
        row = self.conn.execute("SELECT * FROM spend WHERE id = %s", (sid,)).fetchone()
        self.assertEqual(float(row["cost_rub"]), round(3 * 0.0305, 4))
        self.assertTrue(row["ok"])

    def test_disabled_source(self):
        self.set_source("yandex-gen", enabled=False)
        with self.assertRaises(ledger.SourceOff):
            self.meter(self.user()).reserve("yandex-gen", 1)
        self.assertEqual(self.spent()["n"], 0)

    def test_plan_gate(self):
        self.set_source("yandex-gen", plans=["pro"])
        u = self.user()
        with self.assertRaises(ledger.PlanNotAllowed):
            self.meter(u, plan="free").reserve("yandex-gen", 1)
        self.meter(u, plan="pro").reserve("yandex-gen", 1)

    def test_per_user_daily_limit_is_per_user(self):
        self.set_source("yandex-serp", per_user_daily=2)
        a, b = self.user("a@t"), self.user("b@t")
        self.meter(a).reserve("yandex-serp", 2)
        with self.assertRaises(ledger.UserLimit):
            self.meter(a).reserve("yandex-serp", 1)
        self.meter(b).reserve("yandex-serp", 1)

    def test_service_ceiling(self):
        self.set_source("yandex-gen", service_daily_rub=10, per_user_daily=None)
        a, b = self.user("a@t"), self.user("b@t")
        self.meter(a).reserve("yandex-gen", 1)       # 5,08
        with self.assertRaises(ledger.ServiceCeiling):
            self.meter(b).reserve("yandex-gen", 1)   # 10,16 > 10

    def test_hourly_quota(self):
        self.set_source("wordstat", rate_per_hour=3, per_user_daily=None)
        m = self.meter(self.user())
        for _ in range(3):
            m.reserve("wordstat", 1)
        with self.assertRaises(ledger.RateLimited) as ctx:
            m.reserve("wordstat", 1)
        self.assertGreater(ctx.exception.retry_after.total_seconds(), 3000)
        self.conn.execute("UPDATE spend SET at = at - interval '61 minutes'")
        m.reserve("wordstat", 1)

    def test_fake_spend_does_not_touch_live_limits(self):
        self.set_source("wordstat", rate_per_hour=2, per_user_daily=None)
        u = self.user()
        for _ in range(2):
            self.meter(u, fake=True).reserve("wordstat", 1)
        self.meter(u, fake=False).reserve("wordstat", 1)

    def test_cache_hit_is_free_and_logged(self):
        u = self.user()
        src = Counting()
        params = {"phrase": "ремонт квартир", "region": 225}
        first = sources.get(self.conn, self.meter(u, fake=True), src, params)
        second = sources.get(self.conn, self.meter(u, fake=True), src, params)
        self.assertEqual(first, second)
        self.assertEqual(src.calls, 1)
        rows = self.conn.execute("SELECT cached, cost_rub FROM spend ORDER BY id").fetchall()
        self.assertEqual([r["cached"] for r in rows], [False, True])
        self.assertEqual(float(rows[1]["cost_rub"]), 0.0)

    def test_expired_cache_is_bought_again(self):
        u = self.user()
        src = Counting()
        params = {"phrase": "ремонт", "region": 225}
        sources.get(self.conn, self.meter(u, fake=True), src, params)
        self.conn.execute("UPDATE cache SET expires_at = now() - interval '1 second'")
        sources.get(self.conn, self.meter(u, fake=True), src, params)
        self.assertEqual(src.calls, 2)

    def test_disabled_source_does_not_serve_cache(self):
        u = self.user()
        params = {"phrase": "ремонт", "region": 225}
        sources.get(self.conn, self.meter(u, fake=True), Counting(), params)
        self.set_source("wordstat", enabled=False)
        with self.assertRaises(ledger.SourceOff):
            sources.get(self.conn, self.meter(u, fake=True), Counting(), params)

    def test_failed_call_stays_in_ledger(self):
        class Broken(sources.FakeWordstat):
            def fetch(self, params):
                raise RuntimeError("403")
        u = self.user()
        with self.assertRaises(sources.SourceError):
            sources.get(self.conn, self.meter(u, fake=True), Broken(), {"phrase": "x", "region": 1})
        row = self.conn.execute("SELECT ok, cost_rub FROM spend").fetchone()
        self.assertFalse(row["ok"])
        self.assertGreater(float(row["cost_rub"]), 0)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM cache").fetchone())

    def test_live_mode_requires_keys(self):
        with self.assertRaises(ValueError):
            sources.build("live", {})
