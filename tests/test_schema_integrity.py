"""
Схема и размеры полей:
- схема после всех миграций совпадает с моделями (то же, что `alembic
  check`): расхождение вроде api_provider VARCHAR(7) против значения в 19
  символов больше не проходит молча;
- строка длиннее String(n) не записывается в базу молча (SQLite длину не
  проверяет) — в тестах это ошибка, см. app/database.py;
- реквизиты из форм сохраняются без пробелов.
"""
import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app import database
from app.accounting import compact_requisite, tidy_account_number
from app.models import Base, Counterparty, Cooperative, RoleEnum
from tests.conftest import login, make_user


@pytest.mark.filterwarnings("ignore:Cannot correctly sort tables")
def test_migrated_schema_matches_models(app):
    engine = sa.create_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == [], f"Схема после миграций расходится с моделями — нужна миграция: {diff}"


def test_too_long_string_is_rejected(app, db):
    db.add(Counterparty(name="ООО «Тест»", kpp=" 550301001"))  # 10 символов в String(9)
    with pytest.raises(database.StringTooLongError, match="Counterparty.kpp"):
        db.flush()
    db.rollback()


def test_too_long_string_only_logged_when_not_strict(app, db, monkeypatch, caplog):
    monkeypatch.setattr(database, "STRICT_STRING_LENGTHS", False)
    db.add(Counterparty(name="ООО «Тест»", kpp=" 550301001"))
    db.flush()
    assert "Counterparty.kpp" in caplog.text
    db.rollback()


def test_requisite_helpers():
    assert compact_requisite(" 5507 109692 ") == "5507109692"
    assert compact_requisite("   ") is None
    assert tidy_account_number("  40703  810 7 7703 0002079 ") == "40703 810 7 7703 0002079"


def test_counterparty_form_strips_requisites(app, db, client):
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")
    client.post("/counterparties/new", data={"name": "ООО «СМС Аэро»", "inn": " 5507109692", "kpp": " 550301001 "})
    cp = db.query(Counterparty).filter_by(name="ООО «СМС Аэро»").one()
    assert (cp.inn, cp.kpp) == ("5507109692", "550301001")


def test_cooperative_requisites_stripped(app, db, client):
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")
    client.post("/setup/cooperative", data={
        "full_name": "ГСК «Тест»", "inn": "7610 037501", "kpp": " 761001001", "ogrn": "1027601 234567",
    })
    coop = db.query(Cooperative).one()
    assert (coop.inn, coop.kpp, coop.ogrn) == ("7610037501", "761001001", "1027601234567")
