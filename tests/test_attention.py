"""
Колокольчик «Требует внимания» в шапке (base.html, _attention_menu.html) и
его опрос /attention (main.attention) — счётчики из app/attention.py.
Колокольчик стоит вне .navbar-collapse, чтобы на телефоне уведомления были
видны без раскрытия меню; бейджей на имени пользователя больше нет.
"""
import re

from app.models import RoleEnum, VoteStatus

from tests.conftest import make_person, make_user, login
from tests.test_mailbox import _make_board, _make_settings
from tests.test_voting import _make_vote


def _make_member(db):
    person = make_person(db, full_name="Член Кооператива")
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()


def _bell(body):
    """Разметка колокольчика — от id до закрытия его списка."""
    start = body.index('id="attentionBell"')
    return body[start:body.index("</ul>", start)]


def test_bell_shows_pending_vote_outside_collapsed_menu(db, client):
    _make_member(db)
    _make_vote(db)
    db.commit()
    login(client, "member1", "pass12345")

    body = client.get("/").get_data(as_text=True)
    # до .navbar-collapse — виден и при свёрнутом меню на телефоне
    assert body.index('id="attentionBell"') < body.index('class="collapse navbar-collapse"')
    bell = _bell(body)
    assert re.search(r'data-attention-total>1<', bell)
    assert "bi-bell-fill" in bell
    assert "Не проголосовано" in bell
    assert re.search(r'class="d-none" data-attention-empty', bell)


def test_bell_empty_state_when_nothing_pending(db, client):
    _make_member(db)
    login(client, "member1", "pass12345")

    bell = _bell(client.get("/").get_data(as_text=True))
    assert re.search(r'badge[^"]*d-none[^"]*" data-attention-total>0<', bell)
    assert "Новых уведомлений нет" in bell
    assert re.search(r'class="" data-attention-empty', bell)


def test_closed_vote_is_not_counted(db, client):
    _make_member(db)
    _make_vote(db, status=VoteStatus.CLOSED)
    db.commit()
    login(client, "member1", "pass12345")

    data = client.get("/attention").get_json()
    assert data["total"] == 0


def test_no_bell_for_anonymous(client):
    body = client.get("/").get_data(as_text=True)
    assert 'id="attentionBell"' not in body


def test_attention_endpoint_returns_total_and_menu_html(db, client):
    _make_member(db)
    _make_vote(db)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.get("/attention")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1
    assert "Не проголосовано" in data["html"]
    assert data["mailbox_unread"] == 0


def test_attention_endpoint_anonymous_gets_401_without_flash(client):
    resp = client.get("/attention")
    assert resp.status_code == 401
    # фоновый опрос после истечения сессии не должен оставлять flash «войдите»
    body = client.get("/").get_data(as_text=True)
    assert "Пожалуйста, войдите в систему" not in body


def test_mail_unread_counted_in_bell_for_board(db, client):
    _make_board(db)
    _make_settings(db, unread_count=3)
    login(client, "board1", "pass1234")

    data = client.get("/attention").get_json()
    assert data["total"] == 3
    assert data["mailbox_unread"] == 3
    assert "Непрочитанные письма" in data["html"]


def test_profile_menu_has_no_notification_badges(db, client):
    _make_board(db)
    _make_settings(db, unread_count=3)
    login(client, "board1", "pass1234")

    body = client.get("/").get_data(as_text=True)
    start = body.index("board1</span>")
    profile = body[start:body.index("</ul>", start)]
    assert "badge" not in profile
    assert "Непрочитанные письма" not in profile
