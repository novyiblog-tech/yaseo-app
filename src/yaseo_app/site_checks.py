"""Проверки, которых нет в движке: видит ли робот настоящий сайт и порядок с HTTPS.

Идут в процессе бесплатного аудита, по тем же правилам доступа (внутренние адреса —
только по разрешению). Находки в формате движка: код, важность, адрес, доказательство,
что сделать. Их кладут в общий список находок, и оценка, шаги, отчёт и сравнение
проверок подхватывают их без отдельной логики.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_BODY = 2_000_000
TIMEOUT = 15

# Страницы защиты от ботов. Метка — строка, которую защита вставляет в свою заглушку.
STUB_MARKERS = (
    ("Cloudflare", ("cf-browser-verification", "cf_chl_opt", "challenge-platform",
                    "just a moment...", "attention required! | cloudflare")),
    ("DDoS-Guard", ("ddos-guard", "__ddg1", "check.ddos-guard.net")),
    ("Qrator", ("qrator", "__qrator")),
    ("Servicepipe", ("servicepipe", "spsc_")),
    ("Variti", ("variti",)),
    ("Яндекс SmartCaptcha", ("smartcaptcha", "showcaptcha")),
    ("капча", ("g-recaptcha", "h-captcha", "hcaptcha.com")),
    ("проверка браузера", ("checking your browser", "проверка браузера",
                           "please enable javascript and cookies", "вы не робот",
                           "подтвердите, что вы не робот", "access denied")),
)
STUB_STATUSES = {403, 429, 503}
THIN_WORDS = 40
# Браузеру отдают во столько раз больше текста, чем роботу, — робот видит не то.
DIFF_RATIO = 3.0

TLS_WARN_DAYS = 14
TLS_NOTICE_DAYS = 30


def _issue(severity: str, code: str, url: str, evidence: str, fix: str) -> dict:
    return {"severity": severity, "code": code, "url": url, "evidence": evidence,
            "fix": fix, "source": "yaseo_app"}


# --- страница, которую видит робот --------------------------------------------------

def _text_words(html: str) -> int:
    html = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return len(re.findall(r"\w{2,}", text))


def fetch(url: str, ua: str, allow_private: bool, use_proxy: bool) -> dict:
    """Одна страница с заданным User-Agent: итоговый адрес, код, текст."""
    from yaseo import net
    import urllib.request
    opener = net.site_opener(proxy=use_proxy, follow=True,
                             allow_private=True if allow_private else None)
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "text/html,*/*;q=0.8",
                                               "Accept-Language": "ru,en;q=0.8"})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            status, final, body = r.status, r.geturl(), r.read(MAX_BODY)
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        status, final, body = e.code, e.geturl() or url, e.read(MAX_BODY) if e.fp else b""
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"url": url, "status": None, "final": None, "html": "", "words": 0,
                "error": str(getattr(e, "reason", e))}
    m = re.search(r"charset=([\w-]+)", ctype)
    try:
        html = body.decode(m[1] if m else "utf-8", errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    return {"url": url, "status": status, "final": final, "html": html,
            "words": _text_words(html), "error": None}


def stub_marker(html: str) -> tuple[str, str] | None:
    low = html.lower()
    for vendor, marks in STUB_MARKERS:
        for m in marks:
            if m in low:
                return vendor, m
    return None


def classify_access(bot: dict, browser: dict) -> dict:
    """Вердикт: real — настоящая страница, stub — заглушка защиты, empty — текста нет
    (сайт рисуется скриптом), differs — браузеру отдают намного больше, чем роботу,
    down — не открылся вовсе."""
    if bot["status"] is None:
        return {"verdict": "down", "evidence": bot["error"] or "нет ответа"}
    mark = stub_marker(bot["html"])
    if mark and (bot["status"] in STUB_STATUSES or bot["words"] < 150):
        return {"verdict": "stub", "vendor": mark[0],
                "evidence": f"код {bot['status']}, в странице «{mark[1]}» ({mark[0]})"}
    if bot["status"] in STUB_STATUSES:
        return {"verdict": "stub", "vendor": None,
                "evidence": f"роботу ответили кодом {bot['status']}"
                            + (f", браузеру — {browser['status']}" if browser["status"] else "")}
    if browser["words"] >= THIN_WORDS and browser["words"] > DIFF_RATIO * max(bot["words"], 1):
        return {"verdict": "differs",
                "evidence": f"роботу — {bot['words']} слов, браузеру — {browser['words']}"}
    scripts = len(re.findall(r"(?i)<script\b", bot["html"]))
    if bot["words"] < THIN_WORDS and scripts:
        # Мало текста и есть скрипты — страницу дорисовывает браузер. Просто короткая
        # страница без скриптов — не сюда, её ловит движок как «мало текста».
        return {"verdict": "empty",
                "evidence": f"в разметке {bot['words']} слов текста и {scripts} скриптов"}
    return {"verdict": "real", "evidence": f"роботу — {bot['words']} слов, "
                                           f"браузеру — {browser['words']}"}


def access_check(url: str, allow_private: bool, use_proxy: bool) -> tuple[dict, list[dict]]:
    from yaseo import net
    bot = fetch(url, net.user_agent("audit"), allow_private, use_proxy)
    browser = fetch(url, BROWSER_UA, allow_private, use_proxy)
    res = classify_access(bot, browser)
    res.update(bot_status=bot["status"], browser_status=browser["status"],
               bot_words=bot["words"], browser_words=browser["words"])
    issues = []
    if res["verdict"] == "stub":
        issues.append(_issue(
            "critical", "bot-stub", url, res["evidence"],
            "Разрешить поисковым роботам доступ мимо защиты от ботов: в настройках защиты "
            "добавить Яндекс и Google в белый список."))
    elif res["verdict"] == "differs":
        issues.append(_issue(
            "major", "bot-content-differs", url, res["evidence"],
            "Отдавать роботу ту же страницу, что и человеку: проверить правила защиты "
            "и серверную отрисовку."))
    elif res["verdict"] == "empty":
        issues.append(_issue(
            "major", "js-only-page", url, res["evidence"],
            "Отдавать текст страницы сразу в HTML: включить серверную отрисовку или "
            "предварительную сборку страниц."))
    return res, issues


# --- HTTPS ------------------------------------------------------------------------

def tls_info(host: str, port: int = 443) -> dict:
    """Сертификат с проверкой цепочки и имени. Ошибка проверки — в поле error."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "error": e.verify_message or str(e)}
    except (ssl.SSLError, OSError) as e:
        return {"ok": False, "error": str(e), "no_tls": True}
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]),
                                       tz=timezone.utc)
    issuer = dict(x[0] for x in cert.get("issuer", ()))
    return {"ok": True, "not_after": not_after.isoformat(timespec="seconds"),
            "issuer": issuer.get("organizationName") or issuer.get("commonName", "")}


