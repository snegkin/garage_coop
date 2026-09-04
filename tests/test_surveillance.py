"""
Видеонаблюдение (`/surveillance/`, app/surveillance.py) — общедоступный
раздел (доступен и анонимному посетителю, без входа), настройка
регистраторов/камер — только председателю.

Отдельный фокус: is_chairman()/is_board() в app/permissions.py читают
g.user.role БЕЗ проверки на None — раньше это было безопасно, т.к. каждая
страница сайта требовала входа (g.user гарантированно не None к моменту
вызова). Раздел видеонаблюдения — первая страница, где это НЕ так
(видна анонимному посетителю), поэтому шаблон обязан гейтить такие вызовы
через `current_user and is_chairman()` (короткое замыкание Jinja), а не
звать is_chairman() напрямую — иначе AttributeError на 'NoneType'.
"""
import datetime as dt
import os

from app import database
from app.models import RoleEnum, DvrRecorder, DvrCamera
from app.bank_api import crypto
from app.surveillance import rtsp_url, snapshot_path, history_dir

from tests.conftest import make_person, make_user, login


def make_recorder(db, name="Регистратор 1", host="192.168.1.20", port=554, username="admin", password="secret", **kwargs):
    rec = DvrRecorder(
        name=name, host=host, port=port, username=username,
        password_encrypted=crypto.encrypt(password) if password else None,
        **kwargs,
    )
    db.add(rec)
    db.flush()
    return rec


def make_camera(db, recorder, label="Камера 1", channel=1, stream=0, **kwargs):
    cam = DvrCamera(recorder_id=recorder.id, label=label, channel=channel, stream=stream, **kwargs)
    db.add(cam)
    db.flush()
    return cam


# ---------------------------------------------------------------------------
# Доступность страницы — анонимно и под разными ролями
# ---------------------------------------------------------------------------

def test_anonymous_can_view_surveillance_page(app, db, client):
    """Критично: не должно падать с AttributeError на g.user.role — см.
    докстринг модуля."""
    recorder = make_recorder(db)
    make_camera(db, recorder)
    db.commit()

    resp = client.get("/surveillance/")
    assert resp.status_code == 200
    assert "Регистратор 1" in resp.get_data(as_text=True)


