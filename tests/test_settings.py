"""Настройки кабинета по тарифам (таблица «yaseo — тарифы и настройки», столбец «с тарифа»)."""
import functools
import http.server
import os
import re
import threading
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pgtest import PgTestCase

from yaseo_app import (accounts, billing, free_audit, jobs, mailer, monitor, pipeline, prefs,
                       sources, team, watch, web, worker)
from yaseo_app.accounts import Refused

SITE = Path(__file__).parent / "site"
PASSWORD = "длинный-пароль-1"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35"
       b"\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("TRUNCATE sessions, login_attempts, site_health, yandex_links")

    def give(self, user, plan):
        self.conn.execute(
            "INSERT INTO subscriptions (user_id, plan, status, period_start, period_end)"
            " VALUES (%s, %s, 'active', now(), now() + interval '30 days')"
            " ON CONFLICT (user_id) DO UPDATE SET plan = excluded.plan, status = 'active',"
            " period_end = excluded.period_end", (user["id"], plan))
        self.conn.execute("UPDATE users SET plan = %s WHERE id = %s", (plan, user["id"]))

    def site(self, user, url="http://shop.example"):
        return accounts.add_site(self.conn, user, url, allow_private=True)

    def reload(self, site):
        return self.conn.execute("SELECT * FROM sites WHERE id = %s", (site["id"],)).fetchone()


class FeaturesTest(Base):
    def test_features_follow_sheet(self):
        u = self.user()
        want = {"free": set(), "once": set(),
                "start": {"region", "schedule", "alerts", "exclude", "gentle", "share", "webmaster"},
                "pro": {"region", "schedule", "alerts", "exclude", "gentle", "share", "webmaster",
                        "sections", "rivals", "brand"}}
        want["agency"] = want["pro"] | {"team"}
        for plan, on in want.items():
            if plan != "free":
                self.give(u, plan)
            got = {k for k, v in billing.features(self.conn, u).items() if v}
            self.assertEqual(got, on, plan)

    def test_free_cannot_change_site_settings(self):
        u = self.user()
        s = self.site(u)
        prefs.save_site(self.conn, u, s, {"region": "35", "schedule": "week", "rivals": "a.ru"})
        s = self.reload(s)
        self.assertEqual((s["region"], s["schedule"], s["rivals"]), (225, "off", []))

    def test_region_moves_tracked_queries_and_goes_into_check(self):
        u = self.user()
        self.give(u, "start")
        s = self.site(u)
        monitor.add_queries(self.conn, u, s, "ремонт квартир")
        prefs.save_site(self.conn, u, s, {"region": "35"})
        s = self.reload(s)
        self.assertEqual(s["region"], 35)
        self.assertEqual(monitor.tracked(self.conn, s["id"])[0]["region"], 35)
        jid = accounts.start_check(self.conn, u, s, "ремонт")
        self.assertEqual(self.conn.execute("SELECT params FROM jobs WHERE id = %s",
                                           (jid,)).fetchone()["params"]["region"], 35)
        with self.assertRaises(Refused):
            prefs.save_site(self.conn, u, s, {"region": "99999"})
        # тариф упал — настройка хранится, но не действует
        self.conn.execute("DELETE FROM subscriptions")
        self.assertEqual(prefs.effective(self.conn, u, s)["region"], 225)

    def test_exclude_patterns(self):
        pats = prefs.parse_exclude("/cart\nhttps://shop.example/personal/\n*?sort=*\n\n/cart",
                                   "http://shop.example/")
        self.assertEqual(pats, ["/cart", "/personal/", "*?sort=*"])
        ex = free_audit.excluded
        self.assertTrue(ex("http://shop.example/cart", pats))
        self.assertTrue(ex("http://shop.example/cart/1", pats))
        self.assertFalse(ex("http://shop.example/cartoon", pats))
        self.assertTrue(ex("http://shop.example/personal/orders", pats))
        self.assertTrue(ex("http://shop.example/catalog?sort=price", pats))
        self.assertFalse(ex("http://shop.example/catalog", pats))
        self.assertTrue(ex("http://x/catalog/tv/filter?a=1", ["/catalog/*/filter"]))
        for bad in ("/", "cart", "https://other.example/cart"):
            with self.assertRaises(Refused):
                prefs.parse_exclude(bad, "http://shop.example/")

    def test_rivals_parsed_and_capped(self):
        got = prefs.parse_rivals("https://www.Rival.ru/page, shop.example\nother.ru",
                                 "http://shop.example/")
        self.assertEqual(got, ["rival.ru", "other.ru"], "свой сайт — не конкурент")
        with self.assertRaises(Refused):
            prefs.parse_rivals("\n".join(f"r{i}.ru" for i in range(6)), "http://shop.example/")


