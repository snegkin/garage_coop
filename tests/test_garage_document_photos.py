"""
Документы гаража (технический план, выписка из ЕГРН, межевание и т.п.) —
GarageDocumentPhoto, см. app/garages.py: add_document_photo/
remove_document_photo/document_photo_file. В отличие от GaragePhoto (фото
гаража, только правление), загружать документы может СОБСТВЕННИК гаража
ИЛИ правление (is_owner_or_board) — такие документы обычно на руках
именно у собственника.
"""
import io

from app.models import RoleEnum, GarageDocumentPhoto, GarageDocumentType

from tests.conftest import make_garage, make_person, make_ownership, make_user, login


def test_owner_can_upload_document(app, db, client):
    owner = make_person(db, full_name="Владелец Документович")
    garage = make_garage(db, number="601")
    make_ownership(db, garage, owner)
    make_user(db, "owner500", "pass12345", role=RoleEnum.MEMBER, person=owner)
    db.commit()
    login(client, "owner500", "pass12345")

    resp = client.post(f"/garages/{garage.id}/documents/add", data={
        "doc_type": "technical_plan",
        "file": (io.BytesIO(b"%PDF-1.4 fake"), "plan.pdf"),
        "comment": "от Росреестра",
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    doc = db.query(GarageDocumentPhoto).filter_by(garage_id=garage.id).one()
    assert doc.doc_type == GarageDocumentType.TECHNICAL_PLAN
    assert doc.original_filename == "plan.pdf"
    assert doc.comment == "от Росреестра"
    assert doc.is_image is False


def test_board_can_upload_document(db, client):
    garage = make_garage(db, number="602")
    make_user(db, "board980", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board980", "pass12345")

    resp = client.post(f"/garages/{garage.id}/documents/add", data={
        "doc_type": "usrn_extract",
        "file": (io.BytesIO(b"scan bytes"), "egrn.jpg"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    doc = db.query(GarageDocumentPhoto).filter_by(garage_id=garage.id).one()
    assert doc.doc_type == GarageDocumentType.USRN_EXTRACT
    assert doc.is_image is True


def test_unrelated_member_cannot_upload_document(db, client):
    owner = make_person(db, full_name="Владелец Второй")
    other = make_person(db, full_name="Посторонний Человек")
    garage = make_garage(db, number="603")
    make_ownership(db, garage, owner)
    make_user(db, "member500", "pass12345", role=RoleEnum.MEMBER, person=other)
    db.commit()
    login(client, "member500", "pass12345")

    resp = client.post(f"/garages/{garage.id}/documents/add", data={
        "doc_type": "other",
        "file": (io.BytesIO(b"data"), "file.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 403
    assert db.query(GarageDocumentPhoto).filter_by(garage_id=garage.id).count() == 0


def test_upload_rejects_disallowed_extension(db, client):
    garage = make_garage(db, number="604")
    make_user(db, "board981", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board981", "pass12345")

    resp = client.post(f"/garages/{garage.id}/documents/add", data={
        "doc_type": "other",
        "file": (io.BytesIO(b"MZ..."), "malware.exe"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert db.query(GarageDocumentPhoto).filter_by(garage_id=garage.id).count() == 0


def test_owner_can_remove_own_document(db, client):
    owner = make_person(db, full_name="Владелец Третий")
    garage = make_garage(db, number="605")
    make_ownership(db, garage, owner)
    doc = GarageDocumentPhoto(garage_id=garage.id, doc_type=GarageDocumentType.OTHER,
                              original_filename="doc.pdf", file_path="stored1.pdf")
    db.add(doc)
    make_user(db, "owner501", "pass12345", role=RoleEnum.MEMBER, person=owner)
    db.commit()
    login(client, "owner501", "pass12345")

    resp = client.post(f"/garages/{garage.id}/documents/{doc.id}/remove")
    assert resp.status_code == 302
    assert db.query(GarageDocumentPhoto).filter_by(id=doc.id).count() == 0


def test_unrelated_member_cannot_remove_document(db, client):
    owner = make_person(db, full_name="Владелец Четвёртый")
    other = make_person(db, full_name="Посторонний Второй")
    garage = make_garage(db, number="606")
    make_ownership(db, garage, owner)
    doc = GarageDocumentPhoto(garage_id=garage.id, doc_type=GarageDocumentType.OTHER,
                              original_filename="doc.pdf", file_path="stored2.pdf")
    db.add(doc)
    make_user(db, "member501", "pass12345", role=RoleEnum.MEMBER, person=other)
    db.commit()
    login(client, "member501", "pass12345")

    resp = client.post(f"/garages/{garage.id}/documents/{doc.id}/remove")
    assert resp.status_code == 403
    assert db.query(GarageDocumentPhoto).filter_by(id=doc.id).count() == 1


def test_member_can_download_own_garage_document(app, db, client):
    owner = make_person(db, full_name="Владелец Пятый")
    garage = make_garage(db, number="607")
    make_ownership(db, garage, owner)
    doc = GarageDocumentPhoto(garage_id=garage.id, doc_type=GarageDocumentType.OTHER,
                              original_filename="doc.txt", file_path="stored3.txt")
    db.add(doc)
    make_user(db, "owner502", "pass12345", role=RoleEnum.MEMBER, person=owner)
    db.commit()
    with open(f"{app.config['UPLOAD_FOLDER']}/stored3.txt", "wb") as fh:
        fh.write(b"document contents")
    login(client, "owner502", "pass12345")

    resp = client.get(f"/garages/documents/{doc.id}/doc.txt")
    assert resp.status_code == 200
    assert resp.data == b"document contents"


def test_unrelated_member_cannot_download_garage_document(db, client):
    owner = make_person(db, full_name="Владелец Шестой")
    other = make_person(db, full_name="Посторонний Третий")
    garage = make_garage(db, number="608")
    make_ownership(db, garage, owner)
    doc = GarageDocumentPhoto(garage_id=garage.id, doc_type=GarageDocumentType.OTHER,
                              original_filename="doc.txt", file_path="stored4.txt")
    db.add(doc)
    make_user(db, "member502", "pass12345", role=RoleEnum.MEMBER, person=other)
    db.commit()
    login(client, "member502", "pass12345")

    resp = client.get(f"/garages/documents/{doc.id}/doc.txt")
    assert resp.status_code == 403
