"""
Приём уведомлений Модульбанка о новых операциях (app/bank_webhooks.py).
Банк фейковый (fake_bank из test_modulbank): по уведомлению выписка
загружается из «истории операций», а не из тела уведомления.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.bank_api.modulbank import webhook_signature
from app.models import BankAccount, BankStatementLine

from tests.test_modulbank import ACC_NUMBER, make_account, op, fake_bank  # noqa: F401 (фикстура)

TOKEN = "NDIWJFNASDJKFHNASDJFASDJKFHASDJKFHASDJFHASDK"
URL = "/bank-webhooks/modulbank"


def notification(operation, token=TOKEN, **kw):
    body = {
        "companyInn": "1111111111", "companyKpp": "",
        "operation": {"bankAccountNumber": ACC_NUMBER, **operation},
        "SHA1Hash": webhook_signature(token, operation["id"]),
    }
    body.update(kw)
    return body


def test_signature_matches_bank_documentation_example():
    # Пример из спецификации банка, раздел «Уведомления о произошедших транзакциях».
    assert webhook_signature(TOKEN, "a4b825ca-a6f8-4996-a1db-a5f3028bb68d") == "27063efdefd944907e08cfa242170f4d1a260c36"


def test_webhook_loads_operation_from_history_not_from_body(app, db, client, fake_bank):
    account = make_account(db, token=TOKEN)
    db.commit()
    fake_bank["operations"] = [op(id="op-1", amount=500.0)]
    # В уведомлении сумма другая — верим истории операций, а не телу.
    resp = client.post(URL, json=notification(op(id="op-1", amount=999999.0)))
    assert resp.status_code == 200

    db.expire_all()
    lines = database.db_session.query(BankStatementLine).filter_by(bank_account_id=account.id).all()
    assert [(line.external_uid, line.amount) for line in lines] == [("op-1", Decimal("500.00"))]
    history_call = next(c for c in fake_bank["calls"] if "/operation-history/" in c["url"])
    assert history_call["json"]["from"] == "2026-11-05"  # день операции из уведомления
    cred = database.db_session.get(BankAccount, account.id).api_credential
    assert cred.last_webhook_at is not None

    # Повтор того же уведомления (банк ретраит) — без дубля.
    assert client.post(URL, json=notification(op(id="op-1"))).status_code == 200
    assert database.db_session.query(BankStatementLine).filter_by(bank_account_id=account.id).count() == 1


def test_webhook_with_wrong_signature_rejected(app, db, client, fake_bank):
    make_account(db, token=TOKEN)
    db.commit()
    fake_bank["operations"] = [op(id="op-1")]
    resp = client.post(URL, json=notification(op(id="op-1"), token="ЧУЖОЙ-ТОКЕН-1234567"))
    assert resp.status_code == 403
    assert fake_bank["calls"] == []
    assert database.db_session.query(BankStatementLine).count() == 0


def test_webhook_works_with_csrf_enabled(app, db, client, fake_bank):
    app.config["WTF_CSRF_ENABLED"] = True
    make_account(db, token=TOKEN)
    db.commit()
    fake_bank["operations"] = [op(id="op-1")]
    assert client.post(URL, json=notification(op(id="op-1"))).status_code == 200


def test_webhook_for_not_executed_operation_ignored(app, db, client, fake_bank):
    make_account(db, token=TOKEN)
    db.commit()
    resp = client.post(URL, json=notification(op(id="op-2", status="Draft", category="Credit")))
    assert resp.status_code == 200
    assert fake_bank["calls"] == []


def test_webhook_for_account_not_in_directory_ignored(app, db, client, fake_bank):
    make_account(db, token=TOKEN)
    db.commit()
    body = notification(op(id="op-3"))
    body["operation"]["bankAccountNumber"] = "40702810000000000777"  # другой счёт той же компании
    resp = client.post(URL, json=body)
    assert resp.status_code == 200
    assert fake_bank["calls"] == []


def test_webhook_before_operation_in_history_asks_bank_to_retry(app, db, client, fake_bank):
    make_account(db, token=TOKEN)
    db.commit()
    fake_bank["operations"] = []
    assert client.post(URL, json=notification(op(id="op-4"))).status_code == 503


def test_webhook_bank_error_asks_bank_to_retry(app, db, client, fake_bank):
    make_account(db, token=TOKEN)
    db.commit()
    fake_bank["status"] = 503
    assert client.post(URL, json=notification(op(id="op-5"))).status_code == 503


def test_webhook_malformed_body(app, db, client):
    assert client.post(URL, data="не json", content_type="application/json").status_code == 400
    assert client.post(URL, json={"operation": {"id": "x"}}).status_code == 400


def test_settings_modal_shows_webhook_url(app, db, client):
    from tests.conftest import make_user, login
    from app.models import RoleEnum
    make_account(db, token=TOKEN)
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")
    html = client.get("/finance/bank-accounts").get_data(as_text=True)
    assert "/bank-webhooks/modulbank" in html