class SiteCrawlTest(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
        handler.log_message = lambda *a, **k: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        super().tearDownClass()

    def test_excluded_section_is_not_crawled(self):
        full = free_audit.run_isolated(self.url, max_pages=10, allow_private=True)
        cut = free_audit.run_isolated(self.url, max_pages=10, allow_private=True,
                                      exclude=["/prices.html"])
        urls = lambda r: {p["url"] for p in r["audit"]["pages"]}
        self.assertTrue(any(u.endswith("/prices.html") for u in urls(full)))
        self.assertFalse(any(u.endswith("/prices.html") for u in urls(cut)))
        self.assertEqual(cut["exclude"], ["/prices.html"])

    def test_sections_off_and_rivals_in_check(self):
        u = self.user()
        self.give(u, "pro")
        s = self.site(u, self.url)
        prefs.save_site(self.conn, u, s, {"sec_demand": "yes", "sec_positions": "yes",
                                          "rivals": "avito.ru\n2gis.ru", "exclude": "",
                                          "gentle": "yes"})
        jid = accounts.start_check(self.conn, u, self.reload(s), "ремонт квартир\nдизайн",
                                   max_pages=3)
        job = self.conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone()
        self.assertEqual(job["params"]["off"], ["answers"])
        self.assertTrue(job["params"]["gentle"])
        worker.run_once(self.conn, "w", sources.build("fake"), allow_private=True)
        r = self.conn.execute("SELECT result FROM jobs WHERE id = %s", (jid,)).fetchone()["result"]
        self.assertEqual(r["paid"]["answers"]["status"], "off")
        self.assertEqual(r["rivals"], ["avito.ru", "2gis.ru"])
        item = r["paid"]["positions"]["items"][0]
        self.assertEqual(set(item["watched"]), {"avito.ru", "2gis.ru"})
        spent = self.conn.execute("SELECT count(*) AS n FROM spend WHERE source = 'yandex-gen'"
                                  ).fetchone()["n"]
        self.assertEqual(spent, 0, "выключенная нейросеть не тратится")


class ShareBrandTest(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def login(self, c, email):
        c.post("/login", data={"email": email, "password": PASSWORD})

    def owner(self, email="o@t.ru", plan="pro"):
        u = self.conn.execute(
            "INSERT INTO users (email, password_hash, email_confirmed_at) VALUES (%s, %s, now())"
            " RETURNING *", (email, accounts.hash_password(PASSWORD))).fetchone()
        self.give(u, plan)
        return u

    def done_job(self, u):
        import json
        s = self.site(u)
        data = json.loads((Path(web.HERE) / "examples" / "test-site.json").read_text())
        jid = jobs.enqueue(self.conn, u["id"], "audit", {"url": s["url"], "max_pages": 5},
                           site_id=s["id"])
        from psycopg.types.json import Jsonb
        self.conn.execute("UPDATE jobs SET status = 'done', result = %s WHERE id = %s",
                          (Jsonb({"free": data, "queries": [], "paid": {
                              k: {"status": "skipped", "items": []}
                              for k in ("demand", "positions", "answers")}}), jid))
        return jid

    def test_share_link_without_login(self):
        u = self.owner()
        jid = self.done_job(u)
        with TestClient(self.app) as c:
            self.login(c, "o@t.ru")
            page = c.get(f"/jobs/{jid}").text
            c.post(f"/jobs/{jid}/share", data={"csrf": csrf(page)})
            page = c.get(f"/jobs/{jid}").text
            link = re.search(r'value="[^"]*(/r/[^"]+)"', page).group(1)
        with TestClient(self.app) as anon:
            r = anon.get(link)
            self.assertEqual(r.status_code, 200)
            self.assertIn("Проверка сайта", r.text)
            self.assertEqual(r.headers.get("x-robots-tag"), "noindex, nofollow")
            self.conn.execute("DELETE FROM subscriptions")
            self.assertEqual(anon.get(link).status_code, 404, "тариф упал — ссылка не работает")

    def test_site_page_shows_summary_and_settings_page_saves(self):
        u = self.owner()
        s = self.site(u)
        with TestClient(self.app) as c:
            self.login(c, "o@t.ru")
            page = c.get(f"/sites/{s['id']}").text
            self.assertIn(f'href="/sites/{s["id"]}/settings"', page)
            self.assertNotIn('name="rivals"', page, "форма — на своей странице, тут сводка")
            form = c.get(f"/sites/{s['id']}/settings").text
            self.assertIn('name="rivals"', form)
            bad = c.post(f"/sites/{s['id']}/settings", data={
                "csrf": csrf(form), "region": "35", "schedule": "week", "rivals": "a.ru, b, c"})
            self.assertEqual(bad.status_code, 400)
            self.assertIn('name="rivals"', bad.text, "ошибка — на той же странице настроек")
            c.post(f"/sites/{s['id']}/settings", data={
                "csrf": csrf(form), "region": "35", "schedule": "week", "rivals": "a.ru"})
            page = c.get(f"/sites/{s['id']}").text
        self.assertIn("Краснодар", page)
        self.assertIn("раз в неделю", page)

    def test_free_site_summary_points_to_plans(self):
        u = self.owner(plan="free")
        s = self.site(u)
        with TestClient(self.app) as c:
            self.login(c, "o@t.ru")
            page = c.get(f"/sites/{s['id']}").text
        self.assertIn("с «Старт»", page)
        self.assertIn("с «Про»", page)
        self.assertNotIn(f"/sites/{s['id']}/settings", page, "менять нечего — ведём на тарифы")

    def test_free_cannot_share(self):
        u = self.owner(plan="once")
        jid = self.done_job(u)
        job = self.conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone()
        with self.assertRaises(Refused):
            prefs.share(self.conn, u, job)

    def test_brand_in_report(self):
        u = self.owner()
        jid = self.done_job(u)
        with self.assertRaises(Refused):
            prefs.save_brand(self.conn, u, "Студия", "", b"<svg onload=alert(1)>")
        with TestClient(self.app) as c:
            self.login(c, "o@t.ru")
            page = c.get("/settings").text
            r = c.post("/settings/brand", data={"brand_name": "Студия Ромашка",
                                                "brand_contacts": "+7 900 000-00-00",
                                                "csrf": csrf(page)},
                       files={"logo": ("logo.png", PNG, "image/png")})
            self.assertEqual(r.status_code, 200)
            rep = c.get(f"/jobs/{jid}/report").text
        self.assertIn("Студия Ромашка", rep)
        self.assertIn("data:image/png;base64,", rep)
        self.assertIn("000-00-00", rep)
        self.assertNotIn('<span class="ya">ya</span>seo', rep)
        self.give(u, "start")
        self.assertIsNone(prefs.brand(self.conn, u), "на «Старт» — отчёт со знаком yaseo")


class TeamTest(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def test_member_works_in_owner_cabinet(self):
        owner = self.conn.execute(
            "INSERT INTO users (email, password_hash, email_confirmed_at) VALUES ('o@t.ru', %s, now())"
            " RETURNING *", (accounts.hash_password(PASSWORD),)).fetchone()
        self.give(owner, "pro")
        with self.assertRaises(Refused):
            team.invite(self.conn, owner, "m@t.ru")
        self.give(owner, "agency")
        self.site(owner)
        team.invite(self.conn, owner, "m@t.ru")
        team.invite(self.conn, owner, "m2@t.ru")
        with self.assertRaises(Refused):
            team.invite(self.conn, owner, "m3@t.ru")
        mail = self.conn.execute("SELECT * FROM outbox WHERE to_email = 'm@t.ru'").fetchone()
        token = re.search(r"t=([\w-]+)", mail["text"]).group(1)
        with TestClient(self.app) as c:
            r = c.post("/team/join", data={"t": token, "password": PASSWORD, "consent": "yes"})
            self.assertIn("shop.example", r.text, "коллега видит сайты владельца")
            self.assertIn("m@t.ru", r.text)
            page = c.get("/billing").text
            r = c.post("/billing/buy", data={"choice": "agency:3", "csrf": csrf(page)})
            self.assertEqual(r.status_code, 403, "оплата — только у владельца")
            self.assertIn("владелец", c.get("/settings").text)
            self.assertEqual(c.post("/team/join", data={"t": token, "password": PASSWORD,
                                                        "consent": "yes"}).status_code, 400,
                             "ссылка одноразовая")
        self.give(owner, "pro")
        with TestClient(self.app) as c:
            r = c.post("/login", data={"email": "m@t.ru", "password": PASSWORD})
            self.assertIn("выключен", r.text)
        member = self.conn.execute("SELECT id FROM users WHERE email = 'm@t.ru'").fetchone()
        team.remove(self.conn, owner, member["id"])
        self.assertEqual(len(team.members(self.conn, owner)), 1)


class YandexTest(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web.create_app(cls.dsn, allow_private=True, secure_cookies=False)

    def setUp(self):
        super().setUp()
        os.environ["YASEO_YANDEX_OAUTH"] = "fake"

    def tearDown(self):
        os.environ.pop("YASEO_YANDEX_OAUTH", None)

    def test_connect_and_show_without_storing(self):
        u = self.conn.execute(
            "INSERT INTO users (email, password_hash, email_confirmed_at) VALUES ('y@t.ru', %s, now())"
            " RETURNING *", (accounts.hash_password(PASSWORD),)).fetchone()
        self.give(u, "start")
        s = self.site(u)
        with TestClient(self.app) as c:
            c.post("/login", data={"email": "y@t.ru", "password": PASSWORD})
            page = c.get("/settings").text
            self.assertIn("Подключить Яндекс", page)
            self.assertEqual(c.get("/settings/yandex/callback?code=x&state=wrong").status_code, 403)
            r = c.post("/settings/yandex/connect", data={"csrf": csrf(page)})
            self.assertIn("Подключён Яндекс ID", r.text)
            r = c.get(f"/sites/{s['id']}/yandex")
            self.assertIn("страниц в поиске", r.text)
            self.assertIn("из поисковых систем", r.text)
        tables = {r["table_name"] for r in self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        self.assertFalse({t for t in tables if "webmaster" in t or "metrika" in t},
                         "данных Вебмастера и Метрики не храним")


class WatchTest(Base):
    def mails(self, kind):
        return self.conn.execute("SELECT * FROM outbox WHERE kind = %s ORDER BY id",
                                 (kind,)).fetchall()

    def test_autocheck_by_schedule_and_letter(self):
        u = self.user()
        self.give(u, "start")
        s = self.site(u)
        prefs.save_site(self.conn, u, s, {"schedule": "week"})
        jid = accounts.start_check(self.conn, u, self.reload(s), "ремонт")
        self.conn.execute("UPDATE jobs SET status = 'done', score = 60 WHERE id = %s", (jid,))
        today = date.today()
        self.assertEqual(watch.queue_autochecks(self.conn, today), 0, "неделя не прошла")
        self.conn.execute("UPDATE jobs SET created_at = now() - interval '8 days'")
        self.conn.execute("UPDATE sites SET schedule_tried = NULL")
        self.assertEqual(watch.queue_autochecks(self.conn, today), 1)
        self.assertEqual(watch.queue_autochecks(self.conn, today), 0, "одна попытка в день")
        auto = self.conn.execute("SELECT * FROM jobs WHERE params->>'auto' = 'true'").fetchone()
        self.assertEqual(auto["params"]["queries"], ["ремонт"], "фразы прошлой проверки")
        self.conn.execute("UPDATE jobs SET status = 'done', score = 70 WHERE id = %s", (auto["id"],))
        watch.after_audit(self.conn, auto)
        [mail] = self.mails("autocheck")
        self.assertIn("70 из 100", mail["subject"])
        self.assertIn("выросла на 10", mail["text"])

    def test_site_down_then_up(self):
        u = self.user()
        self.give(u, "start")
        s = self.site(u, "http://127.0.0.1:9/")
        n = watch.queue_health(self.conn, __import__("datetime").datetime(2026, 10, 1, 10))
        self.assertEqual(n, 1)
        job = {"site_id": s["id"], "user_id": u["id"]}
        watch.run_health(self.conn, job, allow_private=True)
        self.assertEqual(self.mails("alert"), [], "один неудачный заход — ещё не тревога")
        watch.run_health(self.conn, job, allow_private=True)
        [down] = self.mails("alert")
        self.assertIn("не открывается", down["subject"])
        watch.run_health(self.conn, job, allow_private=True)
        self.assertEqual(len(self.mails("alert")), 1, "второй раз не пишем")
        self.conn.execute("UPDATE sites SET url = %s WHERE id = %s",
                          ("http://127.0.0.1:1/", s["id"]))
        # сайт «открылся»: подменяем проверку ответом 200
        from yaseo_app import site_checks
        real = site_checks.fetch
        site_checks.fetch = lambda *a, **k: {"status": 200, "error": None}
        try:
            watch.run_health(self.conn, job, allow_private=True)
        finally:
            site_checks.fetch = real
        self.assertIn("снова открывается", self.mails("alert")[-1]["subject"])

    def test_alerts_only_on_plan_and_when_on(self):
        u = self.user()
        self.site(u)
        from datetime import datetime
        self.assertEqual(watch.queue_health(self.conn, datetime(2026, 10, 1, 10)), 0)
        self.give(u, "start")
        self.conn.execute("UPDATE users SET alerts = false")
        self.assertEqual(watch.queue_health(self.conn, datetime(2026, 10, 1, 11)), 0)

    def test_position_drop_letter(self):
        u = self.user()
        self.give(u, "start")
        s = self.site(u)
        monitor.add_queries(self.conn, u, s, "ремонт\nдизайн\nплитка")
        today = date(2026, 10, 1)
        rows = {"ремонт": (3, None), "дизайн": (4, 12), "плитка": (5, 6)}
        for q, (before, now) in rows.items():
            for d, pos in ((today - timedelta(days=1), before), (today, now)):
                self.conn.execute("INSERT INTO positions (site_id, query, region, day, position)"
                                  " VALUES (%s, %s, 225, %s, %s)", (s["id"], q, d, pos))
        fell = watch.drops(self.conn, s["id"], today)
        self.assertEqual({d["query"] for d in fell}, {"ремонт", "дизайн"})
        u = self.conn.execute("SELECT * FROM users WHERE id = %s", (u["id"],)).fetchone()
        watch.check_positions(self.conn, u, s, today)
        watch.check_positions(self.conn, u, s, today)
        [mail] = self.mails("alert")
        self.assertIn("ремонт: 3 → вне десятки", mail["text"])

    def test_digest_setting_kept_when_plan_lacks_it(self):
        app = web.create_app(self.dsn, allow_private=True, secure_cookies=False)
        self.conn.execute(
            "INSERT INTO users (email, password_hash, email_confirmed_at) VALUES ('d@t.ru', %s, now())",
            (accounts.hash_password(PASSWORD),))
        with TestClient(app) as c:
            c.post("/login", data={"email": "d@t.ru", "password": PASSWORD})
            page = c.get("/settings").text
            self.assertIn("с тарифа «Старт»", page)
            c.post("/settings", data={"csrf": csrf(page)})
        row = self.conn.execute("SELECT weekly_digest, alerts FROM users").fetchone()
        self.assertEqual((row["weekly_digest"], row["alerts"]), (True, True))
