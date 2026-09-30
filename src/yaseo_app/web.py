"""Кабинет: страницы на сервере, тёмная тема. Логика — в accounts.py.

    uv run uvicorn yaseo_app.web:app --reload

YASEO_ALLOW_PRIVATE=1 — пускать локальные адреса (тестовый сайт), только для разработки.
YASEO_INSECURE_COOKIES=1 — cookie без Secure, для http://localhost.
"""
from __future__ import annotations

import functools
import hashlib
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException

import subprocess
import tempfile

from yaseo_app import accounts, billing, db, legal, history, mailer, monitor, report, score, verify
from yaseo_app import promo as promos
from yaseo_app.accounts import Refused

HERE = Path(__file__).parent
IMAGES = HERE / "design" / "img"
COOKIE = "yaseo_session"
PUBLIC_PATHS = {"/", "/example", "/robots.txt", "/sitemap.xml"}
RENDER = HERE.parent.parent / "scripts" / "chrome-render.sh"
SECTION_TITLES = {"demand": "Сколько людей это ищут", "positions": "Ваше место в Яндексе",
                  "answers": "Упоминает ли вас нейросеть Яндекса"}
SECTION_HINTS = {
    "demand": "Сколько раз в месяц эти фразы набирают в Яндексе — по данным сервиса Яндекс Вордстат.",
    "positions": "На каком месте ваш сайт в поиске Яндекса по каждой фразе и кто стоит в самом верху.",
    "answers": "Называет ли нейросеть Яндекса ваш сайт, когда отвечает на эти фразы, и кого называет вместо вас.",
}
STATUS_TITLES = {"queued": "в очереди", "running": "идёт", "done": "готово",
                 "failed": "не удалась"}
SECTION_STATUS = {"ok": "собран", "partial": "собран частично", "skipped": "пропущен",
                  "failed": "не собран", "off": "выключен"}

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


def client_ip(request: Request) -> str | None:
    """Адрес посетителя. За обратным прокси uvicorn запускается с --proxy-headers."""
    import ipaddress
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(str(v)):.2f}".replace(".", ",") + " ₽"


def _img_url(name: str) -> str:
    """Адрес картинки с отпечатком содержимого: перерисовали — адрес сменился, кэш не мешает."""
    return f"/img/{name}?v={_img_digest(name, (IMAGES / name).stat().st_mtime_ns)}"


@functools.lru_cache(maxsize=32)
def _img_digest(name: str, mtime_ns: int) -> str:
    return hashlib.sha256((IMAGES / name).read_bytes()).hexdigest()[:10]


def _templates() -> Environment:
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    env.filters["money"] = _money
    env.filters["img"] = _img_url
    env.filters["rub"] = billing.money
    env.filters["num"] = lambda v: f"{v:,}".replace(",", "\u00a0") if isinstance(v, int) else v
    env.filters["dt"] = lambda d: d.astimezone().strftime("%d.%m.%Y %H:%M") if d else "—"
    env.globals["color_of"] = score.color_of
    env.filters["plural"] = report.plural
    env.globals.update(STATUS_TITLES=STATUS_TITLES, SECTION_TITLES=SECTION_TITLES,
                       SECTION_HINTS=SECTION_HINTS,
                       SECTION_STATUS=SECTION_STATUS)
    return env


def _eta_seconds(job: dict) -> float:
    """Примерная длительность проверки: обход страниц плюс запросы к данным Яндекса.
    По первым проверкам на боевом сервере: 31 страница и 3 виртуальных запроса — 16 с."""
    p = job["params"]
    pages = int(p.get("max_pages", 20))
    queries = len(p.get("queries") or [])
    per_query = 1 if os.environ.get("YASEO_SOURCES", "fake") == "fake" else 8
    return 8 + 0.6 * pages + per_query * queries


def _progress(c, job: dict) -> dict:
    if job["status"] == "queued":
        ahead = c.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('queued', 'running')"
                          " AND id < %s", (job["id"],)).fetchone()["n"]
        return {"queued": True, "ahead": ahead}
    row = c.execute("SELECT extract(epoch FROM now() - started_at) AS el FROM jobs WHERE id = %s",
                    (job["id"],)).fetchone()
    elapsed = float(row["el"] or 0)
    eta = _eta_seconds(job)
    return {"queued": False, "pct": max(3, min(95, int(elapsed / eta * 100))),
            "left": _duration(max(0.0, eta - elapsed)), "over": elapsed > eta}


