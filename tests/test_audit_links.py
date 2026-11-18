"""
Ссылки из журнала (audit.link_for): на карточку сущности, а у записей без
неё — на раздел, к которому относится действие.
"""
import datetime as dt

from app import audit
from app.models import BankAccount, Document, DocumentType, RoleEnum

from tests.conftest import login, make_person, make_user


def test_section_fallback_for_settings(app):
    with app.test_request_context():
        assert audit.link_for("mailbox.settings_save", None, None) == "/mailbox/"
        assert audit.link_for("cooperative.settings_edit", None, None) == "/cooperative/"
        assert audit.link_for("power.tariff_add", None, None) == "/power/"
        assert audit.link_for("unknown.thing", None, None) is None


def test_bank_account_links_to_statement(app, db):
    account = BankAccount(bank_name="Банк", checking_account="40703", bik="0", correspondent_account="301")
    db.add(account)
    db.commit()
    with app.test_request_context():
        assert audit.link_for("bank_api.statement_sync", "bank_account", account.id).endswith(f"/{account.id}/statement")
        assert audit.link_for("bank_api.balance_sync", "bank_account", account.id).endswith(f"/{account.id}/statement")
        assert audit.link_for("bank_api.payment_registry_upload", "bank_account", account.id).endswith("/registry/payments")


def test_document_and_user_links(app, db):
    person = make_person(db, "Документов Д.")
    user = make_user(db, "docs", "pw", person=person)
    doc = Document(doc_type=DocumentType.OTHER, date=dt.date(2026, 11, 1), title="Выписка из ЕГРН")
    db.add(doc)
    db.commit()
    with app.test_request_context():
        assert audit.link_for("document.edit", "document", doc.id).startswith("/cooperative/")
        assert audit.link_for("auth.login", "user", user.id) == f"/persons/{person.id}"


def test_audit_log_page_shows_link_for_settings_entry(client, db):
    make_user(db, "chair", "pw", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pw")
    with client.application.test_request_context():
        audit.record("mailbox.settings_save", "Настройки почты правления обновлены")
    db.commit()
    html = client.get("/governance/audit-log").get_data(as_text=True)
    assert 'href="/mailbox/"' in html
