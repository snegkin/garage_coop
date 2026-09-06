"""
Вход по номеру телефона (auth.login_by_phone, /auth/login-phone) —
альтернатива логину/паролю на странице входа. Номер сверяется со всеми
Phone кооператива, независимо от формата записи (см. auth._normalize_phone_digits).

Если у найденного Person ещё нет учётной записи — самостоятельная
регистрация в два шага: запрос СМС-кода (login_by_phone), затем его
подтверждение (register_phone_confirm) — учётная запись создаётся только
после этого. Если учётная запись уже есть — обычная проверка пароля, без
кода.
"""
import pytest

from app.models import RoleEnum, User, Phone

from tests.conftest import make_person, make_user


def _add_phone(db, person, number, label="мобильный"):
    phone = Phone(person_id=person.id, number=number, label=label)
    db.add(phone)
    db.flush()
    return phone


class _FakeSmsClient:
    def __init__(self):
        self.sent = []  # [(phone_digits, text)]

    def send(self, phone_digits, text):
        self.sent.append((phone_digits, text))

    def last_code(self):
        """Достаёт 6-значный код из последнего отправленного текста —
        тесты не парсят реальный формат СМС, полагаются только на то, что
        где-то в тексте есть 6 цифр подряд (см. auth.py: "Код подтверждения: {code}")."""
        import re
        match = re.search(r"\d{6}", self.sent[-1][1])
        assert match, f"код не найден в отправленном тексте: {self.sent[-1][1]!r}"
        return match.group(0)


@pytest.fixture()
def fake_sms(monkeypatch):
    client = _FakeSmsClient()
    monkeypatch.setattr("app.auth.get_sms_client", lambda settings: client)
    return client


# ---------------------------------------------------------------------------
# Самостоятельная регистрация — запрос кода
# ---------------------------------------------------------------------------

def test_login_by_phone_sends_code_when_no_account_exists(db, client, fake_sms):
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "+7 900 123-45-67")
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "89001234567", "password": "mypassword1"})
    assert resp.status_code == 200  # не редирект — страница ввода кода
    assert "code" in resp.get_data(as_text=True) or "Код" in resp.get_data(as_text=True)
    assert len(fake_sms.sent) == 1
    assert fake_sms.sent[0][0] == "9001234567"

    # Аккаунт ещё не создан — код пока не подтверждён.
    assert db.query(User).filter_by(person_id=person.id).count() == 0


def test_login_by_phone_without_sms_configured_shows_error(db, client):
    """Без настроенного СМС-провайдера (get_sms_client возвращает None) —
    понятная ошибка, а не падение."""
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "9001234568")
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "9001234568", "password": "mypassword1"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/auth/login"
    assert db.query(User).count() == 0


# ---------------------------------------------------------------------------
# Самостоятельная регистрация — подтверждение кода
# ---------------------------------------------------------------------------

def test_register_phone_confirm_creates_account_with_correct_code(db, client, fake_sms):
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "9001234567")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9001234567", "password": "mypassword1"})
    code = fake_sms.last_code()

    resp = client.post("/auth/register-phone/confirm", data={"phone": "9001234567", "code": code})
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/auth/login"

    user = db.query(User).filter_by(person_id=person.id).one()
    assert user.username == "iivanov"
    assert user.role == RoleEnum.MEMBER

    dash = client.get("/dashboard")
    assert "/auth/login" not in (dash.headers.get("Location") or "")


def test_register_phone_confirm_wrong_code_does_not_create_account(db, client, fake_sms):
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "9001234567")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9001234567", "password": "mypassword1"})
    resp = client.post("/auth/register-phone/confirm", data={"phone": "9001234567", "code": "000000"})
    assert resp.status_code == 200
    assert db.query(User).filter_by(person_id=person.id).count() == 0


def test_register_phone_confirm_sets_role_from_person_flags(db, client, fake_sms):
    person = make_person(db, full_name="Правленцев Прав Правленцевич", is_board_member=True)
    _add_phone(db, person, "9997654321")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9997654321", "password": "boardpass1"})
    code = fake_sms.last_code()
    client.post("/auth/register-phone/confirm", data={"phone": "9997654321", "code": code})

    user = db.query(User).filter_by(person_id=person.id).one()
    assert user.role == RoleEnum.BOARD


def test_register_phone_confirm_password_becomes_login_password(db, client, fake_sms):
    person = make_person(db, full_name="Сидоров Семён Семёнович")
    _add_phone(db, person, "9161234567")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9161234567", "password": "my-chosen-pass"})
    code = fake_sms.last_code()
    client.post("/auth/register-phone/confirm", data={"phone": "9161234567", "code": code})
    client.get("/auth/logout")

    resp = client.post("/auth/login", data={"username": "ssidorov", "password": "my-chosen-pass"})
    assert resp.status_code == 302


