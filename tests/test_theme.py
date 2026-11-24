"""Тема: светлая / тёмная / как в системе (по умолчанию — по prefers-color-scheme)."""
import pytest

from app.models import RoleEnum
from app.theme import next_theme_mode

from tests.conftest import make_user, login


def _login(client, db):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")


@pytest.mark.parametrize("os_theme, cycle", [
    ("light", ["auto", "dark", "light", "auto"]),
    ("dark", ["auto", "light", "dark", "auto"]),
])
def test_cycle_first_click_always_changes_look(os_theme, cycle):
    for cur, nxt in zip(cycle, cycle[1:]):
        assert next_theme_mode(cur, os_theme) == nxt


def test_default_mode_is_auto(client, db):
    _login(client, db)
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'var mode = "auto";' in body
    assert 'href="/set-theme/dark"' in body  # сервер считает ОС светлой


def test_forced_theme_and_back_to_auto(client, db):
    _login(client, db)
    client.get("/set-theme/dark")
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'var mode = "dark";' in body
    assert 'data-bs-theme="dark"' in body

    client.get("/set-theme/auto")
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'var mode = "auto";' in body
