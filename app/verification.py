"""
Одноразовые коды подтверждения — общая механика для двух сценариев (см.
models.VerificationCode/VerificationCodePurpose):
- подтверждение номера телефона при самостоятельной регистрации
  (auth.py: login_by_phone/register_phone_confirm);
- восстановление пароля по email или телефону (auth.py: forgot_password/
  reset_password).

Код хранится хэшем (werkzeug.security — тот же приём, что и пароль
пользователя), не в открытом виде — сырой код существует только в момент
отправки (СМС/письмо) и в форме, которую вводит человек.
"""
import datetime as dt
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from . import database
from .models import VerificationCode, VerificationCodePurpose

CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5  # после этого числа попыток код невалиден в любом случае — защита от перебора


def generate_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(6))


def issue_code(purpose: VerificationCodePurpose, target: str, payload: str | None = None) -> str:
    """Удаляет непогашенные коды на тот же (purpose, target) — не копятся,
    предыдущий код перестаёт быть действительным при повторном запросе.
    Возвращает СЫРОЙ код (для отправки), в БД остаётся только хэш.

    payload — непрозрачная строка, которую нужно будет получить обратно
    при успешном consume_code (сейчас — хэш пароля для PHONE_REGISTER, см.
    docstring VerificationCode.payload в models.py), без повторной
    передачи через браузер между запросом и подтверждением кода."""
    database.db_session.query(VerificationCode).filter(
        VerificationCode.purpose == purpose,
        VerificationCode.target == target,
        VerificationCode.consumed_at.is_(None),
    ).delete(synchronize_session=False)

    code = generate_code()
    now = dt.datetime.utcnow()
    database.db_session.add(VerificationCode(
        purpose=purpose,
        target=target,
        code_hash=generate_password_hash(code),
        expires_at=now + dt.timedelta(minutes=CODE_TTL_MINUTES),
        created_at=now,
        payload=payload,
    ))
    database.db_session.flush()
    return code


def consume_code(purpose: VerificationCodePurpose, target: str, submitted: str) -> tuple[bool, str | None]:
    """Ищет непогашенный неистёкший код на (purpose, target) — их может
    быть максимум один благодаря issue_code, но на всякий случай берём
    самый свежий. При совпадении помечает consumed_at и возвращает
    (True, payload); при несовпадении — (False, None) и увеличивает
    attempts (после MAX_ATTEMPTS код невалиден и при правильном коде
    тоже — не даём подбирать бесконечно)."""
    row = (
        database.db_session.query(VerificationCode)
        .filter(
            VerificationCode.purpose == purpose,
            VerificationCode.target == target,
            VerificationCode.consumed_at.is_(None),
        )
        .order_by(VerificationCode.id.desc())
        .first()
    )
    if row is None:
        return False, None
    if row.expires_at < dt.datetime.utcnow():
        return False, None
    if row.attempts >= MAX_ATTEMPTS:
        return False, None

    if not check_password_hash(row.code_hash, submitted):
        row.attempts += 1
        return False, None

    row.consumed_at = dt.datetime.utcnow()
    return True, row.payload
