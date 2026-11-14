"""
Факсимиле подписи председателя и печати кооператива (app/facsimile.py):
обработка изображения (прозрачный фон, обрезка), загрузка только
председателем, вставка в документ только галочкой/параметром и только
председателю, подпись — только своя, PDF с факсимиле — в журнал.
"""
import datetime as dt
import io

from PIL import Image, ImageDraw, ImageFilter

from app import facsimile
from app.models import AuditLog, Cooperative, DocumentTemplate, IssuedDocument, Person, RoleEnum

from tests.conftest import make_person, make_user, login


def _scan(color=(20, 40, 160)) -> bytes:
    """«Скан»: белый лист с синей закорючкой посередине."""
    img = Image.new("RGB", (400, 300), (250, 250, 250))
    ImageDraw.Draw(img).line([(100, 150), (200, 120), (300, 170)], fill=color, width=8)
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def _setup(db):
    db.add(Cooperative(full_name="Гаражный кооператив «Восход»", inn="1234567890", kpp="123401001",
                       ogrn="1023400000000"))
    chairman = make_person(db, full_name="Сидоров Сидор Сидорович", is_chairman=True)
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=chairman)
    board = make_person(db, full_name="Член Правления")
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=board)
    template = DocumentTemplate(name="Объявление", title="ОБЪЯВЛЕНИЕ", addressee_html="", body_html="<p>x</p>",
                                recipient="none", show_number=False, show_signature=True, signer_role="Правление",
                                chairman_only=False, sort_order=0)
    db.add(template)
    db.flush()
    own = IssuedDocument(template_id=template.id, title="ОБЪЯВЛЕНИЕ", addressee_html="", body_html="<p>Текст</p>",
                         issued_at=dt.date(2026, 11, 13), show_signature=True, signer_role="Правление",
                         signer_person_id=chairman.id)
    other = IssuedDocument(template_id=template.id, title="ОБЪЯВЛЕНИЕ", addressee_html="", body_html="<p>Старое</p>",
                           issued_at=dt.date(2025, 1, 1), show_signature=True, signer_role="Председатель",
                           signer_person_id=board.id)
    db.add_all([own, other])
    db.commit()
    return chairman, own, other


def test_prepare_image_makes_background_transparent_and_crops():
    png = facsimile.prepare_image(_scan())
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.mode == "RGBA"
    # Поля обрезаны до штриха, фон прозрачный, сам штрих — непрозрачный и синий.
    assert img.width < 230 and img.height < 70
    assert img.getpixel((0, img.height - 1))[3] == 0
    opaque = [p for p in img.getdata() if p[3] == 255]
    assert opaque and all(p[2] > p[0] for p in opaque)


def test_prepare_image_rejects_garbage_and_blank():
    for data in (b"not an image", _blank()):
        try:
            facsimile.prepare_image(data)
        except ValueError:
            continue
        raise AssertionError("ожидался ValueError")


