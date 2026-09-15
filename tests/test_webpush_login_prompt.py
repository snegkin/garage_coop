"""
Галочка «получать push-уведомления» — на форме принудительной смены
пароля при первом входе (auth/force_change_password.html), а не на форме
входа/восстановления пароля (по прямой просьбе перенесено оттуда: та
страница видна регулярно, эта — ровно один раз). Отмечена по умолчанию;
отмеченная сразу включает канал webpush и ВСЕ виды уведомлений (по прямой
просьбе — "по умолчанию предупреждать о всех возможных событиях"), а
также заводит одноразовый флаг в сессии (см. app/__init__.py:_inject_user),
который next-страница подхватывает и запрашивает у браузера разрешение
(см. base.html) — здесь это не тестируется (JS/браузер), только сама
подготовка со стороны сервера.
"""
from app.models import User, NotificationChannel
from tests.conftest import make_person, make_user, login


def _make_first_login_user(db, username="firstlogin1", password="temp1234"):
    person = make_person(db, full_name="Первовходов Перв Первович")
    user = make_user(db, username, password, person=person)
    user.must_change_password = True
    user.notify_channel = NotificationChannel.EMAIL
    user.notify_charge = False
    user.notify_forum = False
    db.commit()
    return user


def test_force_change_password_checkbox_checked_by_default(app, db, client):
    """Форма отдаёт галочку отмеченной — сама HTML-разметка, не сервер."""
    _make_first_login_user(db)
    login(client, "firstlogin1", "temp1234")

    resp = client.get("/auth/change-password")
    assert resp.status_code == 200
    start = resp.data.index(b'name="enable_webpush"')
    checkbox_tag = resp.data[start:resp.data.index(b">", start)]
    assert b"checked" in checkbox_tag


def test_force_change_password_with_checkbox_enables_channel_and_all_events(app, db, client):
    _make_first_login_user(db)
    login(client, "firstlogin1", "temp1234")

    resp = client.post("/auth/change-password", data={
        "new_password": "newpassword1", "confirm_password": "newpassword1",
        "enable_webpush": "1",
    })
    assert resp.status_code == 302

    db.expire_all()
    user = db.query(User).filter_by(username="firstlogin1").one()
    assert user.must_change_password is False
    assert user.notify_channel == NotificationChannel.WEBPUSH
    assert user.notify_charge is True
    assert user.notify_payment is True
    assert user.notify_news is True
    assert user.notify_forum is True
    assert user.notify_board_chat is True

    with client.session_transaction() as sess:
        assert sess.get("webpush_prompt") is True


def test_force_change_password_unchecked_leaves_notify_channel_unchanged(app, db, client):
    """Человек явно снял галочку — ничего не меняем."""
    _make_first_login_user(db)
    login(client, "firstlogin1", "temp1234")

    resp = client.post("/auth/change-password", data={
        "new_password": "newpassword1", "confirm_password": "newpassword1",
        # enable_webpush отсутствует — как при снятой галочке в браузере
    })
    assert resp.status_code == 302

    db.expire_all()
    user = db.query(User).filter_by(username="firstlogin1").one()
    assert user.must_change_password is False
    assert user.notify_channel == NotificationChannel.EMAIL  # не тронут

    with client.session_transaction() as sess:
        assert not sess.get("webpush_prompt")


def test_login_form_has_no_webpush_checkbox(app, db, client):
    """Регрессия: галочку убрали с формы входа."""
    person = make_person(db, full_name="Обычнов Обычный Обычнович")
    make_user(db, "obychnov1", "pass12345", person=person)
    db.commit()

    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert b"enable_webpush" not in resp.data


def test_reset_password_form_has_no_webpush_checkbox(app, db, client):
    """Регрессия: галочку убрали с формы восстановления пароля."""
    resp = client.get("/auth/reset-password?target=someone@example.com")
    assert resp.status_code == 200
    assert b"enable_webpush" not in resp.data