def classify_tls(info: dict, now: datetime, url: str) -> list[dict]:
    if not info.get("ok"):
        if info.get("no_tls"):
            return [_issue("critical", "no-https", url, f"https не открывается: {info['error']}",
                           "Выпустить сертификат (бесплатно — Let's Encrypt или у хостера) "
                           "и включить HTTPS.")]
        return [_issue("critical", "tls-invalid", url, f"сертификат не прошёл проверку: "
                                                       f"{info['error']}",
                       "Перевыпустить сертификат на этот домен у доверенного центра.")]
    days = (datetime.fromisoformat(info["not_after"]) - now).days
    if days < 0:
        return [_issue("critical", "tls-invalid", url, f"сертификат истёк {info['not_after'][:10]}",
                       "Продлить сертификат.")]
    if days <= TLS_WARN_DAYS:
        return [_issue("major", "tls-expiring", url,
                       f"сертификат истекает {info['not_after'][:10]}, через {days} дн.",
                       "Продлить сертификат и включить автопродление.")]
    if days <= TLS_NOTICE_DAYS:
        return [_issue("minor", "tls-expiring", url,
                       f"сертификат истекает {info['not_after'][:10]}, через {days} дн.",
                       "Проверить, что автопродление сертификата включено.")]
    return []


def _first_hop(url: str, allow_private: bool, use_proxy: bool) -> tuple[int | None, str | None]:
    """Код и адрес первого перенаправления, без перехода по нему."""
    from yaseo import net
    import urllib.request
    opener = net.site_opener(proxy=use_proxy, follow=False,
                             allow_private=True if allow_private else None)
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": net.user_agent("audit")})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location") if e.headers else None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    return f"{p.scheme}://{host}" + (f":{p.port}" if p.port else "")