def _blank() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (50, 50), (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


def test_upload_only_by_chairman(db, client):
    chairman, _own, _other = _setup(db)
    login(client, "board1", "pass1234")
    client.post("/facsimile/seal", data={"image": (io.BytesIO(_scan()), "seal.jpg")},
                content_type="multipart/form-data")
    assert db.query(Cooperative).one().seal_png is None
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    client.post("/facsimile/seal", data={"image": (io.BytesIO(_scan()), "seal.jpg")},
                content_type="multipart/form-data")
    client.post("/facsimile/signature", data={"image": (io.BytesIO(_scan((10, 10, 10))), "sign.png")},
                content_type="multipart/form-data")
    db.expire_all()
    assert db.query(Cooperative).one().seal_png[:4] == b"\x89PNG"
    assert db.get(Person, chairman.id).signature_png[:4] == b"\x89PNG"
    assert db.query(AuditLog).filter(AuditLog.action.like("facsimile.%_upload")).count() == 2
    page = client.get("/cooperative/edit").get_data(as_text=True)
    assert page.count("data:image/png;base64,") >= 2

    client.post("/facsimile/seal", data={"delete": "1"})
    db.expire_all()
    assert db.query(Cooperative).one().seal_png is None


def test_document_facsimile_only_by_toggle_and_only_own_signature(db, client):
    chairman, own, other = _setup(db)
    db.query(Cooperative).one().seal_png = facsimile.prepare_image(_scan())
    db.get(Person, chairman.id).signature_png = facsimile.prepare_image(_scan((10, 10, 10)))
    db.commit()

    # Член правления: ни галочки, ни изображений в странице.
    login(client, "board1", "pass1234")
    page = client.get(f"/documents/{own.id}").get_data(as_text=True)
    assert "data-facsimile-toggle" not in page and "data:image/png" not in page
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    page = client.get(f"/documents/{own.id}").get_data(as_text=True)
    # Галочка есть, по умолчанию выключена; изображения на странице скрыты (без .shown).
    assert "data-facsimile-toggle" in page and 'data-facsimile-toggle checked' not in page
    assert 'class="fx-sign"' in page and "fx-seal" in page and "fx-sign shown" not in page
    assert "data-pdf-link" in page

    # Чужой документ (подписант — не вошедший председатель): печать есть, подписи нет.
    page = client.get(f"/documents/{other.id}").get_data(as_text=True)
    assert 'class="fx-sign' not in page and "fx-seal" in page

    # PDF без галочки — без записи в журнал; с facsimile=1 — с записью.
    assert client.get(f"/documents/{own.id}/pdf").data[:4] == b"%PDF"
    assert db.query(AuditLog).filter_by(action="facsimile.pdf").count() == 0
    assert client.get(f"/documents/{own.id}/pdf?facsimile=1").data[:4] == b"%PDF"
    assert db.query(AuditLog).filter_by(action="facsimile.pdf").count() == 1

    page = client.get(f"/documents/{own.id}?facsimile=1").get_data(as_text=True)
    assert "fx-sign shown" in page and "seal-shown" in page


def test_board_member_param_is_ignored(db, client):
    _chairman, own, _other = _setup(db)
    db.query(Cooperative).one().seal_png = facsimile.prepare_image(_scan())
    db.commit()
    login(client, "board1", "pass1234")
    assert client.get(f"/documents/{own.id}/pdf?facsimile=1").data[:4] == b"%PDF"
    assert db.query(AuditLog).filter_by(action="facsimile.pdf").count() == 0


def _photo_with_shadow() -> bytes:
    """«Фото»: желтоватая бумага, свет — градиентом и с тенью в углу; штрих
    тоже затемняется светом (отражение × освещение)."""
    from PIL import ImageChops
    refl = Image.new("RGB", (400, 300), (242, 237, 219))
    ImageDraw.Draw(refl).line([(100, 150), (200, 120), (300, 170)], fill=(30, 30, 70), width=8)
    light = Image.new("L", (400, 300))
    px = light.load()
    for y in range(300):
        for x in range(400):
            px[x, y] = 255 - int(110 * x / 400) - (70 if x + y < 120 else 0)
    # Тень на фото всегда с мягким краем (рука/телефон не у самого листа).
    light = light.filter(ImageFilter.GaussianBlur(4))
    img = ImageChops.multiply(refl, Image.merge("RGB", [light] * 3))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


def test_flatten_removes_shadow_and_gradient():
    data = _photo_with_shadow()
    clean = Image.open(io.BytesIO(facsimile.prepare_image(data)))
    # Остался только штрих: рамка плотно вокруг него (~200×50 + поля), а не во весь снимок.
    assert clean.width < 230 and clean.height < 80
    raw = Image.open(io.BytesIO(facsimile.prepare_image(data, facsimile.Filter(flatten=False, despeckle=False))))
    # Без выравнивания освещения тёмная правая часть бумаги считается «чернилами».
    assert raw.width > 300


def test_filter_color_and_strength():
    data = _scan()
    black = Image.open(io.BytesIO(facsimile.prepare_image(data, facsimile.Filter(color="black"))))
    opaque = [p for p in black.getdata() if p[3] == 255]
    assert opaque and all(p[:3] == (25, 25, 25) for p in opaque)
    # Максимальная очистка светлого штриха — ничего не остаётся.
    light = _scan((200, 205, 230))
    try:
        facsimile.prepare_image(light, facsimile.Filter(strength=100))
    except ValueError:
        pass
    else:
        raise AssertionError("ожидался ValueError")
    assert facsimile.prepare_image(light, facsimile.Filter(strength=0))[:4] == b"\x89PNG"
    flt = facsimile.Filter.from_form({"strength": "abc", "flatten": "0", "color": "purple"})
    assert (flt.strength, flt.flatten, flt.despeckle, flt.color) == (50, False, True, "original")


def test_preview_endpoint(db, client):
    _setup(db)
    data = {"image": (io.BytesIO(_scan()), "seal.jpg"), "strength": "60", "flatten": "1", "color": "blue"}
    login(client, "board1", "pass1234")
    assert client.post("/facsimile/preview", data=data, content_type="multipart/form-data").status_code in (302, 403)
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    data = {"image": (io.BytesIO(_scan()), "seal.jpg"), "strength": "60", "flatten": "1", "color": "blue"}
    resp = client.post("/facsimile/preview", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200 and resp.mimetype == "image/png" and resp.data[:4] == b"\x89PNG"
    assert db.query(Cooperative).one().seal_png is None  # предпросмотр ничего не сохраняет
    resp = client.post("/facsimile/preview", data={"image": (io.BytesIO(_blank()), "x.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 422
    page = client.get("/cooperative/edit").get_data(as_text=True)
    assert "data-fx-form" in page and 'name="strength"' in page

    # Сохранение — с теми же настройками, что в превью.
    client.post("/facsimile/seal", data={"image": (io.BytesIO(_scan()), "seal.jpg"), "color": "black"},
                content_type="multipart/form-data")
    db.expire_all()
    saved = Image.open(io.BytesIO(db.query(Cooperative).one().seal_png))
    assert all(p[:3] == (25, 25, 25) for p in saved.getdata() if p[3] == 255)


def test_layout_controls(db, client):
    chairman, own, _other = _setup(db)
    db.query(Cooperative).one().seal_png = facsimile.prepare_image(_scan())
    db.get(Person, chairman.id).signature_png = facsimile.prepare_image(_scan((10, 10, 10)))
    db.commit()
    login(client, "chair", "pass1234")
    page = client.get("/cooperative/edit").get_data(as_text=True)
    assert "data-fx-layout-form" in page and 'data-fx-range="sign_dy"' in page and 'data-fx-range="seal_size"' in page
    # По умолчанию: низ подписи на 8 мм ниже линии, печать 38 мм.
    page = client.get(f"/documents/{own.id}").get_data(as_text=True)
    assert "bottom: -8.0mm; height: 15.0mm" in page and "width: 38.0mm" in page

    client.post("/facsimile/layout", data={"sign_dx": "2", "sign_dy": "5.5", "sign_h": "99",
                                           "seal_dx": "-10", "seal_dy": "abc", "seal_size": "30"})
    page = client.get(f"/documents/{own.id}").get_data(as_text=True)
    # Значения — в пределах (высота подписи ограничена 35 мм), мусор — по умолчанию.
    assert "left: -2.0mm; right: -6.0mm; bottom: -5.5mm; height: 35.0mm" in page
    assert "width: 30.0mm; height: 30.0mm" in page and "margin: -26.0mm 0 0 -25.0mm" in page
    assert db.query(AuditLog).filter_by(action="facsimile.layout").count() == 0  # оформление — не в аудит
    assert client.get(f"/documents/{own.id}/pdf?facsimile=1").data[:4] == b"%PDF"

    client.post("/facsimile/layout", data={"reset": "1"})
    db.expire_all()
    assert db.get(Person, chairman.id).signature_layout is None and db.query(Cooperative).one().seal_layout is None
    client.get("/auth/logout")

    login(client, "board1", "pass1234")
    client.post("/facsimile/layout", data={"sign_dy": "1"})
    db.expire_all()
    assert db.get(Person, chairman.id).signature_layout is None
