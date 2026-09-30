"""Кабинет: страницы на сервере, тёмная тема. Логика — в accounts.py.

    uv run uvicorn yaseo_app.web:app --reload

YASEO_ALLOW_PRIVATE=1 — пускать локальные адреса (тестовый сайт), только для разработки.
YASEO_INSECURE_COOKIES=1 — cookie без Secure, для http://localhost.
"""
from __future__ import annotations

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

from yaseo_app import accounts, billing, db, history, report, score
from yaseo_app.accounts import Refused

HERE = Path(__file__).parent
COOKIE = "yaseo_session"
RENDER = HERE.parent.parent / "scripts" / "chrome-render.sh"
SECTION_TITLES = {"demand": "Спрос в Wordstat", "positions": "Позиции в Яндексе",
                  "answers": "Ответы нейросети Яндекса"}
STATUS_TITLES = {"queued": "в очереди", "running": "идёт", "done": "готово",
                 "failed": "не удалась"}
SECTION_STATUS = {"ok": "собран", "partial": "собран частично", "skipped": "пропущен",
                  "failed": "не собран", "off": "выключен"}

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(str(v)):.2f}".replace(".", ",") + " ₽"


def _templates() -> Environment:
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    env.filters["money"] = _money
    env.filters["rub"] = billing.money
    env.filters["num"] = lambda v: f"{v:,}".replace(",", "\u00a0") if isinstance(v, int) else v
    env.filters["dt"] = lambda d: d.astimezone().strftime("%d.%m.%Y %H:%M") if d else "—"
    env.globals["color_of"] = score.color_of
    env.globals.update(STATUS_TITLES=STATUS_TITLES, SECTION_TITLES=SECTION_TITLES,
                       SECTION_STATUS=SECTION_STATUS)
    return env


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
        resp.headers["Cache-Control"] = "no-store"
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
        return RedirectResponse("/sites" if user else "/login", status_code=303)

    @app.get("/signup")
    def signup_form(request: Request):
        return page(request, "cabinet/signup.html.j2")

    @app.post("/signup")
    def signup(request: Request, email: str = Form(""), password: str = Form(""),
               consent: str = Form(""), c=Depends(conn)):
        try:
            user = accounts.signup(c, email, password, consent == "yes")
        except Refused as exc:
            return page(request, "cabinet/signup.html.j2", error=str(exc), email=email,
                        status=400)
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
        site = own_site(c, user, site_id)
        return page(request, "cabinet/site.html.j2", user, site=site,
                    jobs=accounts.site_jobs(c, site_id))

    @app.post("/sites/{site_id}/run")
    def run(request: Request, site_id: int, queries: str = Form(""),
            max_pages: int = Form(20), csrf: str = Form(""),
            user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        site = own_site(c, user, site_id)
        try:
            job_id = accounts.start_check(c, user, site, queries, max_pages)
        except Refused as exc:
            return page(request, "cabinet/site.html.j2", user, site=site, status=400,
                        error=str(exc), queries=queries,
                        jobs=accounts.site_jobs(c, site_id))
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/sites/{site_id}/delete")
    def delete_site(request: Request, site_id: int, csrf: str = Form(""),
                    user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        site = own_site(c, user, site_id)
        try:
            accounts.delete_site(c, user, site["id"])
        except Refused as exc:
            return page(request, "cabinet/site.html.j2", user, site=site, status=400,
                        error=str(exc), jobs=accounts.site_jobs(c, site_id))
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
        return page(request, "cabinet/job.html.j2", user, job=job, a=assessment, diff=diff,
                    steps_shown=shown, plan=cur["plan"],
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
        return report.render(free, a, max_pages=job["params"].get("max_pages", 20))

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

    def billing_page(request, c, user, status=200, error=None):
        cur = billing.current(c, user)
        return page(request, "cabinet/billing.html.j2", user, status=status, error=error,
                    cur=cur, use=billing.usage(c, user, cur), plans=billing.plans(c),
                    payments=billing.payment_history(c, user),
                    fake=isinstance(billing.provider(c), billing.FakeProvider))

    @app.get("/billing")
    def billing_view(request: Request, user=Depends(current), c=Depends(conn)):
        return billing_page(request, c, user)

    @app.post("/billing/buy")
    def buy(request: Request, plan: str = Form(""), csrf: str = Form(""),
            user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        try:
            url = billing.start_purchase(c, user, plan, billing.provider(c),
                                         str(request.base_url) + "billing/return?payment={payment}")
        except Refused as exc:
            return billing_page(request, c, user, status=400, error=str(exc))
        return RedirectResponse(url, status_code=303)

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
                    plan=billing.plan(c, row["plan"]))

    @app.post("/billing/auto-renew")
    def auto_renew(request: Request, on: str = Form(""), csrf: str = Form(""),
                   user=Depends(current), c=Depends(conn)):
        check_csrf(user, csrf)
        billing.set_auto_renew(c, user, on == "yes")
        return RedirectResponse("/billing", status_code=303)

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
