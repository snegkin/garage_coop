"""Тема: пока не выбрана кнопкой — по настройкам ОС (prefers-color-scheme)."""
from app.models import RoleEnum

from tests.conftest import make_user, login

AUTO_MARK = 'matchMedia("(prefers-color-scheme: dark)")'


def _dashboard(client, db):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")


def test_theme_follows_os_until_chosen(client, db):
    _dashboard(client, db)
    body = client.get("/dashboard").get_data(as_text=True)
    assert AUTO_MARK in body
    assert 'id="themeToggle"' in body


def test_forced_theme_disables_os_detection(client, db):
    _dashboard(client, db)
    client.get("/set-theme/dark")
    body = client.get("/dashboard").get_data(as_text=True)
    assert AUTO_MARK not in body
    assert 'data-bs-theme="dark"' in body
    assert "/set-theme/light" in body
