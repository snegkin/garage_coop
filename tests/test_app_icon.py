"""Своя иконка сайта и PWA у каждого кооператива (app/app_icon.py)."""
import io

from PIL import Image

from app.app_icon import prepare_icon, render_variant, VARIANTS
from app.models import Cooperative, RoleEnum

from tests.conftest import make_person, make_user, login


def _png(size=(300, 200), color=(200, 30, 30, 255)):
    img = Image.new("RGBA", (size[0] + 40, size[1] + 40), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", size, color), (20, 20))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _setup(db):
    db.add(Cooperative(full_name="ГСК «Тест»", inn="7610037501", kpp="761001001", ogrn="1027600000000"))
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=make_person(db, full_name="Пред Седатель"))
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=make_person(db, full_name="Член Правления"))
    db.commit()


def test_prepare_icon_squares_and_trims():
    with Image.open(io.BytesIO(prepare_icon(_png()))) as img:
        assert img.size == (512, 512)
        # прозрачные поля обрезаны: знак во всю ширину, сверху/снизу — поля до квадрата
        assert img.getpixel((5, 256))[3] == 255
        assert img.getpixel((256, 5))[3] == 0


def test_prepare_icon_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        prepare_icon(b"not an image")
    with pytest.raises(ValueError):
        prepare_icon(_png(color=(0, 0, 0, 0)))


def test_variants_sizes_and_backgrounds():
    source = prepare_icon(_png())
    for name, (size, kind) in VARIANTS.items():
        with Image.open(io.BytesIO(render_variant(source, name))) as img:
            assert img.size == (size, size), name
            if kind in ("opaque", "maskable"):
                assert img.mode == "RGB"
    with Image.open(io.BytesIO(render_variant(source, "icon-maskable-512.png"))) as img:
        assert img.getpixel((10, 10)) == (255, 255, 255)  # фон вне безопасной зоны


def test_default_icons_without_upload(db, client):
    _setup(db)
    resp = client.get("/app-icon/icon-192.png")
    assert resp.status_code == 200 and resp.mimetype == "image/png"
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/app-icon/nope.png").status_code == 404


def test_upload_replaces_icons_everywhere(db, client):
    _setup(db)
    login(client, "chair", "pass1234")
    resp = client.post("/app-icon/upload", data={"image": (io.BytesIO(_png()), "logo.png")},
                       content_type="multipart/form-data")
    data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] and data["custom"]
    assert "?v=" in data["preview"]

    with Image.open(io.BytesIO(client.get("/app-icon/icon-192.png").data)) as img:
        assert img.getpixel((96, 96))[:3] == (200, 30, 30)
    manifest = client.get("/manifest.webmanifest").get_json(force=True)
    assert all("/app-icon/" in i["src"] and "?v=" in i["src"] for i in manifest["icons"])
    page = client.get("/").get_data(as_text=True)
    assert "/app-icon/favicon-32x32.png?v=" in page

    resp = client.post("/app-icon/upload", data={"delete": "1"})
    assert resp.get_json()["custom"] is False
    db.expire_all()
    assert db.query(Cooperative).one().app_icon_png is None


def test_upload_bad_file_and_permissions(db, client):
    _setup(db)
    login(client, "chair", "pass1234")
    resp = client.post("/app-icon/upload", data={"image": (io.BytesIO(b"xx"), "a.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400 and not resp.get_json()["ok"]

    client.get("/auth/logout")
    login(client, "board1", "pass1234")
    client.post("/app-icon/upload", data={"image": (io.BytesIO(_png()), "a.png")},
                content_type="multipart/form-data")
    db.expire_all()
    assert db.query(Cooperative).one().app_icon_png is None


def test_edit_page_has_icon_card(db, client):
    _setup(db)
    login(client, "chair", "pass1234")
    body = client.get("/cooperative/edit").get_data(as_text=True)
    assert 'id="appIcon"' in body and "/app-icon/upload" in body