def test_register_phone_confirm_escalates_login_on_collision(db, client, fake_sms):
    make_user(db, "iivanov", "existingpass", role=RoleEnum.MEMBER)  # занял базовый логин
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "9031112233")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9031112233", "password": "newpass1"})
    code = fake_sms.last_code()
    client.post("/auth/register-phone/confirm", data={"phone": "9031112233", "code": code})

    user = db.query(User).filter_by(person_id=person.id).one()
    assert user.username == "iiivanov"


def test_register_phone_resend_sends_new_code_reusing_same_password(db, client, fake_sms):
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "9001234567")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9001234567", "password": "mypassword1"})
    first_code = fake_sms.last_code()

    resp = client.post("/auth/register-phone/resend", data={"phone": "9001234567"})
    assert resp.status_code == 200
    second_code = fake_sms.last_code()
    assert second_code != first_code

    # Старый код уже не должен приниматься, новый — должен.
    bad = client.post("/auth/register-phone/confirm", data={"phone": "9001234567", "code": first_code})
    assert db.query(User).filter_by(person_id=person.id).count() == 0

    good = client.post("/auth/register-phone/confirm", data={"phone": "9001234567", "code": second_code})
    assert good.status_code == 302
    user = db.query(User).filter_by(person_id=person.id).one()
    from werkzeug.security import check_password_hash
    assert check_password_hash(user.password_hash, "mypassword1")


# ---------------------------------------------------------------------------
# Учётная запись уже существует — обычный вход по паролю, без кода
# ---------------------------------------------------------------------------

def test_login_by_phone_logs_in_existing_account_without_sms(db, client):
    """Существующий аккаунт — вход сразу, СМС-клиент не нужен вовсе (не
    настроен, и это не мешает)."""
    person = make_person(db, full_name="Кузнецов Кузьма Кузьмич")
    _add_phone(db, person, "9051112233")
    make_user(db, "kkuznetsov", "realpassword", role=RoleEnum.MEMBER, person=person)
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "9051112233", "password": "realpassword"})
    assert resp.status_code == 302
    assert db.query(User).filter_by(person_id=person.id).count() == 1  # не создан второй аккаунт


def test_login_by_phone_wrong_password_for_existing_account(db, client):
    person = make_person(db, full_name="Кузнецов Кузьма Кузьмич")
    _add_phone(db, person, "9051112234")
    make_user(db, "kkuznetsov2", "realpassword", role=RoleEnum.MEMBER, person=person)
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "9051112234", "password": "wrongpassword"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/auth/login"


def test_login_by_phone_disabled_account(db, client):
    person = make_person(db, full_name="Отключёнов Отключён Отключёнович")
    _add_phone(db, person, "9051112235")
    user = make_user(db, "otkl", "realpassword", role=RoleEnum.MEMBER, person=person)
    user.is_active = False
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "9051112235", "password": "realpassword"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/auth/login"


# ---------------------------------------------------------------------------
# Не найдено / неоднозначно
# ---------------------------------------------------------------------------

def test_login_by_phone_unknown_number(client, db):
    resp = client.post("/auth/login-phone", data={"phone": "9000000000", "password": "whatever1"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/auth/login"
    assert db.query(User).count() == 0


def test_login_by_phone_ambiguous_number_shared_by_two_people_is_rejected(db, client):
    """Неоднозначность (два разных человека с одинаковым номером) — отказ,
    не угадываем, в чей аккаунт входить/который создавать."""
    p1 = make_person(db, full_name="Один Человек Человекович")
    p2 = make_person(db, full_name="Другой Человек Человекович")
    _add_phone(db, p1, "9009998877")
    _add_phone(db, p2, "9009998877")
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "9009998877", "password": "whatever1"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/auth/login"
    assert db.query(User).count() == 0


def test_login_by_phone_missing_fields(client, db):
    resp = client.post("/auth/login-phone", data={"phone": "", "password": ""})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/auth/login"


# ---------------------------------------------------------------------------
# Форма страницы входа
# ---------------------------------------------------------------------------

def test_login_page_has_phone_tab(client):
    resp = client.get("/auth/login")
    body = resp.get_data(as_text=True)
    assert 'name="phone"' in body
    assert "/auth/login-phone" in body


def test_login_page_has_forgot_password_link(client):
    resp = client.get("/auth/login")
    body = resp.get_data(as_text=True)
    assert "/auth/forgot-password" in body