def test_anonymous_does_not_see_management_controls(app, db, client):
    recorder = make_recorder(db)
    db.commit()

    resp = client.get("/surveillance/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "addRecorderModal" not in body
    assert f'/surveillance/recorders/{recorder.id}/delete' not in body
    assert f'editRecorderModal{recorder.id}' not in body


def test_plain_member_does_not_see_management_controls(app, db, client):
    person = make_person(db, full_name="Рядовой Член Членович")
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER, person=person)
    recorder = make_recorder(db)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.get("/surveillance/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "addRecorderModal" not in body
    assert f'/surveillance/recorders/{recorder.id}/delete' not in body
    assert f'editRecorderModal{recorder.id}' not in body


def test_board_member_does_not_see_management_controls(app, db, client):
    """Настройка — строго председателю, не всему правлению (RTSP-логин/пароль
    — чувствительные данные, тот же уровень, что у настроек eWeLink)."""
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/surveillance/")
    assert resp.status_code == 200
    assert "addRecorderModal" not in resp.get_data(as_text=True)


def test_chairman_sees_management_controls(app, db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    recorder = make_recorder(db)
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.get("/surveillance/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "addRecorderModal" in body
    assert f'/surveillance/recorders/{recorder.id}/delete' in body
    assert f'editRecorderModal{recorder.id}' in body
    assert f'/surveillance/recorders/{recorder.id}/edit' in body


def test_nav_link_visible_to_any_logged_in_role(app, db, client):
    """Пункт меню «Видеонаблюдение» перенесён в выпадающее меню
    «Кооператив», но БЕЗ гейта по роли — сам раздел общедоступен по прямому
    URL (нет @login_required/@roles_required на surveillance.view, см.
    модуль), поэтому и пункт меню видно любому вошедшему пользователю
    (правило «права по пунктам меню, по факту декоратора на роуте», см.
    base.html). Анонимному посетителю ссылка всё ещё показывается отдельно
    на странице входа — см. test_login_page_has_surveillance_link_for_anonymous
    ниже."""
    person = make_person(db, full_name="Обычный Человек Человекович")
    make_user(db, "member2", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member2", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code in (200, 302)  # рядовой член редиректится в кабинет — nav всё равно есть в base.html
    resp = client.get("/cabinet/garages") if resp.status_code == 302 else resp
    body = resp.get_data(as_text=True)
    assert '/surveillance/' in body

    # Сама страница при этом открыта напрямую, без каких-либо прав.
    assert client.get("/surveillance/").status_code == 200

    client.get("/auth/logout")
    board_person = make_person(db, full_name="Правление Человекович")
    make_user(db, "board9", "pass12345", role=RoleEnum.BOARD, person=board_person)
    db.commit()
    login(client, "board9", "pass12345")
    board_body = client.get("/dashboard").get_data(as_text=True)
    assert '/surveillance/' in board_body


def test_login_page_has_surveillance_link_for_anonymous(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert '/surveillance/' in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Создание регистратора (+ камеры одной формой)
# ---------------------------------------------------------------------------

def test_chairman_creates_recorder_with_cameras(app, db, client):
    make_user(db, "chair2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair2", "pass12345")

    resp = client.post("/surveillance/recorders/new", data={
        "name": "Регистратор — въезд",
        "host": "192.168.1.20",
        "port": "8554",
        "username": "admin",
        "password": "s3cr3t",
        "camera_label": ["Въезд", "Двор"],
        "camera_channel": ["1", "2"],
        "camera_stream": ["0", "1"],
    })
    assert resp.status_code == 302
    db.expire_all()

    recorder = db.query(DvrRecorder).filter_by(name="Регистратор — въезд").one()
    assert recorder.host == "192.168.1.20"
    assert recorder.port == 8554
    # Пароль не хранится открытым текстом.
    assert recorder.password_encrypted != "s3cr3t"
    assert crypto.decrypt(recorder.password_encrypted) == "s3cr3t"

    cameras = db.query(DvrCamera).filter_by(recorder_id=recorder.id).order_by(DvrCamera.sort_order).all()
    assert len(cameras) == 2
    assert cameras[0].label == "Въезд" and cameras[0].channel == 1 and cameras[0].stream == 0
    assert cameras[1].label == "Двор" and cameras[1].channel == 2 and cameras[1].stream == 1


def test_create_recorder_skips_blank_camera_rows(app, db, client):
    """Строка камеры без номера канала (например, добавили кнопкой и тут же
    убрали, но само поле почему-то дошло пустым) пропускается, не падает."""
    make_user(db, "chair3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair3", "pass12345")

    resp = client.post("/surveillance/recorders/new", data={
        "name": "Регистратор 2", "host": "10.0.0.5",
        "camera_label": ["Камера А", ""],
        "camera_channel": ["1", ""],
        "camera_stream": ["0", ""],
    })
    assert resp.status_code == 302
    db.expire_all()
    recorder = db.query(DvrRecorder).filter_by(name="Регистратор 2").one()
    assert db.query(DvrCamera).filter_by(recorder_id=recorder.id).count() == 1


def test_create_recorder_defaults_port_when_blank(app, db, client):
    make_user(db, "chair4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair4", "pass12345")

    resp = client.post("/surveillance/recorders/new", data={"name": "Регистратор 3", "host": "10.0.0.6"})
    assert resp.status_code == 302
    db.expire_all()
    recorder = db.query(DvrRecorder).filter_by(name="Регистратор 3").one()
    assert recorder.port == 554


def test_create_recorder_requires_name_and_host(app, db, client):
    make_user(db, "chair5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair5", "pass12345")

    resp = client.post("/surveillance/recorders/new", data={"name": "", "host": "10.0.0.7"})
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(DvrRecorder).count() == 0


def test_board_member_cannot_create_recorder(app, db, client):
    make_user(db, "board2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board2", "pass12345")

    resp = client.post("/surveillance/recorders/new", data={"name": "X", "host": "10.0.0.8"})
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(DvrRecorder).count() == 0


def test_anonymous_cannot_create_recorder(client, db):
    resp = client.post("/surveillance/recorders/new", data={"name": "X", "host": "10.0.0.9"})
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
    assert db.query(DvrRecorder).count() == 0


# ---------------------------------------------------------------------------
# Правка регистратора и его камер
# ---------------------------------------------------------------------------

def test_chairman_edits_recorder_fields(app, db, client):
    recorder = make_recorder(db, name="Старое имя", host="10.0.0.1", port=554, username="olduser")
    make_user(db, "chair10", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair10", "pass12345")

    resp = client.post(f"/surveillance/recorders/{recorder.id}/edit", data={
        "name": "Новое имя", "host": "10.0.0.2", "port": "8555",
        "username": "newuser", "password": "", "comment": "уточнили адрес",
    })
    assert resp.status_code == 302
    db.expire_all()

    updated = database.db_session.get(DvrRecorder, recorder.id)
    assert updated.name == "Новое имя"
    assert updated.host == "10.0.0.2"
    assert updated.port == 8555
    assert updated.username == "newuser"
    assert updated.comment == "уточнили адрес"


def test_edit_recorder_empty_password_does_not_clear_existing_secret(app, db, client):
    recorder = make_recorder(db, password="original-secret")
    make_user(db, "chair11", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair11", "pass12345")

    client.post(f"/surveillance/recorders/{recorder.id}/edit", data={
        "name": recorder.name, "host": recorder.host, "password": "",
    })
    db.expire_all()
    updated = database.db_session.get(DvrRecorder, recorder.id)
    assert crypto.decrypt(updated.password_encrypted) == "original-secret"


def test_edit_recorder_nonempty_password_replaces_secret(app, db, client):
    recorder = make_recorder(db, password="original-secret")
    make_user(db, "chair12", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair12", "pass12345")

    client.post(f"/surveillance/recorders/{recorder.id}/edit", data={
        "name": recorder.name, "host": recorder.host, "password": "new-secret",
    })
    db.expire_all()
    updated = database.db_session.get(DvrRecorder, recorder.id)
    assert crypto.decrypt(updated.password_encrypted) == "new-secret"


def test_edit_recorder_updates_existing_camera_by_id_without_recreating_it(app, db, client):
    """Камера сверяется по id (скрытое поле camera_id) — правка не должна
    пересоздавать существующую камеру новой строкой (иначе она потеряла бы
    связь с уже снятым на диске кадром до следующего кадра раз в минуту)."""
    recorder = make_recorder(db)
    camera = make_camera(db, recorder, label="Старое название", channel=1, stream=0)
    make_user(db, "chair13", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair13", "pass12345")

    resp = client.post(f"/surveillance/recorders/{recorder.id}/edit", data={
        "name": recorder.name, "host": recorder.host,
        "camera_id": [str(camera.id)],
        "camera_label": ["Новое название"],
        "camera_channel": ["2"],
        "camera_stream": ["1"],
    })
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.query(DvrCamera).filter_by(recorder_id=recorder.id).count() == 1
    updated_camera = database.db_session.get(DvrCamera, camera.id)
    assert updated_camera is not None  # тот же id, не новая запись
    assert updated_camera.label == "Новое название"
    assert updated_camera.channel == 2
    assert updated_camera.stream == 1


def test_edit_recorder_adds_new_camera_alongside_existing(app, db, client):
    recorder = make_recorder(db)
    camera = make_camera(db, recorder, label="Камера 1", channel=1)
    make_user(db, "chair14", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair14", "pass12345")

    resp = client.post(f"/surveillance/recorders/{recorder.id}/edit", data={
        "name": recorder.name, "host": recorder.host,
        "camera_id": [str(camera.id), ""],
        "camera_label": ["Камера 1", "Камера 2"],
        "camera_channel": ["1", "2"],
        "camera_stream": ["0", "0"],
    })
    assert resp.status_code == 302
    db.expire_all()

    cameras = database.db_session.query(DvrCamera).filter_by(recorder_id=recorder.id).order_by(DvrCamera.sort_order).all()
    assert len(cameras) == 2
    assert cameras[0].id == camera.id
    assert cameras[1].label == "Камера 2" and cameras[1].channel == 2


def test_edit_recorder_removes_camera_missing_from_form_and_its_snapshot_file(app, db, client, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    kept = make_camera(db, recorder, label="Оставили", channel=1)
    removed = make_camera(db, recorder, label="Убрали", channel=2)
    make_user(db, "chair15", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    snap_dir = os.path.join(str(tmp_path), str(recorder.id), "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    removed_snap = os.path.join(snap_dir, f"camera_{removed.id}.jpg")
    with open(removed_snap, "wb") as fh:
        fh.write(b"fake-jpeg-bytes")

    removed_hist_dir = history_dir(recorder.id, removed.id)
    os.makedirs(removed_hist_dir, exist_ok=True)
    with open(os.path.join(removed_hist_dir, "20260101_000000.jpg"), "wb") as fh:
        fh.write(b"fake-jpeg-bytes")

    login(client, "chair15", "pass12345")
    resp = client.post(f"/surveillance/recorders/{recorder.id}/edit", data={
        "name": recorder.name, "host": recorder.host,
        "camera_id": [str(kept.id)],
        "camera_label": ["Оставили"],
        "camera_channel": ["1"],
        "camera_stream": ["0"],
    })
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(DvrCamera, kept.id) is not None
    assert database.db_session.get(DvrCamera, removed.id) is None
    assert not os.path.exists(removed_snap)
    assert not os.path.exists(removed_hist_dir)


def test_edit_recorder_requires_name_and_host(app, db, client):
    recorder = make_recorder(db, name="Оставить как есть")
    make_user(db, "chair16", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair16", "pass12345")

    resp = client.post(f"/surveillance/recorders/{recorder.id}/edit", data={"name": "", "host": recorder.host})
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(DvrRecorder, recorder.id).name == "Оставить как есть"


def test_board_member_cannot_edit_recorder(app, db, client):
    recorder = make_recorder(db, name="Не трогать")
    make_user(db, "board9b", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board9b", "pass12345")

    resp = client.post(f"/surveillance/recorders/{recorder.id}/edit", data={"name": "Изменено", "host": recorder.host})
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(DvrRecorder, recorder.id).name == "Не трогать"


def test_anonymous_cannot_edit_recorder(client, db):
    recorder = make_recorder(db, name="Не трогать анонимно")
    db.commit()

    resp = client.post(f"/surveillance/recorders/{recorder.id}/edit", data={"name": "Изменено", "host": recorder.host})
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
    db.expire_all()
    assert database.db_session.get(DvrRecorder, recorder.id).name == "Не трогать анонимно"


# ---------------------------------------------------------------------------
# Удаление регистратора
# ---------------------------------------------------------------------------

def test_chairman_deletes_recorder_and_its_cameras(app, db, client, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    make_user(db, "chair6", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair6", "pass12345")

    snap_dir = os.path.join(str(tmp_path), str(recorder.id), "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, f"camera_{camera.id}.jpg"), "wb") as fh:
        fh.write(b"fake-jpeg-bytes")

    resp = client.post(f"/surveillance/recorders/{recorder.id}/delete")
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(DvrRecorder, recorder.id) is None
    assert database.db_session.get(DvrCamera, camera.id) is None
    assert not os.path.exists(os.path.join(str(tmp_path), str(recorder.id)))


def test_board_member_cannot_delete_recorder(app, db, client):
    recorder = make_recorder(db)
    make_user(db, "board3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board3", "pass12345")

    resp = client.post(f"/surveillance/recorders/{recorder.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(DvrRecorder, recorder.id) is not None


# ---------------------------------------------------------------------------
# Отдача кадра
# ---------------------------------------------------------------------------

def test_snapshot_returns_404_when_no_file_yet(app, db, client):
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    resp = client.get(f"/surveillance/cameras/{camera.id}/snapshot")
    assert resp.status_code == 404


def test_snapshot_returns_404_for_unknown_camera(client):
    resp = client.get("/surveillance/cameras/999999/snapshot")
    assert resp.status_code == 404


def test_snapshot_serves_saved_frame_anonymously(app, db, client, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    snap_dir = os.path.join(str(tmp_path), str(recorder.id), "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, f"camera_{camera.id}.jpg"), "wb") as fh:
        fh.write(b"fake-jpeg-bytes")

    resp = client.get(f"/surveillance/cameras/{camera.id}/snapshot")
    assert resp.status_code == 200
    assert resp.data == b"fake-jpeg-bytes"
    assert resp.mimetype == "image/jpeg"


# ---------------------------------------------------------------------------
# История кадров (галерея за сутки, см. scripts/dvr_snapshot.py)
# ---------------------------------------------------------------------------

def test_camera_history_empty_state_when_no_history_yet(app, db, client):
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    resp = client.get(f"/surveillance/cameras/{camera.id}/history")
    assert resp.status_code == 200
    assert "За последние сутки кадров пока нет." in resp.get_data(as_text=True)


def test_camera_history_returns_404_for_unknown_camera(client):
    resp = client.get("/surveillance/cameras/999999/history")
    assert resp.status_code == 404


def test_camera_history_lists_frames_newest_first(app, db, client, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    hist_dir = history_dir(recorder.id, camera.id)
    os.makedirs(hist_dir, exist_ok=True)
    for name in ("20260905_100000.jpg", "20260905_120000.jpg", "20260905_110000.jpg", "garbage.jpg"):
        with open(os.path.join(hist_dir, name), "wb") as fh:
            fh.write(b"x")

    resp = client.get(f"/surveillance/cameras/{camera.id}/history")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # самый новый (12:00) должен идти раньше самого старого (10:00) в тексте страницы
    assert body.index("20260905_120000.jpg") < body.index("20260905_100000.jpg")
    assert "garbage.jpg" not in body  # не парсится как метка времени — пропускается, не падает
    assert "3 кадров за последние сутки" in body


def test_camera_history_frame_serves_existing_file(app, db, client, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    hist_dir = history_dir(recorder.id, camera.id)
    os.makedirs(hist_dir, exist_ok=True)
    with open(os.path.join(hist_dir, "20260905_120000.jpg"), "wb") as fh:
        fh.write(b"fake-jpeg-bytes")

    resp = client.get(f"/surveillance/cameras/{camera.id}/history/20260905_120000.jpg")
    assert resp.status_code == 200
    assert resp.data == b"fake-jpeg-bytes"
    assert resp.mimetype == "image/jpeg"


def test_camera_history_frame_404_for_missing_file(app, db, client, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    resp = client.get(f"/surveillance/cameras/{camera.id}/history/20260905_120000.jpg")
    assert resp.status_code == 404


def test_camera_history_frame_rejects_path_traversal_filename(app, db, client, tmp_path, monkeypatch):
    """Имя файла должно строго совпадать с паттерном метки времени — иначе
    404, даже если в остальном похоже на попытку выйти за пределы папки
    истории (../../etc/passwd и т.п.)."""
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    for bad_name in ("..%2F..%2Fetc%2Fpasswd", "20260905_120000.jpg.exe", "not-a-timestamp.jpg"):
        resp = client.get(f"/surveillance/cameras/{camera.id}/history/{bad_name}")
        assert resp.status_code == 404, bad_name


# ---------------------------------------------------------------------------
# rtsp_url() / snapshot_path()
# ---------------------------------------------------------------------------

def test_rtsp_url_includes_credentials_and_channel_stream(app, db):
    recorder = make_recorder(db, host="192.168.1.20", port=8554, username="user", password="pass")
    camera = make_camera(db, recorder, channel=1, stream=0)
    db.commit()

    url = rtsp_url(recorder, camera)
    assert url == "rtsp://user:pass@192.168.1.20:8554/channel=1_stream=0.sdp"


def test_rtsp_url_encodes_special_characters_in_credentials(app, db):
    recorder = make_recorder(db, username="ad min", password="p@ss:word")
    camera = make_camera(db, recorder, channel=2, stream=1)
    db.commit()

    url = rtsp_url(recorder, camera)
    assert "ad%20min" in url
    assert "p%40ss%3Aword" in url
    # Отдельные @/: из пароля не должны сломать разбор URL — ровно один "@"
    # перед хостом и структура channel=2_stream=1 в конце.
    assert url.endswith("@192.168.1.20:554/channel=2_stream=1.sdp")


def test_rtsp_url_without_credentials(app, db):
    recorder = make_recorder(db, username=None, password=None)
    camera = make_camera(db, recorder, channel=3, stream=0)
    db.commit()

    url = rtsp_url(recorder, camera)
    assert url == "rtsp://192.168.1.20:554/channel=3_stream=0.sdp"


def test_snapshot_path_layout(app, db):
    path = snapshot_path(7, 42)
    assert path.endswith(os.path.join("7", "snapshots", "camera_42.jpg"))