def mirrors_check(url: str, allow_private: bool, use_proxy: bool) -> tuple[dict, list[dict]]:
    """Адреса сайта с www и без, по http и https должны сходиться в один."""
    from yaseo import net
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    bare = host.removeprefix("www.")
    hosts = [host] if _is_ip(host) else [bare, "www." + bare]
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    variants = [f"{scheme}://{h}{port}/" for h in hosts for scheme in ("http", "https")]
    ua = net.user_agent("audit")
    seen = {}
    for v in variants:
        r = fetch(v, ua, allow_private, use_proxy)
        code, loc = _first_hop(v, allow_private, use_proxy)
        seen[v] = {"final": _origin(r["final"]) if r["status"] and r["status"] < 400 else None,
                   "status": r["status"], "first_code": code, "first_location": loc}

    live = {v: s for v, s in seen.items() if s["final"]}
    finals = sorted({s["final"] for s in live.values()})
    issues = []
    https_live = any(s["final"].startswith("https://") for s in live.values())
    if len(finals) > 1:
        issues.append(_issue(
            "major", "site-mirrors", url, "открываются как разные сайты: " + ", ".join(finals),
            "Выбрать один главный адрес и настроить постоянное (301) перенаправление "
            "на него со всех остальных."))
    http_left = [v for v, s in live.items() if v.startswith("http://")
                 and s["final"].startswith("http://")]
    if https_live and http_left:
        issues.append(_issue(
            "major", "http-not-redirected", http_left[0],
            f"{http_left[0]} открывается без перехода на https",
            "Настроить постоянное (301) перенаправление с http на https."))
    # Временный переход считается, только если он ведёт на другой адрес сайта
    # (www, схема). Переход внутри сайта, например / → /ru/, — не про зеркала.
    temp = [(v, s) for v, s in live.items() if s["first_code"] in (302, 303, 307)
            and s["first_location"]
            and _origin(urljoin(v, s["first_location"])) != _origin(v)]
    if temp and len(finals) == 1:
        v, s = temp[0]
        issues.append(_issue(
            "minor", "redirect-temporary", v,
            f"{v} → {s['first_location']} с кодом {s['first_code']}",
            "Сделать перенаправление постоянным: код 301 вместо временного."))
    return {"variants": seen, "main": finals[0] if len(finals) == 1 else None}, issues


def run(url: str, allow_private: bool = False, use_proxy: bool = False,
        now: datetime | None = None) -> dict:
    from yaseo import net
    net.check_url(url, allow_private=True if allow_private else None)
    now = now or datetime.now(timezone.utc)
    access, issues = access_check(url, allow_private, use_proxy)
    https: dict = {}
    if access["verdict"] != "down":
        mirrors, more = mirrors_check(url, allow_private, use_proxy)
        issues += more
        host = urlsplit(url).hostname or ""
        info = tls_info(host)
        https = {"tls": info, "mirrors": mirrors}
        issues += classify_tls(info, now, urlunsplit(("https", host, "/", "", "")))
    return {"access": access, "https": https, "issues": issues}
