"""
Тесты на разделение документов на общедоступные и внутренние
(Document.is_internal, см. app/cooperative.py и app/permissions.is_board()).

Общедоступный документ (is_internal=False) — виден и доступен для скачивания
любому вошедшему члену кооператива, как и раньше. Внутренний
(is_internal=True) — виден и доступен только правлению (роль не ниже BOARD);
рядовой член не должен видеть его ни в списке, ни получить файл напрямую
по id (IDOR), даже зная URL.
"""
import datetime as dt

from app.models import RoleEnum, Document, DocumentType

from tests.conftest import make_person, make_user, login


def _make_document(db, is_internal, title="Документ", doc_type=DocumentType.OTHER):
    doc = Document(
        doc_type=doc_type,
        date=dt.date(2026, 1, 1),
        title=title,
        file_path=None,
        is_internal=is_internal,
    )
    db.add(doc)
    db.flush()
    return doc


def _make_member(db, username="member1"):
    person = make_person(db, full_name="Member One")
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()


def _make_board(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def test_member_does_not_see_internal_document_in_list(app, db, client):
    _make_document(db, is_internal=False, title="Public Doc")
    _make_document(db, is_internal=True, title="Internal Doc")
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Public Doc" in body
    assert "Internal Doc" not in body


def test_board_sees_both_public_and_internal_documents(app, db, client):
    _make_document(db, is_internal=False, title="Public Doc")
    _make_document(db, is_internal=True, title="Internal Doc")
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Public Doc" in body
    assert "Internal Doc" in body


def test_member_cannot_download_internal_document_directly(app, db, client):
    """IDOR-проверка: даже зная id внутреннего документа и прямой URL
    /cooperative/documents/<id>/file, рядовой член не должен получить файл."""
    doc = _make_document(db, is_internal=True, title="Internal Doc")
    doc.file_path = "somefile.pdf"
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get(f"/cooperative/documents/{doc.id}/file")
    assert resp.status_code == 403


def test_member_can_download_public_document(app, db, client, tmp_path):
    upload_dir = app.config["UPLOAD_FOLDER"]
    file_name = "public_report.txt"
    file_path = f"{upload_dir}/{file_name}"
    with open(file_path, "w") as f:
        f.write("hello")

    doc = _make_document(db, is_internal=False, title="Public Doc")
    doc.file_path = file_path
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get(f"/cooperative/documents/{doc.id}/file")
    assert resp.status_code == 200


def test_only_board_can_create_document(client, db):
    _make_member(db)
    login(client, "member1", "pass1234")
    resp = client.post("/cooperative/documents/new", data={
        "doc_type": DocumentType.REPORT.value,
        "date": "2026-01-01",
        "title": "Sneaky doc",
    })
    assert resp.status_code == 302
    assert db.query(Document).filter_by(title="Sneaky doc").first() is None


def test_board_create_document_checkbox_sets_is_internal(app, db, client):
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.post("/cooperative/documents/new", data={
        "doc_type": DocumentType.ESTIMATE.value,
        "date": "2026-01-01",
        "title": "Internal Estimate",
        "is_internal": "on",
    })
    assert resp.status_code == 302
    doc = database_query_first_by_title(db, "Internal Estimate")
    assert doc is not None
    assert doc.is_internal is True


def test_board_create_document_without_checkbox_is_public(app, db, client):
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.post("/cooperative/documents/new", data={
        "doc_type": DocumentType.INVOICE.value,
        "date": "2026-01-01",
        "title": "Public Invoice",
    })
    assert resp.status_code == 302
    doc = database_query_first_by_title(db, "Public Invoice")
    assert doc is not None
    assert doc.is_internal is False


def test_new_document_types_are_valid_enum_values():
    """Новые виды документов из задачи: счета, выписки, справки, смета, отчёт."""
    values = {t.value for t in DocumentType}
    assert {"invoice", "statement", "certificate", "estimate", "report"} <= values


def test_edit_form_does_not_render_none_for_unset_number(app, db, client):
    """Document.number необязателен (None, если не заполнен при создании) —
    форма правки не должна выводить туда буквальный текст "None"
    (голое {{ doc.number }} без or '' в _document_fields.html). Кнопка
    «Изменить»/модалка правки рендерится только для документов с файлом
    (см. cooperative/view.html: {% for d in docs if d.file_path %}) — файл
    в тесте нужен, иначе модалки вообще не будет в ответе."""
    doc = Document(doc_type=DocumentType.OTHER, date=dt.date(2026, 1, 1), title="Без номера", file_path="docs/x.pdf")
    db.add(doc)
    db.flush()
    assert doc.number is None
    db.commit()
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'id="editDocumentModal{doc.id}"' in body
    modal_start = body.index(f'id="editDocumentModal{doc.id}"')
    modal_chunk = body[modal_start:modal_start + 2000]
    assert 'name="number" value="None"' not in modal_chunk
    assert 'name="number" value=""' in modal_chunk


def test_documents_table_access_column_is_sortable_and_searchable(app, db, client):
    """Столбец «Доступ» (внутренний/общедоступный) раньше был no-sort и не
    входил в data-filter-text — не сортировался и не находился поиском,
    тот же класс бага, что уже правили для статуса разнесения в выписке
    (см. bank_statement.html)."""
    doc = _make_document(db, is_internal=True, title="Внутренний документ")
    db.commit()
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Заголовок столбца «Доступ» больше не no-sort — ищем именно
    # <th>Доступ</th>, а не <th class="no-sort">Доступ</th>.
    assert "<th>Доступ</th>" in body
    assert 'class="no-sort">Доступ' not in body

    # Статус доступа попадает в data-filter-text строки документа.
    row_idx = body.index("Внутренний документ")
    row_chunk = body[max(0, row_idx - 500):row_idx]
    filter_attr_start = row_chunk.rindex('data-filter-text="')
    filter_attr = row_chunk[filter_attr_start:]
    assert "внутренний" in filter_attr


def database_query_first_by_title(db, title):
    return db.query(Document).filter_by(title=title).first()
