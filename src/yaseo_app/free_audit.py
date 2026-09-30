"""Бесплатная часть аудита: обход сайта и готовность к ИИ-поиску.

Платных вызовов здесь нет: движок ходит только на сам сайт. Каждый аудит идёт отдельным
процессом со своим каталогом — у движка база, ключи и настройки общие на процесс,
и два аудита разных пользователей в одном процессе делить их не должны.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 2
MAX_PAGES_LIMIT = 200
JOB_TIMEOUT = 900

# Ключи поставщиков в процесс бесплатного аудита не попадают вовсе.
SECRET_ENV = (
    "YC_FOLDER_ID",
    "YANDEX_AI_STUDIO_API_KEY",
    "YANDEX_OAUTH_CLIENT_ID",
    "YANDEX_OAUTH_CLIENT_SECRET",
    "YANDEX_OAUTH_TOKEN",
    "YANDEX_OAUTH_REFRESH",
    "PERPLEXITY_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "YASEO_YANDEX_CLIENT_ID",
    "YASEO_YANDEX_CLIENT_SECRET",
)
# Бережный обход: пауза между страницами вместо обычных 0,3 с — для слабого хостинга.
GENTLE_DELAY = 2.0


class AuditFailed(Exception):
    pass


def excluded(url: str, patterns: list[str]) -> bool:
    """Попадает ли адрес под исключения сайта. Без звёздочки — начало пути (/cart
    исключает /cart и /cart/1), со звёздочкой — шаблон на путь с параметрами."""
    from fnmatch import fnmatchcase
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    for p in patterns or ():
        if "*" in p:
            if fnmatchcase(path, p) or fnmatchcase(path, p.rstrip("*") + "*"):
                return True
        elif path == p or path.startswith(p.rstrip("/") + "/") or path.startswith(p + "?") \
                or (p.endswith("/") and path.startswith(p)):
            return True
    return False


def collect(url: str, max_pages: int = 20, allow_private: bool = False,
            use_proxy: bool = False, exclude: list[str] | None = None,
            gentle: bool = False) -> dict:
    """Запускает движок в текущем процессе. Снаружи звать run_isolated()."""
    from importlib.metadata import version

    from yaseo import audit, net
    from yaseo.geo import readiness

    from yaseo_app import site_checks

    max_pages = max(1, min(int(max_pages), MAX_PAGES_LIMIT))
    private = True if allow_private else None
    net.check_url(url, allow_private=private)

    if exclude:
        # Движок не знает про исключения: фильтр ставим на его проверку «обходить ли адрес».
        # Процесс отдельный на каждый аудит, подмена не протекает в чужие проверки.
        crawlable = audit._crawlable
        audit._crawlable = lambda u: crawlable(u) and not excluded(u, exclude)
    site = audit.audit_site(url, max_pages=max_pages, use_proxy=use_proxy,
                            allow_private=private,
                            **({"delay": GENTLE_DELAY} if gentle else {}))
    geo = readiness.check(url, allow_private=private)
    audit_dict = dataclasses.asdict(site)
    checks = site_checks.run(url, allow_private=allow_private, use_proxy=use_proxy)
    audit_dict["issues"] = audit_dict.get("issues", []) + checks.pop("issues")
    return {
        "schema": SCHEMA,
        "url": url,
        "engine": version("yaseo"),
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paid_calls": 0,
        "exclude": list(exclude or []),
        "gentle": gentle,
        "audit": audit_dict,
        "checks": checks,
        "geo": dataclasses.asdict(geo),
    }


def isolated_env(workdir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["YASEO_DB"] = str(workdir / "yaseo.db")
    env["YASEO_ENV_FILE"] = str(workdir / "empty.env")
    env["XDG_DATA_HOME"] = str(workdir / "data")
    env["XDG_CONFIG_HOME"] = str(workdir / "config")
    env.pop("YASEO_DOMAIN", None)
    return env


def run_isolated(url: str, max_pages: int = 20, allow_private: bool = False,
                 use_proxy: bool = False, timeout: int = JOB_TIMEOUT,
                 exclude: list[str] | None = None, gentle: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="yaseo-job-") as tmp:
        workdir = Path(tmp)
        (workdir / "empty.env").write_text("", encoding="utf-8")
        out = workdir / "result.json"
        cmd = [sys.executable, "-m", "yaseo_app.free_audit", url,
               "--max-pages", str(max_pages), "--out", str(out)]
        if allow_private:
            cmd.append("--allow-private")
        if use_proxy:
            cmd.append("--use-proxy")
        if exclude:
            cmd += ["--exclude", json.dumps(list(exclude), ensure_ascii=False)]
        if gentle:
            cmd.append("--gentle")
        try:
            done = subprocess.run(cmd, env=isolated_env(workdir), cwd=workdir,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise AuditFailed(f"аудит не уложился в {timeout} с") from exc
        if done.returncode != 0 or not out.exists():
            tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
            raise AuditFailed("; ".join(tail) or f"код выхода {done.returncode}")
        return json.loads(out.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Бесплатная часть аудита сайта")
    parser.add_argument("url")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-private", action="store_true")
    parser.add_argument("--use-proxy", action="store_true")
    parser.add_argument("--exclude", default="[]", help="JSON-список исключённых путей")
    parser.add_argument("--gentle", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = collect(args.url, args.max_pages, args.allow_private, args.use_proxy,
                         exclude=json.loads(args.exclude), gentle=args.gentle)
    except Exception as exc:  # процесс-исполнитель: причина уходит родителю одной строкой
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
