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

from yaseo_app import accounts, db, report, score
from yaseo_app.accounts import Refused

HERE = Path(__file__).parent
COOKIE = "yaseo_session"
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
    env.filters["num"] = lambda v: f"{v:,}".replace(",", "\u00a0") if isinstance(v, int) else v
    env.filters["dt"] = lambda d: d.astimezone().strftime("%d.%m.%Y %H:%M") if d else "—"
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

    def own_job(c, user, job_id: int) -> dict:
        job = accounts.get_job(c, user, job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return job

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: int, user=Depends(current), c=Depends(conn)):
        job = own_job(c, user, job_id)
        assessment = None
        if job["status"] == "done":
            assessment = score.assess(job["result"]["free"])
        return page(request, "cabinet/job.html.j2", user, job=job, a=assessment,
                    color=score.color_of(assessment.total) if assessment else None)

    @app.get("/jobs/{job_id}/report")
    def job_report(job_id: int, user=Depends(current), c=Depends(conn)):
        job = own_job(c, user, job_id)
        if job["status"] != "done":
            raise HTTPException(status_code=404)
        free = job["result"]["free"]
        return HTMLResponse(report.render(free, max_pages=job["params"].get("max_pages", 20)))

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in (301, 302, 303):
            return Response(status_code=exc.status_code, headers=exc.headers)
        text = {404: "Такой страницы нет.", 403: exc.detail}.get(exc.status_code,
                                                                  "Что-то пошло не так.")
        return page(request, "cabinet/error.html.j2", status=exc.status_code, message=text)

    return app


app = create_app()
