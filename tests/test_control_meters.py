"""
Тесты на внутренние контрольные счётчики (app/control_meters.py) —
иерархическое дерево узлов для сверки показаний, без начислений.

Покрывает: дерево (создание/защита от циклов/защита от удаления узла с
детьми или гаражами), привязку гаражей к узлу, историю показаний узла
(дельта на лету, удаление только последней записи), сверку (reconcile_node —
равномерное деление потерь между непосредственными потребителями узла,
исключение потребителей без данных из суммы/деления, дочерний узел
учитывается целиком, отрицательные потери не скрываются),
root_level_reconciliation (сверка с MasterMeterReading), права доступа
(BOARD-only) и аудит.
"""
import datetime as dt
from decimal import Decimal

from app.control_meters import reconcile_node, reconcile_node_default, root_level_reconciliation
from app.models import (
    RoleEnum, ControlMeter, ControlMeterReading, ElectricityMeter, ElectricityReading,
    ElectricityTariff, MasterMeterReading, AuditLog, Charge, Expense,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_board(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def _make_member(db, username="member1"):
    person = make_person(db, full_name="Member One")
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()


def _make_node(db, name="Узел", parent_id=None):
    node = ControlMeter(name=name, parent_id=parent_id)
    db.add(node)
    db.flush()
    return node


def _add_node_reading(db, node, reading, reading_date):
    r = ControlMeterReading(control_meter_id=node.id, reading=Decimal(reading), reading_date=reading_date)
    db.add(r)
    db.flush()
    return r


def _attach_garage_with_meter(db, garage, initial_reading=None, installed_date=None):
    meter = ElectricityMeter(garage_id=garage.id, meter_number=f"m-{garage.id}", initial_reading=initial_reading, installed_date=installed_date)
    db.add(meter)
    db.flush()
    return meter


def _add_garage_reading(db, meter, reading, reading_date):
    r = ElectricityReading(meter_id=meter.id, reading=Decimal(reading), reading_date=reading_date)
    db.add(r)
    db.flush()
    return r


# ---------------------------------------------------------------------------
# Дерево
# ---------------------------------------------------------------------------

def test_create_top_level_and_nested_node(db, client):
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.post("/control-meters/new", data={"name": "ВРУ-1", "parent_id": "", "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200
    top = db.query(ControlMeter).filter_by(name="ВРУ-1").one()
    assert top.parent_id is None

    resp = client.post("/control-meters/new", data={"name": "Щит ряда Б", "parent_id": str(top.id), "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200
    child = db.query(ControlMeter).filter_by(name="Щит ряда Б").one()
    assert child.parent_id == top.id


def test_cannot_set_self_or_descendant_as_parent(db, client):
    _make_board(db)
    top = _make_node(db, "Верх")
    child = _make_node(db, "Низ", parent_id=top.id)
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{top.id}/edit", data={"name": "Верх", "parent_id": str(child.id), "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200
    db.refresh(top)
    assert top.parent_id is None
    assert "родителем" in resp.get_data(as_text=True)


def test_cannot_delete_node_with_children(db, client):
    _make_board(db)
    top = _make_node(db, "Верх")
    _make_node(db, "Низ", parent_id=top.id)
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{top.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get(ControlMeter, top.id) is not None
    assert "дочерние узлы" in resp.get_data(as_text=True)


def test_cannot_delete_node_with_attached_garages(db, client):
    _make_board(db)
    node = _make_node(db, "Узел")
    garage = make_garage(db, number="1")
    garage.control_meter_id = node.id
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{node.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get(ControlMeter, node.id) is not None
    assert "гараж" in resp.get_data(as_text=True).lower()


def test_delete_empty_node_succeeds(db, client):
    _make_board(db)
    node = _make_node(db, "Узел")
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{node.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get(ControlMeter, node.id) is None


# ---------------------------------------------------------------------------
# Привязка гаражей
# ---------------------------------------------------------------------------

def test_garage_control_meter_id_is_null_by_default(db):
    garage = make_garage(db, number="1")
    db.commit()
    assert garage.control_meter_id is None


def test_attach_and_detach_garage(db, client):
    _make_board(db)
    node = _make_node(db, "Узел")
    garage = make_garage(db, number="1")
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{node.id}/garages/attach", data={"garage_id": str(garage.id)}, follow_redirects=True)
    assert resp.status_code == 200
    db.refresh(garage)
    assert garage.control_meter_id == node.id

    resp = client.post(f"/control-meters/{node.id}/garages/{garage.id}/detach", follow_redirects=True)
    assert resp.status_code == 200
    db.refresh(garage)
    assert garage.control_meter_id is None


def test_reattach_garage_between_nodes(db, client):
    _make_board(db)
    node_a = _make_node(db, "Узел А")
    node_b = _make_node(db, "Узел Б")
    garage = make_garage(db, number="1")
    garage.control_meter_id = node_a.id
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{node_b.id}/garages/attach", data={"garage_id": str(garage.id)}, follow_redirects=True)
    assert resp.status_code == 200
    db.refresh(garage)
    assert garage.control_meter_id == node_b.id


# ---------------------------------------------------------------------------
# Показания узла
# ---------------------------------------------------------------------------

def test_reading_delta_computed_on_the_fly(db):
    node = _make_node(db)
    _add_node_reading(db, node, "1000", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "1150", dt.date(2026, 2, 1))
    db.commit()

    rec = reconcile_node(node, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.node_delta == Decimal("150")


def test_cannot_add_reading_less_than_previous(db, client):
    _make_board(db)
    node = _make_node(db)
    _add_node_reading(db, node, "1000", dt.date(2026, 1, 1))
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{node.id}/readings/add", data={"reading": "900", "reading_date": "2026-02-01", "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200
    assert db.query(ControlMeterReading).filter_by(control_meter_id=node.id).count() == 1


def test_can_delete_only_last_reading(db, client):
    _make_board(db)
    node = _make_node(db)
    r1 = _add_node_reading(db, node, "1000", dt.date(2026, 1, 1))
    r2 = _add_node_reading(db, node, "1150", dt.date(2026, 2, 1))
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(f"/control-meters/{node.id}/readings/{r1.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get(ControlMeterReading, r1.id) is not None
    assert "последнее" in resp.get_data(as_text=True)

    resp = client.post(f"/control-meters/{node.id}/readings/{r2.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get(ControlMeterReading, r2.id) is None


def test_add_reading_does_not_create_charge_or_expense(db, client):
    """Регрессия: контрольные счётчики — чисто диагностика, без денег."""
    _make_board(db)
    node = _make_node(db)
    db.commit()
    login(client, "board1", "pass1234")

    charges_before = db.query(Charge).count()
    expenses_before = db.query(Expense).count()
    resp = client.post(f"/control-meters/{node.id}/readings/add", data={"reading": "500", "reading_date": "2026-01-01", "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200
    assert db.query(Charge).count() == charges_before
    assert db.query(Expense).count() == expenses_before


# ---------------------------------------------------------------------------
# Сверка
# ---------------------------------------------------------------------------

def test_reconcile_zero_loss(db):
    node = _make_node(db)
    _add_node_reading(db, node, "1000", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "1100", dt.date(2026, 2, 1))

    g1 = make_garage(db, number="1")
    g2 = make_garage(db, number="2")
    g1.control_meter_id = node.id
    g2.control_meter_id = node.id
    m1 = _attach_garage_with_meter(db, g1)
    m2 = _attach_garage_with_meter(db, g2)
    _add_garage_reading(db, m1, "50", dt.date(2026, 1, 1))
    _add_garage_reading(db, m1, "100", dt.date(2026, 2, 1))
    _add_garage_reading(db, m2, "10", dt.date(2026, 1, 1))
    _add_garage_reading(db, m2, "60", dt.date(2026, 2, 1))
    db.commit()

    rec = reconcile_node(node, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.node_delta == Decimal("100")
    assert rec.sum_children_delta == Decimal("100")  # (100-50) + (60-10)
    assert rec.loss == Decimal("0.00")
    assert rec.is_partial is False
    assert rec.is_negative is False


def test_reconcile_loss_split_evenly_among_consumers_with_data(db):
    node = _make_node(db)
    _add_node_reading(db, node, "1000", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "1130", dt.date(2026, 2, 1))  # дельта 130

    g1 = make_garage(db, number="1")
    g2 = make_garage(db, number="2")
    g1.control_meter_id = node.id
    g2.control_meter_id = node.id
    m1 = _attach_garage_with_meter(db, g1)
    m2 = _attach_garage_with_meter(db, g2)
    _add_garage_reading(db, m1, "50", dt.date(2026, 1, 1))
    _add_garage_reading(db, m1, "100", dt.date(2026, 2, 1))  # дельта 50
    _add_garage_reading(db, m2, "10", dt.date(2026, 1, 1))
    _add_garage_reading(db, m2, "60", dt.date(2026, 2, 1))  # дельта 50
    db.commit()

    rec = reconcile_node(node, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.sum_children_delta == Decimal("100")
    assert rec.loss == Decimal("30.00")
    assert rec.loss_per_consumer == Decimal("15.00")
    for c in rec.consumers:
        assert c.share_of_loss == Decimal("15.00")


def test_reconcile_excludes_consumer_without_data_not_as_zero(db):
    node = _make_node(db)
    _add_node_reading(db, node, "1000", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "1100", dt.date(2026, 2, 1))

    g1 = make_garage(db, number="1")
    g2 = make_garage(db, number="2")  # без счётчика вообще
    g1.control_meter_id = node.id
    g2.control_meter_id = node.id
    m1 = _attach_garage_with_meter(db, g1)
    _add_garage_reading(db, m1, "50", dt.date(2026, 1, 1))
    _add_garage_reading(db, m1, "90", dt.date(2026, 2, 1))  # дельта 40
    db.commit()

    rec = reconcile_node(node, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.consumers_total == 2
    assert rec.consumers_with_data == 1
    assert rec.is_partial is True
    # sum считается только по g1 (40), а не (40 + 0)
    assert rec.sum_children_delta == Decimal("40")
    assert rec.loss == Decimal("60.00")
    # делится только на 1 потребителя с данными, а не на 2
    assert rec.loss_per_consumer == Decimal("60.00")
    g2_entry = next(c for c in rec.consumers if c.ref is g2)
    assert g2_entry.delta is None
    assert g2_entry.share_of_loss is None


def test_reconcile_child_node_counted_as_whole_not_expanded(db):
    parent = _make_node(db, "Родитель")
    child = _make_node(db, "Ребёнок", parent_id=parent.id)
    _add_node_reading(db, parent, "1000", dt.date(2026, 1, 1))
    _add_node_reading(db, parent, "1200", dt.date(2026, 2, 1))  # дельта 200
    _add_node_reading(db, child, "500", dt.date(2026, 1, 1))
    _add_node_reading(db, child, "670", dt.date(2026, 2, 1))  # дельта 170

    # у "ребёнка" внутри свой гараж — не должен участвовать в сверке родителя напрямую
    g_inside_child = make_garage(db, number="1")
    g_inside_child.control_meter_id = child.id
    m = _attach_garage_with_meter(db, g_inside_child)
    _add_garage_reading(db, m, "0", dt.date(2026, 1, 1))
    _add_garage_reading(db, m, "999", dt.date(2026, 2, 1))  # огромная дельта — не должна попасть в сверку родителя
    db.commit()

    rec = reconcile_node(parent, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.consumers_total == 1  # только child, гараж внутри не разворачивается
    assert rec.sum_children_delta == Decimal("170")
    assert rec.loss == Decimal("30.00")


def test_reconcile_negative_loss_not_hidden(db):
    node = _make_node(db)
    _add_node_reading(db, node, "1000", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "1050", dt.date(2026, 2, 1))  # дельта 50

    g1 = make_garage(db, number="1")
    g1.control_meter_id = node.id
    m1 = _attach_garage_with_meter(db, g1)
    _add_garage_reading(db, m1, "0", dt.date(2026, 1, 1))
    _add_garage_reading(db, m1, "100", dt.date(2026, 2, 1))  # дельта 100 > 50 родителя
    db.commit()

    rec = reconcile_node(node, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.loss == Decimal("-50.00")
    assert rec.is_negative is True


def test_reconcile_node_default_needs_two_readings(db):
    node = _make_node(db)
    db.commit()
    assert reconcile_node_default(node) is None

    _add_node_reading(db, node, "100", dt.date(2026, 1, 1))
    db.commit()
    assert reconcile_node_default(node) is None

    _add_node_reading(db, node, "150", dt.date(2026, 2, 1))
    db.commit()
    rec = reconcile_node_default(node)
    assert rec is not None
    assert rec.node_delta == Decimal("50")


# ---------------------------------------------------------------------------
# Сверка на вводе (root_level_reconciliation)
# ---------------------------------------------------------------------------

def test_root_level_reconciliation_uses_master_meter_reading(db):
    tariff = ElectricityTariff(rate=Decimal("5"), effective_date=dt.date(2025, 1, 1))
    db.add(tariff)
    db.flush()
    db.add(MasterMeterReading(year=2026, month=1, reading_date=dt.date(2026, 1, 1), reading=Decimal("10000"), tariff_id=tariff.id))
    db.add(MasterMeterReading(year=2026, month=2, reading_date=dt.date(2026, 2, 1), reading=Decimal("10300"), tariff_id=tariff.id))
    db.flush()

    top = _make_node(db, "Верх")
    g_direct = make_garage(db, number="1")  # без узла — напрямую на вводе
    m = _attach_garage_with_meter(db, g_direct)
    _add_garage_reading(db, m, "0", dt.date(2026, 1, 1))
    _add_garage_reading(db, m, "300", dt.date(2026, 2, 1))
    db.commit()

    rec = root_level_reconciliation(dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.node is None
    assert rec.node_delta == Decimal("300")
    assert rec.consumers_total == 2  # top (без показаний) + g_direct
    assert rec.sum_children_delta == Decimal("300")  # top исключён (нет данных), учтён только g_direct
    assert rec.loss == Decimal("0.00")


def test_root_level_reconciliation_without_master_reading_does_not_crash(db):
    rec = root_level_reconciliation(dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.node_delta is None
    assert rec.loss is None


# ---------------------------------------------------------------------------
# Права доступа
# ---------------------------------------------------------------------------

def test_member_gets_redirected_away_from_all_routes(db, client):
    """roles_required (в отличие от голых abort(403) в is_board()-проверках)
    редиректит с флеш-сообщением на дашборд, а не отдаёт 403 — см.
    app/auth.py:roles_required."""
    _make_member(db)
    node = _make_node(db)
    db.commit()
    login(client, "member1", "pass1234")

    for resp in (
        client.get("/control-meters/", follow_redirects=True),
        client.get("/control-meters/new", follow_redirects=True),
        client.get(f"/control-meters/{node.id}", follow_redirects=True),
        client.post(f"/control-meters/{node.id}/readings/add", data={"reading": "1", "reading_date": "2026-01-01"}, follow_redirects=True),
        client.post(f"/control-meters/{node.id}/garages/attach", data={"garage_id": "1"}, follow_redirects=True),
    ):
        assert resp.status_code == 200
        assert "Недостаточно прав" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Аудит
# ---------------------------------------------------------------------------

def test_create_delete_node_writes_audit_log(db, client):
    _make_board(db)
    login(client, "board1", "pass1234")

    client.post("/control-meters/new", data={"name": "Узел X", "parent_id": "", "comment": ""}, follow_redirects=True)
    node = db.query(ControlMeter).filter_by(name="Узел X").one()
    assert db.query(AuditLog).filter_by(action="control_meter.create").count() == 1

    client.post(f"/control-meters/{node.id}/delete", follow_redirects=True)
    assert db.query(AuditLog).filter_by(action="control_meter.delete").count() == 1


def test_add_reading_and_attach_detach_write_audit_log(db, client):
    _make_board(db)
    node = _make_node(db)
    garage = make_garage(db, number="1")
    db.commit()
    login(client, "board1", "pass1234")

    client.post(f"/control-meters/{node.id}/readings/add", data={"reading": "10", "reading_date": "2026-01-01", "comment": ""}, follow_redirects=True)
    assert db.query(AuditLog).filter_by(action="control_meter.reading_add").count() == 1

    client.post(f"/control-meters/{node.id}/garages/attach", data={"garage_id": str(garage.id)}, follow_redirects=True)
    assert db.query(AuditLog).filter_by(action="garage.control_meter_attach").count() == 1

    client.post(f"/control-meters/{node.id}/garages/{garage.id}/detach", follow_redirects=True)
    assert db.query(AuditLog).filter_by(action="garage.control_meter_detach").count() == 1
