"""
Вход по номеру телефона (auth.login_by_phone, /auth/login-phone) —
альтернатива логину/паролю на странице входа. Номер сверяется со всеми
Phone кооператива, независимо от формата записи (см. auth._normalize_phone_digits).

Если у найденного Person ещё нет учётной записи — она создаётся тут же,
"самостоятельной регистрацией" (пароль — тот, что человек ввёл сам).
Если учётная запись уже есть — обычная проверка пароля.
"""
from app.models import RoleEnum, User, Phone

from tests.conftest import make_person, make_user


def _add_phone(db, person, number, label="мобильный"):
    phone = Phone(person_id=person.id, number=number, label=label)
    db.add(phone)
    db.flush()
    return phone


# ---------------------------------------------------------------------------
# Самостоятельная регистрация (аккаунта ещё не было)
# ---------------------------------------------------------------------------

def test_login_by_phone_creates_account_when_none_exists(db, client):
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "+7 900 123-45-67")
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "89001234567", "password": "mypassword1"})
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/auth/login"

    user = db.query(User).filter_by(person_id=person.id).one()
    assert user.username == "iivanov"
    assert user.role == RoleEnum.MEMBER

    # Сессия действительно открыта — дашборд доступен без повторного логина
    # (редирект в /auth/login случился бы, не будь пользователь залогинен).
    dash = client.get("/dashboard")
    assert "/auth/login" not in (dash.headers.get("Location") or "")


def test_login_by_phone_normalizes_different_formats(db, client):
    """Разные форматы одного и того же номера должны совпасть."""
    person = make_person(db, full_name="Петров Пётр Петрович")
    _add_phone(db, person, "8 (900) 111-22-33")
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "+79001112233", "password": "somepass1"})
    assert resp.status_code == 302
    assert db.query(User).filter_by(person_id=person.id).count() == 1


def test_login_by_phone_sets_role_from_person_flags(db, client):
    person = make_person(db, full_name="Правленцев Прав Правленцевич", is_board_member=True)
    _add_phone(db, person, "9997654321")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9997654321", "password": "boardpass1"})
    user = db.query(User).filter_by(person_id=person.id).one()
    assert user.role == RoleEnum.BOARD


def test_login_by_phone_second_password_becomes_the_login_password(db, client):
    """Пароль для новой учётной записи — тот, что ввели в форму, а не
    случайно сгенерированный (в отличие от мастера настройки). Проверяем
    повторным входом по логину/паролю: неудачный вход по логину НЕ
    редиректит (просто перерисовывает форму со флэшем, 200), успешный —
    редиректит (302) — этого достаточно, чтобы отличить успех от провала."""
    person = make_person(db, full_name="Сидоров Семён Семёнович")
    _add_phone(db, person, "9161234567")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9161234567", "password": "my-chosen-pass"})
    client.get("/auth/logout")

    resp = client.post("/auth/login", data={"username": "ssidorov", "password": "my-chosen-pass"})
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Разрешение коллизии логина при регистрации
# ---------------------------------------------------------------------------

def test_login_by_phone_escalates_login_on_collision(db, client):
    make_user(db, "iivanov", "existingpass", role=RoleEnum.MEMBER)  # занял базовый логин
    person = make_person(db, full_name="Иванов Иван Иванович")
    _add_phone(db, person, "9031112233")
    db.commit()

    client.post("/auth/login-phone", data={"phone": "9031112233", "password": "newpass1"})
    user = db.query(User).filter_by(person_id=person.id).one()
    assert user.username == "iiivanov"


# ---------------------------------------------------------------------------
# Учётная запись уже существует — обычный вход по паролю
# ---------------------------------------------------------------------------

def test_login_by_phone_logs_in_existing_account(db, client):
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
