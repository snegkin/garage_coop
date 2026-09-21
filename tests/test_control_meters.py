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
(BOARD-only), аудит и графическую схему (build_scheme_mermaid — тот же
Mermaid-текст, что рендерится на вкладке «Схема» /control-meters/).
"""
import datetime as dt
from decimal import Decimal

from app.control_meters import (
    reconcile_node, reconcile_node_default, root_level_reconciliation,
    garage_supplied_nodes_delta, reconcile_garage_supply, reconcile_garage_supply_default,
    _build_tree, _gateway_garages, _wrap_with_root, build_scheme_mermaid,
)
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


# ---------------------------------------------------------------------------
# Узел, запитанный через абонентский счётчик гаража (parent_garage_id)
# ---------------------------------------------------------------------------

def test_new_and_edit_forms_render(db, client):
    """Smoke-тест на форму create/edit — теперь с radio parent_kind и
    select-ом по гаражам (см. form.html)."""
    _make_board(db)
    node = _make_node(db, "Узел")
    garage = make_garage(db, number="1")
    other_node = _make_node(db, "Другой")
    other_node.parent_garage_id = garage.id
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.get("/control-meters/new")
    assert resp.status_code == 200
    assert "Гараж №1" in resp.get_data(as_text=True)

    resp = client.get(f"/control-meters/{other_node.id}/edit")
    assert resp.status_code == 200
    assert "checked" in resp.get_data(as_text=True)


def test_create_node_with_garage_parent(db, client):
    _make_board(db)
    garage = make_garage(db, number="1")
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.post(
        "/control-meters/new",
        data={"name": "Общее освещение", "parent_kind": "garage", "parent_garage_id": str(garage.id), "comment": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    node = db.query(ControlMeter).filter_by(name="Общее освещение").one()
    assert node.parent_id is None
    assert node.parent_garage_id == garage.id


def test_garage_supplied_nodes_delta_sums_and_flags_partial(db):
    garage = make_garage(db, number="1")
    node_a = _make_node(db, "Освещение А")
    node_a.parent_garage_id = garage.id
    node_b = _make_node(db, "Освещение Б")  # без единого показания — данных нет вовсе
    node_b.parent_garage_id = garage.id
    _add_node_reading(db, node_a, "100", dt.date(2026, 1, 1))
    _add_node_reading(db, node_a, "130", dt.date(2026, 2, 1))  # дельта 30
    db.commit()

    total, is_partial = garage_supplied_nodes_delta(garage, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert total == Decimal("30")  # node_b пропущен, а не считается за 0
    assert is_partial is True


def test_garage_supplied_nodes_delta_empty_when_none_attached(db):
    garage = make_garage(db, number="1")
    db.commit()
    total, is_partial = garage_supplied_nodes_delta(garage, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert total == Decimal("0")
    assert is_partial is False


def test_reconcile_garage_supply_nets_deltas(db):
    garage = make_garage(db, number="1")
    meter = _attach_garage_with_meter(db, garage)
    _add_garage_reading(db, meter, "1000", dt.date(2026, 1, 1))
    _add_garage_reading(db, meter, "1200", dt.date(2026, 2, 1))  # дельта 200 абонента

    node = _make_node(db, "Освещение")
    node.parent_garage_id = garage.id
    _add_node_reading(db, node, "0", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "50", dt.date(2026, 2, 1))  # дельта 50 узла
    db.commit()

    rec = reconcile_garage_supply(garage, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.node_delta == Decimal("200")
    assert rec.sum_children_delta == Decimal("50")
    assert rec.loss == Decimal("150.00")  # чистое потребление абонента
    assert rec.loss_per_consumer is None  # тут нечего делить поровну
    assert rec.consumers[0].share_of_loss is None


def test_control_meter_with_garage_parent_still_reconciles_its_own_children(db):
    """reconcile_node не зависит от того, как узел подключён «наверх» —
    узел с parent_garage_id по-прежнему корректно сверяется со своими
    СОБСТВЕННЫМИ детьми (регрессия на пункт «дерево вычитает корректно»)."""
    garage = make_garage(db, number="1")
    node = _make_node(db, "Освещение")
    node.parent_garage_id = garage.id
    _add_node_reading(db, node, "0", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "100", dt.date(2026, 2, 1))

    sub_garage = make_garage(db, number="2")
    sub_garage.control_meter_id = node.id
    sub_meter = _attach_garage_with_meter(db, sub_garage)
    _add_garage_reading(db, sub_meter, "0", dt.date(2026, 1, 1))
    _add_garage_reading(db, sub_meter, "90", dt.date(2026, 2, 1))
    db.commit()

    rec = reconcile_node(node, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert rec.node_delta == Decimal("100")
    assert rec.sum_children_delta == Decimal("90")
    assert rec.loss == Decimal("10.00")


def test_root_level_reconciliation_excludes_garage_supplied_nodes(db):
    """Узел с parent_garage_id не должен попадать в top-level сверку «на
    вводе» как обычный узел верхнего уровня — физически он подключён не к
    вводу, а к щитку гаража."""
    garage = make_garage(db, number="1")
    node = _make_node(db, "Освещение")
    node.parent_garage_id = garage.id
    db.commit()

    rec = root_level_reconciliation(dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert node not in [c.ref for c in rec.consumers]


def test_build_tree_nests_gateway_garage_and_its_node(db):
    garage = make_garage(db, number="1")
    top = _make_node(db, "Верх")
    garage.control_meter_id = top.id
    node = _make_node(db, "Освещение")
    node.parent_garage_id = garage.id
    db.commit()

    all_nodes = db.query(ControlMeter).all()
    tree = _build_tree(all_nodes, _gateway_garages())

    top_entry = next(e for e in tree if e["kind"] == "node" and e["node"].id == top.id)
    garage_entry = next(e for e in top_entry["children"] if e["kind"] == "garage")
    assert garage_entry["node"].id == garage.id
    assert garage_entry["children"][0]["node"].id == node.id


def test_list_and_detail_pages_render_with_gateway_garage(db, client):
    """Smoke-тест на рендеринг /control-meters/ и /control-meters/<id> —
    дерево теперь смешанное (узлы + гараж-точка подключения), см. _tree.html."""
    _make_board(db)
    top = _make_node(db, "Верх")
    garage = make_garage(db, number="1")
    garage.control_meter_id = top.id
    node = _make_node(db, "Освещение")
    node.parent_garage_id = garage.id
    _add_node_reading(db, node, "0", dt.date(2026, 1, 1))
    _add_node_reading(db, node, "10", dt.date(2026, 2, 1))
    meter = _attach_garage_with_meter(db, garage)
    _add_garage_reading(db, meter, "100", dt.date(2026, 1, 1))
    _add_garage_reading(db, meter, "150", dt.date(2026, 2, 1))
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.get("/control-meters/")
    assert resp.status_code == 200
    assert "Гараж №1" in resp.get_data(as_text=True)

    resp = client.get(f"/control-meters/{node.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Гараж №1" in body
    assert node.name in body


def test_gateway_garages_returns_only_garages_with_supplied_nodes(db):
    garage_with_node = make_garage(db, number="1")
    make_garage(db, number="2")  # обычный гараж, ничего через него не запитано
    node = _make_node(db, "Освещение")
    node.parent_garage_id = garage_with_node.id
    db.commit()

    result = _gateway_garages()
    assert [g.id for g in result] == [garage_with_node.id]


# ---------------------------------------------------------------------------
# Схема (build_scheme_mermaid) — переключатель «Схема» на /control-meters/
# ---------------------------------------------------------------------------

def test_build_scheme_mermaid_includes_root_node_and_edges(app, db):
    top = _make_node(db, "Верх")
    child = _make_node(db, "Низ", parent_id=top.id)
    garage = make_garage(db, number="1")
    garage.control_meter_id = child.id
    db.commit()

    all_nodes = db.query(ControlMeter).all()
    tree = _wrap_with_root(_build_tree(all_nodes, _gateway_garages()))
    with app.test_request_context():
        text = build_scheme_mermaid(tree, reconcile_node_default, reconcile_garage_supply_default)

    assert text.startswith("flowchart TD")
    assert 'cmRoot["Ввод (общий счётчик)"]' in text
    assert f'cmNode{top.id}["Верх"]' in text
    # у "Низ" один подключённый гараж — счётчик в подписи (см. build_scheme_mermaid)
    assert f'cmNode{child.id}["Низ (1)"]' in text
    assert f"cmRoot --> cmNode{top.id}" in text
    assert f"cmNode{top.id} --> cmNode{child.id}" in text
    # обычный подключённый гараж отдельным блоком не рисуется (см. докстринг)
    assert "Гараж №1" not in text


def test_build_scheme_mermaid_escapes_quotes_in_node_name(app, db):
    node = ControlMeter(name='Линия "А"')
    db.add(node)
    db.commit()

    tree = _wrap_with_root(_build_tree([node], []))
    with app.test_request_context():
        text = build_scheme_mermaid(tree, reconcile_node_default, reconcile_garage_supply_default)

    assert "&quot;" in text
    assert '"А"' not in text  # сырая кавычка сломала бы Mermaid-синтаксис блока


def test_build_scheme_mermaid_includes_gateway_garage_block(app, db):
    garage = make_garage(db, number="42")
    node = _make_node(db, "Освещение")
    node.parent_garage_id = garage.id
    db.commit()

    tree = _wrap_with_root(_build_tree([node], _gateway_garages()))
    with app.test_request_context():
        text = build_scheme_mermaid(tree, reconcile_node_default, reconcile_garage_supply_default)

    assert f'cmGw{garage.id}["Гараж №42"]' in text
    assert f"cmGw{garage.id} --> cmNode{node.id}" in text


def test_control_meters_list_page_includes_mermaid_scheme(db, client):
    """Smoke-тест на переключатель «Схема» — контейнер и CDN-скрипт Mermaid
    попадают на страницу /control-meters/."""
    _make_board(db)
    _make_node(db, "Верх")
    db.commit()
    login(client, "board1", "pass1234")

    resp = client.get("/control-meters/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="cmSchemeView"' in body
    assert "flowchart TD" in body
    assert "mermaid" in body.lower()
