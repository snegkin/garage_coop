"""
Приём уведомлений банка о новых операциях (веб-хуков) — сейчас только
Модульбанк (https://api.modulbank.ru/v1/swagger/docs/v1, раздел
«Уведомления о произошедших транзакциях (веб-хуки)»).

Адрес уведомлений банку через API не передать: председатель вписывает его
в ЛК Модульбанка при генерации токена (Настройки → Услуги → API Банка →
Генерация токена авторизации). Нужный адрес показывается в окне
«Настроить API» счёта. В песочнице банк веб-хуки не шлёт.

**Уведомление — только сигнал «загрузи выписку», а не источник данных.**
Подпись SHA1Hash банк считает лишь от первых 10 символов токена и id
операции (см. modulbank.webhook_signature): сумма, назначение и плательщик
ею не защищены. Поэтому по уведомлению загружается выписка за день
операции обычным путём (bank_sync.sync_account_statement — дедуп по
external_uid, авторазнесение, уведомления, журнал), и данные берутся из
истории операций по токену. Подделать уведомление без токена нельзя, а
повтор подлинного уведомления безвреден — выписка за тот же день уже
загружена.

Ответы банку: 200 — принято (в т.ч. «не наше»/«не исполнена» — повторять
бессмысленно); 403 — подпись не сошлась ни с одним токеном; 503 — сбой
загрузки выписки или операции в истории ещё нет: банк повторит через 10
минут и через час, дальше подхватит утренний cron
(scripts/sync_bank_accounts.py).
"""
import datetime as dt
import hmac

from flask import Blueprint, request, current_app

from . import database
from .bank_api import crypto
from .bank_api.modulbank import EXECUTED_STATUSES, webhook_signature, _normalize_number, _parse_date
from .bank_sync import sync_account_statement
from .models import BankAccount, BankApiCredential, BankApiProvider, BankStatementLine

bp = Blueprint("bank_webhooks", __name__, url_prefix="/bank-webhooks")


def _plain(text: str, status: int):
    return text, status, {"Content-Type": "text/plain; charset=utf-8"}


def _signed_accounts(operation_id: str, signature: str) -> list[BankAccount]:
    """Счета Модульбанка, чей токен даёт эту подпись (обычно ровно один)."""
    accounts = (
        database.db_session.query(BankAccount)
        .join(BankApiCredential, BankApiCredential.bank_account_id == BankAccount.id)
        .filter(BankAccount.api_provider == BankApiProvider.MODULBANK,
                BankApiCredential.api_token_encrypted.isnot(None))
        .order_by(BankAccount.id)
        .all()
    )
    result = []
    for account in accounts:
        token = crypto.decrypt(account.api_credential.api_token_encrypted)
        if token and hmac.compare_digest(webhook_signature(token, operation_id), signature.lower()):
            result.append(account)
    return result


@bp.route("/modulbank", methods=["POST"])
def modulbank():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("operation"), dict):
        return _plain("bad request", 400)
    operation = payload["operation"]
    operation_id = str(operation.get("id") or "")
    signature = str(payload.get("SHA1Hash") or "")
    if not operation_id or not signature:
        return _plain("bad request", 400)

    signed = _signed_accounts(operation_id, signature)
    if not signed:
        current_app.logger.warning("Веб-хук Модульбанка с неверной подписью, операция %s, IP %s",
                                   operation_id, request.remote_addr)
        return _plain("forbidden", 403)

    now = dt.datetime.utcnow()
    for account in signed:
        account.api_credential.last_webhook_at = now

    # Один токен видит все счета компании, а в справочнике может быть
    # только часть из них — выбираем счёт по номеру из уведомления.
    number = _normalize_number(operation.get("bankAccountNumber"))
    account = next(
        (a for a in signed
         if number and number in {_normalize_number(a.api_credential.account_number),
                                  _normalize_number(a.checking_account)}),
        None,
    )
    if account is None:
        database.db_session.commit()
        return _plain("ignored: account not in directory", 200)

    # Черновики, подпись, отказы, отмены в выписку не попадают
    # (modulbank.EXECUTED_STATUSES) — грузить нечего.
    if operation.get("status") not in EXECUTED_STATUSES:
        database.db_session.commit()
        return _plain("ignored: not executed", 200)

    day = _parse_date(operation.get("executed")) or _parse_date(operation.get("created")) or dt.date.today()
    database.db_session.commit()
    status, message, _stats = sync_account_statement(account, day, day)
    if status != "success":
        current_app.logger.warning("Веб-хук Модульбанка: выписка счёта %s за %s не загружена: %s",
                                   account.id, day, message)
        return _plain("statement sync failed", 503)

    loaded = database.db_session.query(BankStatementLine.id).filter(
        BankStatementLine.bank_account_id == account.id,
        BankStatementLine.external_uid == operation_id,
    ).first()
    if loaded is None:
        # Уведомление пришло раньше, чем операция появилась в истории, —
        # пусть банк повторит.
        return _plain("operation not yet in history", 503)
    return _plain("ok", 200)
