"""
Показания по фото (app/meter_photos.py): фото → гараж по номеру счётчика →
предложенные показания → подтверждение с начислением. Само распознавание
(отдельный процесс app/meter_ocr.py) подменяется готовыми надписями.
"""
import datetime as dt
import io
import os
from decimal import Decimal

import pytest

from app import meter_photos
from app.models import (
    Charge, ElectricityMeter, ElectricityReading, ElectricityTariff, ElectricityTariffKind, FeeType, MeterPhoto,
    RoleEnum,
)
from tests.conftest import login, make_garage, make_ownership, make_person, make_user

TODAY = dt.date.today()


def tok(text, box):
    return {"text": text, "score": 0.99, "box": list(box)}


@pytest.fixture()
def ocr(monkeypatch):
    """Что «увидит» распознавание: присвоить result["tokens"]/["error"]."""
    result = {"tokens": [], "exif_date": None}
    monkeypatch.setattr(meter_photos, "run_ocr", lambda path: dict(result))
    return result


def _meter(db, garage, number, baseline):
    meter = ElectricityMeter(garage_id=garage.id, meter_number=number, initial_reading=Decimal("0"),
                             installed_date=TODAY - dt.timedelta(days=400))
    db.add(meter)
    db.flush()
    db.add(ElectricityReading(meter_id=meter.id, reading=Decimal(baseline), reading_date=TODAY - dt.timedelta(days=30)))
    return meter


