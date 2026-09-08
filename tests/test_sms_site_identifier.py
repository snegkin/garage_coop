"""
Название/сайт кооператива в тексте SMS с кодом подтверждения
(app/sms/__init__.py: sms_site_identifier, app/auth.py: _sms_code_text,
app/sms_settings.py: send_test) — требование SMS Aero (и операторов
связи вообще) при отправке от чужого/бесплатного имени отправителя:
сообщение с кодом должно содержать название компании/системы или адрес
сайта, иначе "Код: 1234" блокируется как спам. Ссылка — без протокола
(http(s)://), иначе тоже считается подозрительной.
"""
import re
from unittest.mock import patch

import pytest

from app.sms import sms_site_identifier
from app.models import Cooperative, RoleEnum, Phone

from tests.conftest import make_person, make_user, login


def _make_coop(db, **kwargs):
    coop = Cooperative(
        full_name="Тестовый гаражный кооператив", inn="1234567890", kpp="123456789", ogrn="1234567890123",
        **kwargs,
    )
    db.add(coop)
    db.flush()
    return coop


class _FakeSmsClient:
    def __init__(self):
        self.sent = []

    def send(self, phone_digits, text):
        self.sent.append((phone_digits, text))

    def last_text(self):
        return self.sent[-1][1]

    def last_code(self):
        match = re.search(r"\d{6}", self.sent[-1][1])
        assert match
        return match.group(0)


@pytest.fixture()
def fake_sms(monkeypatch):
    client = _FakeSmsClient()
    monkeypatch.setattr("app.auth.get_sms_client", lambda settings: client)
    return client


# ---------------------------------------------------------------------------
# sms_site_identifier — сама функция выбора идентификатора
# ---------------------------------------------------------------------------

def test_prefers_website_over_name(db):
    coop = _make_coop(db, short_name="ГСК", website="https://mycoop.ru/")
    assert sms_site_identifier(coop) == "mycoop.ru"


def test_strips_http_protocol():
    coop = Cooperative(full_name="X", website="http://mycoop.ru")
    assert sms_site_identifier(coop) == "mycoop.ru"


def test_strips_non_http_scheme_too():
    """urlparse достаёт netloc независимо от схемы, не только http/https."""
    coop = Cooperative(full_name="X", website="ftp://mycoop.ru")
    assert sms_site_identifier(coop) == "mycoop.ru"


def test_strips_path_and_query():
    coop = Cooperative(full_name="X", website="https://mycoop.ru/about?ref=1")
    assert sms_site_identifier(coop) == "mycoop.ru"


def test_handles_bare_domain_without_scheme():
    coop = Cooperative(full_name="X", website="mycoop.ru")
    assert sms_site_identifier(coop) == "mycoop.ru"


def test_handles_bare_domain_with_path_and_no_scheme():
    coop = Cooperative(full_name="X", website="mycoop.ru/about")
    assert sms_site_identifier(coop) == "mycoop.ru"


def test_handles_cyrillic_domain():
    """Домен в зоне .рф часто вводят кириллицей как есть (не в punycode) —
    urlparse не привязан к ASCII, netloc достаётся так же корректно."""
    coop = Cooperative(full_name="X", website="https://мойгараж.рф")
    assert sms_site_identifier(coop) == "мойгараж.рф"


def test_handles_cyrillic_domain_without_scheme():
    coop = Cooperative(full_name="X", website="мойгараж.рф")
    assert sms_site_identifier(coop) == "мойгараж.рф"


def test_handles_punycode_domain():
    """Тот же кириллический домен, но в закодированном (punycode) виде —
    как его отдают некоторые регистраторы/панели управления."""
    coop = Cooperative(full_name="X", website="https://xn----dtbbg1boax0b.xn--p1ai")
    assert sms_site_identifier(coop) == "xn----dtbbg1boax0b.xn--p1ai"


def test_falls_back_to_short_name_when_no_website(db):
    coop = _make_coop(db, short_name="ГСК Ромашка")
    assert sms_site_identifier(coop) == "ГСК Ромашка"


def test_falls_back_to_full_name_when_no_short_name_or_website(db):
    coop = _make_coop(db)
    assert sms_site_identifier(coop) == "Тестовый гаражный кооператив"


def test_empty_when_no_cooperative():
    assert sms_site_identifier(None) == ""


# ---------------------------------------------------------------------------
# auth.py — код подтверждения/восстановления пароля по SMS
# ---------------------------------------------------------------------------

def test_registration_code_sms_includes_website(db, client, fake_sms):
    _make_coop(db, website="https://mycoop.ru")
    person = make_person(db, full_name="Регистратов Ким Кимович")
    db.add(Phone(person_id=person.id, number="+7 900 111-22-33"))
    db.commit()

    client.post("/auth/login-phone", data={
        "phone": "89001112233", "password": "pass12345",
    })
    text = fake_sms.last_text()
    assert "mycoop.ru" in text
    assert "https://" not in text
    assert re.search(r"\d{6}", text)  # код по-прежнему извлекается


def test_registration_code_sms_has_no_parentheses_without_cooperative(db, client, fake_sms):
    """Без записи Cooperative вообще — текст остаётся как раньше, без
    пустых скобок."""
    person = make_person(db, full_name="Регистратов Ник Никович")
    db.add(Phone(person_id=person.id, number="+7 900 111-22-44"))
    db.commit()

    client.post("/auth/login-phone", data={
        "phone": "89001112244", "password": "pass12345",
    })
    text = fake_sms.last_text()
    assert "(" not in text


def test_password_reset_code_sms_includes_website(db, client, fake_sms):
    _make_coop(db, website="https://mycoop.ru")
    person = make_person(db, full_name="Восстановов Пароль Парольевич")
    db.add(Phone(person_id=person.id, number="+7 900 555-66-77"))
    make_user(db, "resetowner1", "oldpassword", person=person)
    db.commit()

    client.post("/auth/forgot-password", data={"identifier": "89005556677"})
    text = fake_sms.last_text()
    assert "mycoop.ru" in text
    assert "https://" not in text


# ---------------------------------------------------------------------------
# sms_settings.py — тестовая отправка
# ---------------------------------------------------------------------------

def test_settings_test_message_includes_website(db, client):
    _make_coop(db, website="https://mycoop.ru")
    person = make_person(db, full_name="Председателев Пред Предович")
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()
    login(client, "chair1", "pass12345")
    client.post("/sms/settings", data={"smsaero_email": "me@example.com", "smsaero_api_key": "secretkey123"})

    with patch("app.sms.smsaero.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"success": True}
        client.post("/sms/test", data={"test_phone": "9001234567"})
        sent_json = mock_post.call_args.kwargs["json"]

    assert "mycoop.ru" in sent_json["text"]
    assert "https://" not in sent_json["text"]
