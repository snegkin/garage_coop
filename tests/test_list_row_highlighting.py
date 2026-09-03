"""
Подсветка строк цветом + легенда над таблицей на /garages/ и /persons/:
- есть комментарий (Garage.comment / Person.comment) -> table-info, у обоих списков;
- у гаража нет счётчика (app/garages.py:list_garages, has_meter) -> table-warning,
  вместо отдельной колонки (см. app/garages.py:_current_meter);
- у человека есть pending-ревизия -> table-warning (уже было, поведение не менялось,
  только объединено с table-info в один class= через row_classes).

Легенда показывается, только если хотя бы одно из условий реально встречается
в списке — иначе не занимает место понапрасну.
"""
from app.models import RoleEnum, ElectricityMeter

from tests.conftest import make_garage, make_person, make_user, login


def test_garage_with_comment_row_is_highlighted_and_legend_shown(app, db, client):
    make_garage(db, number="70", comment="Счётчик сломался, чинят")
    make_user(db, "board70", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board70", "pass12345")

    resp = client.get("/garages/")
    html = resp.get_data(as_text=True)
    assert "Есть комментарий" in html  # легенда
    row = html.split('data-filter-text="70 ')[1].split("</tr>")[0]
    assert "table-info" in row


def test_garage_without_meter_row_is_highlighted_and_legend_shown(app, db, client):
    make_garage(db, number="71")
    make_user(db, "board71", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board71", "pass12345")

    resp = client.get("/garages/")
    html = resp.get_data(as_text=True)
    assert "Нет счётчика электричества" in html  # легенда
    row = html.split('data-filter-text="71 ')[1].split("</tr>")[0]
    assert "table-warning" in row


def test_garage_with_meter_and_no_comment_row_not_highlighted(app, db, client):
    garage = make_garage(db, number="72")
    db.add(ElectricityMeter(garage_id=garage.id, meter_number="99999"))
    make_user(db, "board72", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board72", "pass12345")

    resp = client.get("/garages/")
    html = resp.get_data(as_text=True)
    row = html.split('data-filter-text="72 ')[1].split("</tr>")[0]
    assert "table-info" not in row
    assert "table-warning" not in row


def test_legend_absent_when_no_garage_needs_attention(app, db, client):
    garage = make_garage(db, number="73")
    db.add(ElectricityMeter(garage_id=garage.id, meter_number="88888"))
    make_user(db, "board73", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board73", "pass12345")

    resp = client.get("/garages/")
    html = resp.get_data(as_text=True)
    assert "Нет счётчика электричества" not in html
    assert "Есть комментарий" not in html


def test_person_with_comment_legend_shown(app, db, client):
    make_person(db, full_name="Человек С Комментарием", comment="Обсуждали приватизацию, не уточнили детали")
    make_user(db, "board74", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board74", "pass12345")

    resp = client.get("/persons/")
    html = resp.get_data(as_text=True)
    assert "Есть комментарий" in html


def test_person_with_comment_tr_class_contains_table_info(app, db, client):
    make_person(db, full_name="Отдельный Человек", comment="Заметка")
    make_user(db, "board75", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board75", "pass12345")

    resp = client.get("/persons/")
    html = resp.get_data(as_text=True)
    row_start = html.index("Отдельный Человек")
    tr_start = html.rindex("<tr", 0, row_start)
    tr_tag = html[tr_start:html.index(">", tr_start)]
    assert "table-info" in tr_tag
