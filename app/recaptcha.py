"""
Проверка Google reCAPTCHA v2 (чекбокс «Я не робот») — используется на
странице восстановления пароля (app/auth.py: forgot_password), чтобы
СМС/письма с кодом восстановления не рассылались по чужим номерам/адресам
автоматическими запросами (СМС через SMS Aero — платные).

Ключи — переменные окружения (см. config.py: RECAPTCHA_SITE_KEY/
RECAPTCHA_SECRET_KEY, там же предупреждение в лог при старте, если не
заданы) — деплой-секрет конкретного домена, не бизнес-данные кооператива,
поэтому не через админку, в отличие от SMS Aero/почты/eWeLink.
"""
import requests
from flask import current_app

VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
REQUEST_TIMEOUT = 10


def is_configured() -> bool:
    return bool(current_app.config.get("RECAPTCHA_SITE_KEY") and current_app.config.get("RECAPTCHA_SECRET_KEY"))


def verify(response_token: str, remote_ip: str | None = None) -> bool:
    """True, если reCAPTCHA не настроена (см. докстринг модуля — дев-фолбэк,
    предупреждение уже выведено при старте приложения) или подтверждена
    Google. При сетевой ошибке к самому Google — тоже False (страница
    восстановления пароля лучше временно не работает, чем полностью без
    защиты в момент сбоя проверки)."""
    if not is_configured():
        return True
    if not response_token:
        return False

    data = {"secret": current_app.config["RECAPTCHA_SECRET_KEY"], "response": response_token}
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        resp = requests.post(VERIFY_URL, data=data, timeout=REQUEST_TIMEOUT)
        return bool(resp.json().get("success"))
    except (requests.RequestException, ValueError):
        return False