def _setup(db):
    owner = make_person(db, "Иванов Иван")
    g1, g2, other = make_garage(db, number="5"), make_garage(db, number="6"), make_garage(db, number="9")
    make_ownership(db, g1, owner)
    make_ownership(db, g2, owner)
    m1 = _meter(db, g1, "41782563", "12300")
    m2 = _meter(db, g2, "55501234", "800")
    _meter(db, other, "77700011", "100")
    db.add(FeeType(code="electricity", name="Электричество"))
    db.add(ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("6.5"), effective_date=dt.date(2020, 1, 1)))
    make_user(db, "owner_mp", "pass12345", person=owner)
    make_user(db, "board_mp", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    return g1, g2, other, m1, m2


def _upload(client, **form):
    data = {"photo": (io.BytesIO(b"\xff\xd8fake"), "meter.jpg"), **form}
    return client.post("/electricity/photo/recognize", data=data, content_type="multipart/form-data")


def test_owner_garage_found_by_meter_number(db, client, ocr, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    g1, g2, *_ = _setup(db)
    ocr["tokens"] = [tok("55501234", (100, 400, 300, 440)), tok("00845.2", (100, 100, 500, 220))]
    login(client, "owner_mp", "pass12345")

    data = _upload(client).get_json()
    assert data["ok"] and data["garage_id"] == g2.id
    assert data["meter_status"] == "matched"
    assert data["suggestion"] == "845.2"
    assert data["baseline"] == "800.00"


def test_confirm_creates_reading_charge_and_links_photo(db, client, ocr, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    g1, _, _, m1, _ = _setup(db)
    ocr["tokens"] = [tok("41782563", (100, 400, 300, 440)), tok("12345.6", (100, 100, 500, 220))]
    login(client, "owner_mp", "pass12345")
    photo_id = _upload(client).get_json()["photo_id"]

    resp = client.post(f"/electricity/photo/{photo_id}/confirm", data={"garage_id": g1.id, "reading": "12345,6"})
    assert resp.get_json()["ok"], resp.get_json()
    reading = db.query(ElectricityReading).filter_by(meter_id=m1.id, reading=Decimal("12345.6")).one()
    assert reading.reading_date == TODAY
    charge = db.query(Charge).filter_by(reading_id=reading.id).one()
    assert charge.amount == Decimal("296.40")  # 45,6 × 6,5
    photo = db.get(MeterPhoto, photo_id)
    assert photo.reading_id == reading.id and photo.suggested_reading == Decimal("12345.6")
    # фото видно в журнале показаний гаража
    page = client.get(f"/garages/{g1.id}?tab=account").get_data(as_text=True)
    assert f"/electricity/photo/{photo_id}/file" in page

    again = client.post(f"/electricity/photo/{photo_id}/confirm", data={"garage_id": g1.id, "reading": "12400"})
    assert again.status_code == 400


def test_reading_below_previous_rejected(db, client, ocr, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    g1, *_ = _setup(db)
    login(client, "owner_mp", "pass12345")
    photo_id = _upload(client, garage_id=g1.id).get_json()["photo_id"]
    resp = client.post(f"/electricity/photo/{photo_id}/confirm", data={"garage_id": g1.id, "reading": "12000"})
    assert resp.status_code == 400 and not resp.get_json()["ok"]


def test_owner_cannot_confirm_for_foreign_garage(db, client, ocr, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    _, _, other, *_ = _setup(db)
    # номер чужого счётчика на фото не узнаётся — его нет среди своих
    ocr["tokens"] = [tok("77700011", (100, 400, 300, 440))]
    login(client, "owner_mp", "pass12345")
    data = _upload(client).get_json()
    assert data["garage_id"] is None
    resp = client.post(f"/electricity/photo/{data['photo_id']}/confirm", data={"garage_id": other.id, "reading": "200"})
    assert resp.status_code == 403


def test_owner_date_is_today_board_can_backdate(db, client, ocr, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    g1, _, _, m1, _ = _setup(db)
    backdate = TODAY - dt.timedelta(days=3)
    ocr["exif_date"] = backdate.isoformat()
    login(client, "board_mp", "pass12345")
    data = _upload(client, garage_id=g1.id).get_json()
    assert data["reading_date"] == backdate.isoformat()
    resp = client.post(f"/electricity/photo/{data['photo_id']}/confirm",
                       data={"garage_id": g1.id, "reading": "12350", "reading_date": backdate.isoformat()})
    assert resp.get_json()["ok"]
    assert db.query(ElectricityReading).filter_by(meter_id=m1.id, reading=Decimal("12350")).one().reading_date == backdate


def test_ocr_failure_still_allows_manual_entry(db, client, ocr, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    g1, *_ = _setup(db)
    ocr.pop("tokens")
    ocr["error"] = "ModuleNotFoundError: rapidocr"
    login(client, "owner_mp", "pass12345")
    data = _upload(client, garage_id=g1.id).get_json()
    assert data["ok"] and data["ocr_error"] and data["suggestion"] is None
    resp = client.post(f"/electricity/photo/{data['photo_id']}/confirm", data={"garage_id": g1.id, "reading": "12400"})
    assert resp.get_json()["ok"]


def test_photo_file_access(db, client, ocr, app, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    g1, *_ = _setup(db)
    stranger = make_person(db, "Посторонний")
    make_user(db, "stranger_mp", "pass12345", person=stranger)
    db.commit()
    login(client, "owner_mp", "pass12345")
    photo_id = _upload(client, garage_id=g1.id).get_json()["photo_id"]
    assert client.get(f"/electricity/photo/{photo_id}/file").status_code == 200
    client.get("/auth/logout")
    login(client, "stranger_mp", "pass12345")
    assert client.get(f"/electricity/photo/{photo_id}/file").status_code == 403
    assert client.post(f"/electricity/photo/{photo_id}/confirm", data={"garage_id": g1.id, "reading": "1"}).status_code == 403


def test_stale_unconfirmed_photos_cleaned(db, client, ocr, app, monkeypatch):
    monkeypatch.setattr(meter_photos, "shrink_image", lambda p: None)
    g1, *_ = _setup(db)
    login(client, "owner_mp", "pass12345")
    old_id = _upload(client, garage_id=g1.id).get_json()["photo_id"]
    old = db.get(MeterPhoto, old_id)
    old_file = os.path.join(app.config["UPLOAD_FOLDER"], old.file_path)
    old.created_at = dt.datetime.now() - dt.timedelta(days=2)
    db.commit()
    _upload(client, garage_id=g1.id)
    db.expire_all()
    assert db.get(MeterPhoto, old_id) is None and not os.path.exists(old_file)


def test_page_lists_only_own_garages(db, client, ocr):
    _setup(db)
    login(client, "owner_mp", "pass12345")
    page = client.get("/electricity/photo/").get_data(as_text=True)
    assert "41782563" in page and "55501234" in page and "77700011" not in page


def test_run_ocr_reports_broken_interpreter(app, tmp_path):
    img = tmp_path / "x.jpg"
    img.write_bytes(b"x")
    app.config["METER_OCR_PYTHON"] = str(tmp_path / "no-such-python")
    with app.app_context():
        assert "error" in meter_photos.run_ocr(str(img))
    app.config["METER_OCR_PYTHON"] = ""
    with app.app_context():
        assert meter_photos.run_ocr(str(img)) == {"error": "OCR disabled"}
