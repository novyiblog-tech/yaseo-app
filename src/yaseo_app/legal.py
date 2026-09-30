"""Реквизиты исполнителя и редакция оферты.

Реквизиты — из окружения сервера: ИП, на которое идут деньги, ещё не выбрано
(Сергей, 29.09.2026). Пока реквизитов нет, оферта показывается черновиком, а настоящий
платёжный сервис не включается (billing.provider).
"""
from __future__ import annotations

import os

# Вторая редакция 30.09.2026: сроки 1 и 3 месяца, без автосписания по умолчанию, промокод.
# Третья 01.10.2026: разовый аудит на 3 месяца, его остаток засчитывается при переходе выше.
OFFER_VERSION = "01.10.2026"

FIELDS = {
    "operator": ("YASEO_OPERATOR", "наименование ИП"),
    "inn": ("YASEO_OPERATOR_INN", "ИНН"),
    "ogrnip": ("YASEO_OPERATOR_OGRNIP", "ОГРНИП"),
    "address": ("YASEO_OPERATOR_ADDRESS", "адрес для корреспонденции"),
    "email": ("YASEO_SUPPORT_EMAIL", "почта для обращений"),
    "site": ("YASEO_SITE_DOMAIN", "адрес сайта сервиса"),
}
# Домен куплен 30.09.2026 на рег.ру; окружение может его переопределить.
DEFAULTS = {"site": "yaseo.site"}


def requisites() -> dict:
    out, missing = {}, []
    for key, (env, label) in FIELDS.items():
        value = (os.environ.get(env) or DEFAULTS.get(key, "")).strip()
        if not value:
            missing.append(label)
            value = f"[{label}]"
        out[key] = value
    # Например: «НДС не облагается (УСН)». Зависит от системы налогообложения ИП.
    out["vat_note"] = (os.environ.get("YASEO_VAT_NOTE") or "").strip()
    if not out["vat_note"]:
        missing.append("НДС и система налогообложения")
    out["missing"] = missing
    return out


def complete() -> bool:
    return not requisites()["missing"]