def _duration(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return f"{max(5, (sec + 4) // 5 * 5)} сек"
    return f"{(sec + 59) // 60} мин"


def create_app(dsn: str | None = None, allow_private: bool | None = None,
               secure_cookies: bool | None = None) -> FastAPI:
    if allow_private is None:
        allow_private = os.environ.get("YASEO_ALLOW_PRIVATE") == "1"
    if secure_cookies is None:
        secure_cookies = os.environ.get("YASEO_INSECURE_COOKIES") != "1"
    tpl = _templates()
    css = (HERE / "design" / "tokens.css").read_text(encoding="utf-8") + "\n" + \
        (HERE / "design" / "cabinet.css").read_text(encoding="utf-8")

    @asynccontextmanager
    async def lifespan(app):
        pool = ConnectionPool(dsn or db.dsn(), min_size=1, max_size=10,
                              kwargs={"autocommit": True, "row_factory": dict_row})
        with pool.connection() as conn:
            db.migrate(conn)
        app.state.pool = pool
        yield
        pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = ("public, max-age=86400" if request.url.path.startswith("/img/")
                                         else "no-store")
        if request.url.path not in PUBLIC_PATHS and not request.url.path.startswith("/legal/"):
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    def conn(request: Request):
        with request.app.state.pool.connection() as c:
            yield c

    def page(request: Request, name: str, user=None, status: int = 200, **ctx) -> HTMLResponse:
        html = tpl.get_template(name).render(css=css, user=user, path=request.url.path, **ctx)
        return HTMLResponse(html, status_code=status)

    def current(request: Request, c=Depends(conn)) -> dict:
        user = accounts.session_user(c, request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return user

    def check_csrf(user: dict, token: str) -> None:
        if not token or token != user["csrf"]:
            raise HTTPException(status_code=403, detail="Форма устарела, обновите страницу.")

    def signed_in(token: str) -> RedirectResponse:
        resp = RedirectResponse("/sites", status_code=303)
        resp.set_cookie(COOKIE, token, max_age=int(accounts.SESSION_TTL.total_seconds()),
                        httponly=True, secure=secure_cookies, samesite="lax")
        return resp

    @app.get("/")
    def index(request: Request, c=Depends(conn)):
        user = accounts.session_user(c, request.cookies.get(COOKIE))
        if user:
            return RedirectResponse("/sites", status_code=303)
        return landing_page(request, c)

    @app.get("/signup")
    def signup_form(request: Request, promo: str = "", invite: str = "", site: str = ""):
        # invite — ссылки из писем времён беты
        return page(request, "cabinet/signup.html.j2", promo=promo or invite, site=site)

    @app.post("/signup")
    def signup(request: Request, email: str = Form(""), password: str = Form(""),
               consent: str = Form(""), offer: str = Form(""), promo: str = Form(""),
               site: str = Form(""), c=Depends(conn)):
        try:
            user = accounts.signup(c, email, password, consent == "yes",
                                   ip=client_ip(request), promo=promo or None,
                                   offer=offer == "yes")
        except Refused as exc:
            return page(request, "cabinet/signup.html.j2", error=str(exc), email=email,
                        promo=promo, site=site, status=400)
        target = "/sites"
        if site.strip():
            # Адрес, введённый на лендинге, сразу становится сайтом в кабинете.
            try:
                s_row = accounts.add_site(c, user, site, allow_private=allow_private)
                target = f"/sites/{s_row['id']}"
            except Refused:
                pass
        resp = signed_in(accounts.open_session(c, user["id"]))
        resp.headers["Location"] = target
        return resp

    # --- лендинг --------------------------------------------------------------------

    def landing_page(request, c, status=200, **ctx):
        return page(request, "landing/index.html.j2", status=status,
                    plans=billing.plans(c), vat_note=legal.requisites()["vat_note"],
                    promo_plan=billing.plan(c, "promo"),
                    request_base=str(request.base_url).rstrip("/"), **ctx)

    @app.get("/example")
    def example():
        """Пример отчёта — настоящий прогон по тестовому сайту из tests/site."""
        import json
        data = json.loads((HERE / "examples" / "test-site.json").read_text(encoding="utf-8"))
        return HTMLResponse(report.render(data, kind="Пример на тестовом сайте"))

    @app.get("/legal/offer")
    def offer(request: Request):
        return page(request, "landing/offer.html.j2", req=legal.requisites(),
                    version=legal.OFFER_VERSION)

    @app.get("/legal/privacy")
    def privacy(request: Request):
        return page(request, "landing/legal.html.j2",
                    title="Политика обработки персональных данных")

    @app.get("/img/{name}")
    def image(name: str):
        """Картинки оформления из design/img: только готовые webp, без путей."""
        path = IMAGES / name
        if not name.endswith(".webp") or "/" in name or name.startswith(".") or not path.is_file():
            raise HTTPException(status_code=404)
        return Response(path.read_bytes(), media_type="image/webp")

    @app.get("/robots.txt")
    def robots(request: Request):
        base = str(request.base_url).rstrip("/")
        return Response("User-agent: *\nAllow: /$\nAllow: /example\nAllow: /legal/\n"
                        "Disallow: /\n\nSitemap: " + base + "/sitemap.xml\n",
                        media_type="text/plain")

    @app.get("/sitemap.xml")
    def sitemap(request: Request):
        base = str(request.base_url).rstrip("/")
        urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in ("/", "/example"))
        return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns='
                        '"http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + "</urlset>",
                        media_type="application/xml")

    @app.get("/confirm")
    def confirm(request: Request, t: str = "", c=Depends(conn)):
        user = verify.confirm(c, t)
        return page(request, "cabinet/confirmed.html.j2", ok=user is not None,
                    user=accounts.session_user(c, request.cookies.get(COOKIE)))

    @app.post("/confirm/resend")
    def confirm_resend(request: Request, csrf: str = Form(""), user=Depends(current),
                       c=Depends(conn)):
        check_csrf(user, csrf)
        sent = verify.send_confirmation(c, user)
        return page(request, "cabinet/notice.html.j2", user, title="Письмо",
                    text=("Отправили письмо ещё раз на " + user["email"] + "." if sent else
                          "Письмо уже отправлено недавно. Проверьте папку «Спам» или "
                          "попробуйте через минуту."))

    @app.get("/forgot")
    def forgot_form(request: Request):
        return page(request, "cabinet/forgot.html.j2")

    @app.post("/forgot")
    def forgot(request: Request, email: str = Form(""), c=Depends(conn)):
        verify.request_reset(c, email)
        return page(request, "cabinet/notice.html.j2", title="Проверьте почту",
                    text="Если такой адрес зарегистрирован, на него ушло письмо со ссылкой. "
                         "Ссылка действует час.")

    @app.get("/reset")
    def reset_form(request: Request, t: str = "", c=Depends(conn)):
        if not verify.reset_valid(c, t):
            return page(request, "cabinet/notice.html.j2", status=400, title="Ссылка устарела",
                        text="Ссылка уже использована или прошёл час. Запросите новую на "
                             "странице входа.")
        return page(request, "cabinet/reset.html.j2", token=t)

    @app.post("/reset")
    def reset(request: Request, t: str = Form(""), password: str = Form(""), c=Depends(conn)):
        try:
            user = verify.reset(c, t, password)
        except Refused as exc:
            return page(request, "cabinet/reset.html.j2", status=400, token=t, error=str(exc))
        return signed_in(accounts.open_session(c, user["id"]))

    @app.get("/login")
    def login_form(request: Request):
        return page(request, "cabinet/login.html.j2")

    @app.post("/login")
    def login(request: Request, email: str = Form(""), password: str = Form(""),
              c=Depends(conn)):
        try:
            user = accounts.login(c, email, password)
        except Refused as exc:
            return page(request, "cabinet/login.html.j2", error=str(exc), email=email,
                        status=400)
        return signed_in(accounts.open_session(c, user["id"]))

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form(""), user=Depends(current),
               c=Depends(conn)):
        check_csrf(user, csrf)
        accounts.close_session(c, request.cookies.get(COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/sites")
    def sites(request: Request, user=Depends(current), c=Depends(conn)):
        return page(request, "cabinet/sites.html.j2", user, sites=accounts.list_sites(c, user))

    @app.post("/sites")
    def add_site(request: Request, url: str = Form(""), csrf: str = Form(""),
                 user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        try:
            site = accounts.add_site(c, user, url, allow_private=allow_private)
        except Refused as exc:
            return page(request, "cabinet/sites.html.j2", user, status=400, error=str(exc),
                        url=url, sites=accounts.list_sites(c, user))
        return RedirectResponse(f"/sites/{site['id']}", status_code=303)

    def own_site(c, user, site_id: int) -> dict:
        site = accounts.get_site(c, user, site_id)
        if site is None:
            raise HTTPException(status_code=404)
        return site

    @app.get("/sites/{site_id}")
    def site_page(request: Request, site_id: int, user=Depends(current), c=Depends(conn)):
        return site_view(request, c, user, own_site(c, user, site_id))

    def site_view(request, c, user, site, status=200, **extra):
        return page(request, "cabinet/site.html.j2", user, site=site, status=status,
                    jobs=accounts.site_jobs(c, site["id"]),
                    positions=monitor.site_table(c, site["id"], monitor.msk_today(c)),
                    quota=monitor.quota(c, user), plan=billing.current(c, user)["plan"], **extra)

    @app.post("/sites/{site_id}/track")
    def track(request: Request, site_id: int, queries: str = Form(""), csrf: str = Form(""),
              user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        site = own_site(c, user, site_id)
        try:
            monitor.add_queries(c, user, site, queries)
        except Refused as exc:
            return site_view(request, c, user, site, status=400, track_error=str(exc),
                             track_text=queries)
        return RedirectResponse(f"/sites/{site_id}#positions", status_code=303)

    @app.post("/sites/{site_id}/track/{query_id}/delete")
    def untrack(site_id: int, query_id: int, csrf: str = Form(""),
                user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        own_site(c, user, site_id)
        monitor.remove_query(c, user, query_id)
        return RedirectResponse(f"/sites/{site_id}#positions", status_code=303)

    def settings_page(request, c, user, status=200, **extra):
        return page(request, "cabinet/settings.html.j2", user, status=status, dev=allow_private,
                    cur=billing.current(c, user), **extra)

    @app.get("/settings")
    def settings(request: Request, user=Depends(current), c=Depends(conn)):
        return settings_page(request, c, user)

    @app.post("/settings/renew")
    def settings_renew(request: Request, on: str = Form(""), consent: str = Form(""),
                       csrf: str = Form(""), user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        try:
            billing.set_auto_renew(c, user, on == "yes", consent=consent == "yes")
        except Refused as exc:
            return settings_page(request, c, user, status=400, renew_error=str(exc))
        return RedirectResponse("/settings#renew", status_code=303)

    @app.post("/settings")
    def settings_save(weekly_digest: str = Form(""), csrf: str = Form(""),
                      user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        c.execute("UPDATE users SET weekly_digest = %s WHERE id = %s",
                  (weekly_digest == "yes", user["id"]))
        return RedirectResponse("/settings", status_code=303)

    @app.get("/unsubscribe")
    def unsubscribe(request: Request, u: int = 0, t: str = "", c=Depends(conn)):
        """Отписка по ссылке из письма, без входа. Ссылка подписана ключом сервиса."""
        if not mailer.check_unsubscribe(u, t):
            raise HTTPException(status_code=404)
        c.execute("UPDATE users SET weekly_digest = false WHERE id = %s", (u,))
        return page(request, "cabinet/unsubscribed.html.j2")

    # письма на машине разработчика: посмотреть, что ушло бы пользователю
    @app.get("/dev/outbox")
    def dev_outbox(request: Request, user=Depends(current), c=Depends(conn)):
        if not allow_private:
            raise HTTPException(status_code=404)
        rows = c.execute("SELECT id, subject, kind, status, created_at FROM outbox"
                         " WHERE user_id = %s ORDER BY id DESC LIMIT 30", (user["id"],)).fetchall()
        return page(request, "cabinet/outbox.html.j2", user, rows=rows)

    @app.get("/dev/outbox/{mail_id}")
    def dev_mail(mail_id: int, user=Depends(current), c=Depends(conn)):
        if not allow_private:
            raise HTTPException(status_code=404)
        row = c.execute("SELECT html FROM outbox WHERE id = %s AND user_id = %s",
                        (mail_id, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        return HTMLResponse(row["html"])

    @app.post("/sites/{site_id}/run")
    def run(request: Request, site_id: int, queries: str = Form(""),
            max_pages: int = Form(accounts.MAX_PAGES), csrf: str = Form(""),
            user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        site = own_site(c, user, site_id)
        try:
            job_id = accounts.start_check(c, user, site, queries, max_pages)
        except Refused as exc:
            return site_view(request, c, user, site, status=400, error=str(exc),
                             queries=queries)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/sites/{site_id}/delete")
    def delete_site(request: Request, site_id: int, csrf: str = Form(""),
                    user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        site = own_site(c, user, site_id)
        try:
            accounts.delete_site(c, user, site["id"])
        except Refused as exc:
            return site_view(request, c, user, site, status=400, error=str(exc))
        return RedirectResponse("/sites", status_code=303)

    def own_job(c, user, job_id: int) -> dict:
        job = accounts.get_job(c, user, job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return job

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: int, user=Depends(current), c=Depends(conn)):
        job = own_job(c, user, job_id)
        assessment = diff = None
        cur = billing.current(c, user)
        shown = cur["plan"]["steps_shown"]
        if job["status"] == "done":
            assessment = score.assess(job["result"]["free"])
            prev = accounts.previous_done(c, job)
            if prev:
                diff = history.compare(prev["result"], job["result"])
                diff["prev_id"] = prev["id"]
        progress = _progress(c, job) if job["status"] in ("queued", "running") else None
        return page(request, "cabinet/job.html.j2", user, job=job, a=assessment, diff=diff,
                    steps_shown=shown, plan=cur["plan"], progress=progress,
                    color=score.color_of(assessment.total) if assessment else None)

    @app.get("/jobs/{job_id}/report")
    def job_report(job_id: int, user=Depends(current), c=Depends(conn)):
        job = own_job(c, user, job_id)
        if job["status"] != "done":
            raise HTTPException(status_code=404)
        return HTMLResponse(_report_html(c, user, job))

    def _report_html(c, user, job) -> str:
        free = job["result"]["free"]
        a = score.assess(free)
        shown = billing.current(c, user)["plan"]["steps_shown"]
        if shown is not None:
            a.steps = a.steps[:shown]
        return report.render(free, a, max_pages=job["params"].get("max_pages", 20),
                             back_url=f"/jobs/{job['id']}")

    @app.get("/jobs/{job_id}/report.pdf")
    def job_pdf(job_id: int, user=Depends(current), c=Depends(conn)):
        job = own_job(c, user, job_id)
        if job["status"] != "done":
            raise HTTPException(status_code=404)
        if not billing.current(c, user)["plan"]["pdf"]:
            raise HTTPException(status_code=403, detail="PDF отчёта — в платных тарифах.")
        with tempfile.TemporaryDirectory(prefix="yaseo-pdf-") as tmp:
            src, out = Path(tmp) / "report.html", Path(tmp) / "report.pdf"
            src.write_text(_report_html(c, user, job), encoding="utf-8")
            subprocess.run([str(RENDER), "pdf", str(src), str(out)], capture_output=True,
                           timeout=90)
            if not out.exists() or out.stat().st_size == 0:
                raise HTTPException(status_code=500)
            data = out.read_bytes()
        host = (job["params"]["url"].split("://", 1)[-1].split("/", 1)[0]).replace(":", "_")
        return Response(data, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="yaseo-{host}-{job_id}.pdf"'})

    # --- тариф и оплата -----------------------------------------------------------

    def billing_page(request, c, user, status=200, error=None, **extra):
        cur = billing.current(c, user)
        return page(request, "cabinet/billing.html.j2", user, status=status, error=error,
                    cur=cur, use=billing.usage(c, user, cur), plans=billing.plans(c),
                    offers=billing.offers(c, user), period=billing.PERIOD,
                    payments=billing.payment_history(c, user),
                    fake=isinstance(billing.provider(c), billing.FakeProvider), **extra)

    @app.get("/billing")
    def billing_view(request: Request, user=Depends(current), c=Depends(conn)):
        return billing_page(request, c, user)

    @app.post("/billing/buy")
    def buy(request: Request, choice: str = Form(""), plan: str = Form(""),
            months: int = Form(1), auto_renew: str = Form(""), csrf: str = Form(""),
            user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        if choice:  # кнопка карточки: «тариф:месяцев»
            plan, _, m = choice.partition(":")
            months = int(m) if m.isdigit() else 1
        try:
            url = billing.start_purchase(c, user, plan, billing.provider(c),
                                         str(request.base_url) + "billing/return?payment={payment}",
                                         months=months, auto_renew=auto_renew == "yes")
        except Refused as exc:
            return billing_page(request, c, user, status=400, error=str(exc))
        return RedirectResponse(url, status_code=303)

    @app.post("/billing/promo")
    def apply_promo(request: Request, promo: str = Form(""), csrf: str = Form(""),
                    user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        try:
            got = promos.redeem_in_cabinet(c, user, promo)
        except Refused as exc:
            return billing_page(request, c, user, status=400, error=str(exc), promo=promo)
        user = accounts.session_user(c, request.cookies.get(COOKIE))
        return billing_page(request, c, user, notice=f"Промокод применён: тариф «{got['title']}».")

    @app.get("/billing/return")
    def pay_return(request: Request, payment: int, user=Depends(current), c=Depends(conn)):
        row = c.execute("SELECT * FROM payments WHERE id = %s AND user_id = %s",
                        (payment, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        prov = billing.provider(c)
        if row["provider_id"]:
            row = billing.confirm(c, prov, row["provider_id"]) or row
        return page(request, "cabinet/paid.html.j2", user, payment=row,
                    plan=billing.plan(c, row["plan"]), sub=billing.subscription(c, user["id"]))

    @app.post("/pay/webhook/{name}")
    async def webhook(name: str, request: Request):
        """Уведомление сервиса: берём из тела только номер платежа, статус спрашиваем сами."""
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400)
        pid = str((body.get("object") or {}).get("id") or body.get("id") or "")
        with request.app.state.pool.connection() as c:
            prov = billing.provider(c)
            if prov.name != name or not pid:
                raise HTTPException(status_code=404)
            billing.confirm(c, prov, pid)
        return Response(status_code=200)

    # виртуальная страница оплаты — вместо чужого сервиса, только в режиме fake
    def fake_prov(c) -> billing.FakeProvider:
        prov = billing.provider(c)
        if not isinstance(prov, billing.FakeProvider):
            raise HTTPException(status_code=404)
        return prov

    @app.get("/pay/fake/{pid}")
    def fake_pay_page(request: Request, pid: str, user=Depends(current), c=Depends(conn)):
        row = fake_prov(c).page(pid)
        pay = c.execute("SELECT p.*, pl.title FROM payments p JOIN plans pl ON pl.code = p.plan"
                        " WHERE provider_id = %s AND user_id = %s", (pid, user["id"])).fetchone()
        if row is None or pay is None:
            raise HTTPException(status_code=404)
        return page(request, "cabinet/fakepay.html.j2", user, pid=pid, pay=pay, row=row)

    @app.post("/pay/fake/{pid}")
    def fake_pay_decide(request: Request, pid: str, decision: str = Form(""),
                        csrf: str = Form(""), user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        prov = fake_prov(c)
        row = prov.page(pid)
        mine = c.execute("SELECT 1 FROM payments WHERE provider_id = %s AND user_id = %s",
                         (pid, user["id"])).fetchone()
        if row is None or mine is None:
            raise HTTPException(status_code=404)
        prov.decide(pid, decision in ("pay", "pay-bad-card"),
                    method="decline_card" if decision == "pay-bad-card" else "card_4242")
        return RedirectResponse(row["return_url"], status_code=303)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in (301, 302, 303):
            return Response(status_code=exc.status_code, headers=exc.headers)
        text = {404: "Такой страницы нет.", 403: exc.detail}.get(exc.status_code,
                                                                  "Что-то пошло не так.")
        return page(request, "cabinet/error.html.j2", status=exc.status_code, message=text)

    return app


app = create_app()
