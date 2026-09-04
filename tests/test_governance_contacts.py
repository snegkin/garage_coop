"""
Меню «Правление» (было «Состав правления», одиночная ссылка) стало
выпадающим с двумя пунктами: «Контакты» (новая отдельная страница
/governance/contacts — таблица контактов действующего созыва, вынесенная
с общей страницы) и «Созывы» (старая /governance/ — созывы правления,
ревизионная комиссия, бухгалтер, без контактов).
"""
import datetime as dt

from app.models import RoleEnum, Person, Phone, BoardTerm, BoardMember

from tests.conftest import make_user, login


def _make_board_with_chairman(db, phone="+79161234567", email="ivanov@example.com", telegram="@ivanov", vk="ivanov", max_messenger="ivanov"):
    person = Person(full_name="Иванов Иван Иванович", email=email, telegram=telegram, vk=vk, max_messenger=max_messenger)
    db.add(person)
    db.flush()
    db.add(Phone(person_id=person.id, number=phone))
    term = BoardTerm(start_date=dt.date(2026, 1, 1))
    db.add(term)
    db.flush()
    db.add(BoardMember(term_id=term.id, person_id=person.id, is_chairman=True))
    return person


def test_nav_menu_has_pravlenie_dropdown_with_contacts_and_convocations(db, client):
    _make_board_with_chairman(db)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/governance/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert ">Правление<" in body
    assert 'href="/governance/contacts">Контакты<' in body
    assert 'href="/governance/">Созывы<' in body


def test_convocations_page_no_longer_shows_contact_details(db, client):
    _make_board_with_chairman(db)
    make_user(db, "board2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board2", "pass12345")

    resp = client.get("/governance/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Контакты действующего правления" not in body
    assert "+79161234567" not in body
    assert "ivanov@example.com" not in body
    # ФИО председателя легитимно фигурирует в таблице «Созывы» (колонка
    # «Председатель») — тест только про отсутствие контактных данных
    assert "Созывы правления" in body
    assert "Ревизионная комиссия" in body
    assert "Бухгалтер" in body


def test_contacts_page_shows_clickable_contact_details(db, client):
    _make_board_with_chairman(db)
    make_user(db, "board3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board3", "pass12345")

    resp = client.get("/governance/contacts")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Контакты действующего правления" in body
    assert "Иванов Иван Иванович" in body
    assert 'href="tel:+79161234567"' in body
    assert 'href="mailto:ivanov@example.com"' in body
    assert 'href="https://t.me/ivanov"' in body
    assert 'href="https://vk.com/ivanov"' in body
    assert "<td>ivanov</td>" in body  # MAX — без придуманной ссылки, просто текст (не <a>)


def test_contacts_page_accessible_to_regular_member(db, client):
    """Открытые данные — доступны любому залогиненному, не только правлению."""
    _make_board_with_chairman(db)
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.get("/governance/contacts")
    assert resp.status_code == 200
    assert "Иванов Иван Иванович" in resp.get_data(as_text=True)


def test_contacts_page_empty_state_without_current_term(db, client):
    make_user(db, "board4", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board4", "pass12345")

    resp = client.get("/governance/contacts")
    assert resp.status_code == 200
    assert "Действующего состава правления пока нет." in resp.get_data(as_text=True)
