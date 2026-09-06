"""
app/audit_format.py — кликабельные логины/телефоны/ФИО в тексте записей
журнала аудита. Логин/телефон линкуются только внутри «кавычек» (так их
всегда пишет audit.record по всему коду), ФИО/краткое имя — прямым поиском
подстроки без кавычек. При неоднозначности (общий телефон, однофамильцы с
одинаковым кратким именем) ссылка не строится вовсе — безопаснее не дать
ссылку, чем указать не на того человека.

build_linkify_index/linkify_summary вызывают url_for — нужен активный
request context (не просто app_context), поэтому все тесты оборачивают
вызов в `with app.test_request_context():`.
"""
from app.audit_format import build_linkify_index, linkify_summary
from app.models import RoleEnum, Phone

from tests.conftest import make_person, make_user


def test_username_in_quotes_becomes_link(app, db):
    person = make_person(db, full_name="Иванов Иван Иванович")
    user = make_user(db, "ivanov1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()

    with app.test_request_context():
        index = build_linkify_index([person], [user])
        html = str(linkify_summary("Неудачная попытка входа: логин «ivanov1»", index))

    assert f'<a href="/persons/{person.id}">ivanov1</a>' in html
    assert "«" in html and "»" in html  # кавычки вокруг ссылки сохранены


def test_unknown_username_in_quotes_stays_plain_text(app, db):
    with app.test_request_context():
        index = build_linkify_index([], [])
        html = str(linkify_summary("Неудачная попытка входа: логин «nosuchuser»", index))
    assert "<a " not in html
    assert "«nosuchuser»" in html


def test_phone_in_quotes_becomes_link_regardless_of_format(app, db):
    person = make_person(db, full_name="Петров Пётр Петрович")
    db.add(Phone(person_id=person.id, number="+7 915 977-83-61"))
    db.commit()

    with app.test_request_context():
        index = build_linkify_index([person], [])
        html = str(linkify_summary("Неудачная попытка входа по телефону: «89159778361»", index))

    assert f'<a href="/persons/{person.id}">89159778361</a>' in html


def test_full_name_without_quotes_becomes_link(app, db):
    person = make_person(db, full_name="Сидоров Сидор Сидорович")
    db.commit()

    with app.test_request_context():
        index = build_linkify_index([person], [])
        html = str(linkify_summary(f"Изменён судебный участок по месту жительства для {person.full_name}", index))

    assert f'<a href="/persons/{person.id}">Сидоров Сидор Сидорович</a>' in html


def test_short_name_in_parens_becomes_link(app, db):
    person = make_person(db, full_name="Кузнецов Кузьма Кузьмич")
    user = make_user(db, "kuznetsov1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()

    with app.test_request_context():
        index = build_linkify_index([person], [user])
        html = str(linkify_summary(f"Правление сбросило пароль пользователю «kuznetsov1» ({person.short_name})", index))

    assert f'<a href="/persons/{person.id}">kuznetsov1</a>' in html
    assert f'<a href="/persons/{person.id}">{person.short_name}</a>' in html


def test_longer_name_wins_over_shorter_overlapping_name(app, db):
    """У одного человека краткое имя «Иванов И.» не должно откусывать кусок
    полного ФИО другого/того же человека при пересечении — длинное
    совпадение проверяется первым."""
    person = make_person(db, full_name="Иванов Иван Иванович")
    db.commit()

    with app.test_request_context():
        index = build_linkify_index([person], [])
        html = str(linkify_summary(f"Изменения для «{person.full_name}» одобрены", index))

    assert f'<a href="/persons/{person.id}">Иванов Иван Иванович</a>' in html
    # не должно получиться двух вложенных/пересекающихся ссылок на кусок имени
    assert html.count("<a ") == 1


def test_ambiguous_short_name_shared_by_two_people_is_not_linked(app, db):
    p1 = make_person(db, full_name="Иванов Игорь Игоревич")
    p2 = make_person(db, full_name="Иванов Илья Ильич")
    db.commit()
    assert p1.short_name == p2.short_name == "Иванов И.И."

    with app.test_request_context():
        index = build_linkify_index([p1, p2], [])
        html = str(linkify_summary(f"Человек «{p1.short_name}» отправлен в архив", index))

    assert "<a " not in html
    assert f"«{p1.short_name}»" in html


def test_ambiguous_shared_phone_is_not_linked(app, db):
    p1 = make_person(db, full_name="Общий Телефон Первый")
    p2 = make_person(db, full_name="Общий Телефон Второй")
    db.add(Phone(person_id=p1.id, number="+7 900 000-00-01"))
    db.add(Phone(person_id=p2.id, number="+7 900 000-00-01"))
    db.commit()

    with app.test_request_context():
        index = build_linkify_index([p1, p2], [])
        html = str(linkify_summary("Неудачная попытка входа по телефону: «79000000001»", index))

    assert "<a " not in html


def test_html_special_characters_in_summary_are_escaped(app):
    with app.test_request_context():
        index = build_linkify_index([], [])
        html = str(linkify_summary("Комментарий <script>alert(1)</script>", index))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_summary_returns_as_is(app):
    with app.test_request_context():
        index = build_linkify_index([], [])
        assert linkify_summary(None, index) is None
        assert linkify_summary("", index) == ""
