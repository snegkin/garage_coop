"""
Внутренние контрольные счётчики кооператива — иерархическое дерево узлов
(ControlMeter.parent_id, self-referencing FK, по образцу WikiPage.parent_id
из wiki.py) для СВЕРКИ показаний, а не начислений. К каждому узлу подключена
ветвь абонентов: часть гаражей (Garage.control_meter_id) и/или дочерние узлы
более низкого уровня.

Смысл сверки: дельта показаний узла за период должна примерно совпадать с
суммой дельт его НЕПОСРЕДСТВЕННЫХ потребителей (прямых гаражей и/или прямых
дочерних узлов, каждый учитывается как единое целое — не разворачивается
глубже) — расхождение это потери в проводке/изоляции конкретно этого
сегмента, разносятся поровну между потребителями узла, у которых есть данные
за период (см. reconcile_node). Контрольные счётчики НЕ формируют Charge/
Expense — только сырые показания в кВт·ч.

parent_id IS NULL — узел верхнего уровня, физически подключён к вводу
(общему счётчику, см. app/power.py и MasterMeterReading). Это не объединяется
с MasterMeterReading в БД — сверка "ввод vs верхние узлы + гаражи без узла"
считается отдельно, на чтение, см. reconcile_node(node=None, ...) /
root_level_reconciliation().

Раздел целиком доступен только правлению (RoleEnum.BOARD) — внутренний
технический учёт, по аналогии с /power/, а не с garages.py электричеством
по гаражу, где показания вносит и владелец.
"""
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from sqlalchemy import or_

from . import database
from . import audit
from .i18n import translate as _, parse_decimal
from .auth import roles_required
from .models import ControlMeter, ControlMeterReading, ElectricityReading, Garage, MasterMeterReading, RoleEnum
from .garages import _current_meter

bp = Blueprint("control_meters", __name__, url_prefix="/control-meters")


# ---------------------------------------------------------------------------
# Дерево узлов — по образцу wiki.py (_build_visible_tree/_descendant_ids/_parent_options),
# без логики видимости: весь раздел и так BOARD-only.
# ---------------------------------------------------------------------------

def _build_tree(all_nodes):
    """Строит дерево {"node": ControlMeter, "children": [...]} из плоского
    списка, отсортировано по имени на каждом уровне."""
    by_id = {n.id: {"node": n, "children": []} for n in all_nodes}
    roots = []
    for n in all_nodes:
        entry = by_id[n.id]
        if n.parent_id is not None and n.parent_id in by_id:
            by_id[n.parent_id]["children"].append(entry)
        else:
            roots.append(entry)

    def sort_rec(items):
        items.sort(key=lambda e: e["node"].name.lower())
        for it in items:
            sort_rec(it["children"])

    sort_rec(roots)
    return roots


def _descendant_ids(node):
    """id узла и всех его потомков — чтобы при выборе родителя в форме
    редактирования нельзя было выбрать сам узел или его же потомка."""
    ids = {node.id}
    stack = list(node.children)
    while stack:
        n = stack.pop()
        ids.add(n.id)
        stack.extend(n.children)
    return ids


def _parent_options(all_nodes, exclude_ids=frozenset()):
    """Список (node, depth) для <select> «Родительский узел» — с отступом
    по глубине, в том же порядке, что и дерево."""
    tree = _build_tree(all_nodes)
    options = []

    def walk(nodes, depth):
        for entry in nodes:
            if entry["node"].id not in exclude_ids:
                options.append((entry["node"], depth))
            walk(entry["children"], depth + 1)

    walk(tree, 0)
    return options


# ---------------------------------------------------------------------------
# Показания — без денег, дельта считается на лету (аналог
# power._readings_with_amounts, но без тарифа).
# ---------------------------------------------------------------------------

def _readings_with_deltas(readings_desc):
    """readings_desc — список ControlMeterReading по (reading_date, id) убыв.
    Возвращает [(reading, delta_к_предыдущей_по_времени_записи)], None для
    самой первой хронологически записи."""
    chronological = list(reversed(readings_desc))
    result = []
    previous = None
    for r in chronological:
        delta = None if previous is None else (r.reading - previous.reading)
        result.append((r, delta))
        previous = r
    result.reverse()
    return result


# ---------------------------------------------------------------------------
# Сверка
# ---------------------------------------------------------------------------

def _delta(start, end):
    if start is None or end is None:
        return None
    return end - start


def _node_reading_as_of(node: ControlMeter, as_of: dt.date):
    r = (
        database.db_session.query(ControlMeterReading)
        .filter(ControlMeterReading.control_meter_id == node.id, ControlMeterReading.reading_date <= as_of)
        .order_by(ControlMeterReading.reading_date.desc(), ControlMeterReading.id.desc())
        .first()
    )
    return r.reading if r is not None else None


def _master_reading_as_of(as_of: dt.date):
    r = (
        database.db_session.query(MasterMeterReading)
        .filter(MasterMeterReading.reading_date <= as_of)
        .order_by(MasterMeterReading.reading_date.desc(), MasterMeterReading.id.desc())
        .first()
    )
    return r.reading if r is not None else None


def _garage_reading_as_of(meter, as_of: dt.date):
    r = (
        database.db_session.query(ElectricityReading)
        .filter(ElectricityReading.meter_id == meter.id, ElectricityReading.reading_date <= as_of)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    if r is not None:
        return r.reading
    if meter.initial_reading is not None and meter.installed_date is not None and meter.installed_date <= as_of:
        return meter.initial_reading
    return None


def _garage_delta(garage: Garage, date_from: dt.date, date_to: dt.date):
    """Дельта показаний текущего счётчика гаража за интервал. None, если для
    одной из границ данных нет вовсе (не 0 — гараж просто исключается из
    сверки, не считается «не потребил ничего»)."""
    meter = _current_meter(garage)
    if meter is None:
        return None
    return _delta(_garage_reading_as_of(meter, date_from), _garage_reading_as_of(meter, date_to))


@dataclass
class ConsumerReconciliation:
    kind: str  # "garage" | "node"
    ref: object  # Garage | ControlMeter
    delta: Decimal | None
    share_of_loss: Decimal | None = None


@dataclass
class NodeReconciliation:
    node: ControlMeter | None  # None = виртуальный корень (ввод, см. MasterMeterReading)
    date_from: dt.date
    date_to: dt.date
    node_delta: Decimal | None
    consumers: list = field(default_factory=list)
    consumers_with_data: int = 0
    consumers_total: int = 0
    sum_children_delta: Decimal | None = None
    loss: Decimal | None = None            # node_delta - sum_children_delta
    loss_per_consumer: Decimal | None = None  # loss / consumers_with_data, поровну
    is_partial: bool = False               # хоть один потребитель без данных (или их вовсе нет)
    is_negative: bool = False              # loss < 0 — аномалия, не скрываем


def reconcile_node(node: ControlMeter | None, date_from: dt.date, date_to: dt.date) -> NodeReconciliation:
    """Сердце сверки. node=None — виртуальный корень (ввод): node_delta берём
    из MasterMeterReading, потребители — узлы верхнего уровня (parent_id IS
    NULL) и гаражи без узла (control_meter_id IS NULL)."""
    if node is None:
        node_delta = _delta(_master_reading_as_of(date_from), _master_reading_as_of(date_to))
        child_nodes = (
            database.db_session.query(ControlMeter)
            .filter(ControlMeter.parent_id.is_(None)).order_by(ControlMeter.name).all()
        )
        child_garages = (
            database.db_session.query(Garage)
            .filter(Garage.control_meter_id.is_(None)).order_by(Garage.number).all()
        )
    else:
        node_delta = _delta(_node_reading_as_of(node, date_from), _node_reading_as_of(node, date_to))
        child_nodes = node.children
        child_garages = node.garages

    consumers = [
        ConsumerReconciliation(
            kind="node", ref=child,
            delta=_delta(_node_reading_as_of(child, date_from), _node_reading_as_of(child, date_to)),
        )
        for child in child_nodes
    ] + [
        ConsumerReconciliation(kind="garage", ref=g, delta=_garage_delta(g, date_from, date_to))
        for g in child_garages
    ]

    consumers_total = len(consumers)
    with_data = [c for c in consumers if c.delta is not None]
    consumers_with_data = len(with_data)

    sum_children_delta = sum((c.delta for c in with_data), Decimal("0")) if consumers_with_data else None

    if node_delta is None or sum_children_delta is None:
        loss = None
        loss_per_consumer = None
    else:
        loss = (node_delta - sum_children_delta).quantize(Decimal("0.01"))
        loss_per_consumer = (loss / consumers_with_data).quantize(Decimal("0.01"))
        for c in with_data:
            c.share_of_loss = loss_per_consumer

    is_partial = node_delta is None or consumers_with_data < consumers_total
    is_negative = loss is not None and loss < 0

    return NodeReconciliation(
        node=node, date_from=date_from, date_to=date_to, node_delta=node_delta,
        consumers=consumers, consumers_with_data=consumers_with_data, consumers_total=consumers_total,
        sum_children_delta=sum_children_delta, loss=loss, loss_per_consumer=loss_per_consumer,
        is_partial=is_partial, is_negative=is_negative,
    )


def _last_two_dates(ordered_query):
    rows = ordered_query.limit(2).all()
    if len(rows) < 2:
        return None
    return rows[1].reading_date, rows[0].reading_date  # (старая, новая)


def reconcile_node_default(node: ControlMeter | None) -> NodeReconciliation | None:
    """Сверка по двум последним показаниям узла (или MasterMeterReading для
    корня). None, если показаний меньше двух."""
    if node is None:
        q = database.db_session.query(MasterMeterReading).order_by(
            MasterMeterReading.reading_date.desc(), MasterMeterReading.id.desc()
        )
    else:
        q = database.db_session.query(ControlMeterReading).filter_by(control_meter_id=node.id).order_by(
            ControlMeterReading.reading_date.desc(), ControlMeterReading.id.desc()
        )
    dates = _last_two_dates(q)
    if dates is None:
        return None
    date_from, date_to = dates
    return reconcile_node(node, date_from, date_to)


def root_level_reconciliation(date_from: dt.date, date_to: dt.date) -> NodeReconciliation:
    """Сверка «на вводе»: MasterMeterReading vs верхние узлы + гаражи без узла."""
    return reconcile_node(None, date_from, date_to)


# ---------------------------------------------------------------------------
# CRUD дерева узлов
# ---------------------------------------------------------------------------

@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_tree():
    all_nodes = database.db_session.query(ControlMeter).all()
    tree = _build_tree(all_nodes)
    root_reconciliation = reconcile_node_default(None)
    return render_template(
        "control_meters/list.html", tree=tree,
        root_reconciliation=root_reconciliation, reconcile_default=reconcile_node_default,
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        parent_id = int(f["parent_id"]) if f.get("parent_id") else None
        node = ControlMeter(name=f["name"], parent_id=parent_id, comment=f.get("comment") or None)
        database.db_session.add(node)
        database.db_session.flush()
        audit.record(
            "control_meter.create", f"Создан узел контрольного счётчика «{node.name}»",
            entity_type="control_meter", entity_id=node.id,
        )
        database.db_session.commit()
        flash(_("Узел добавлен."), "success")
        return redirect(url_for("control_meters.detail", node_id=node.id))

    all_nodes = database.db_session.query(ControlMeter).all()
    preselected_parent_id = request.args.get("parent_id", type=int)
    return render_template(
        "control_meters/form.html", node=None, parent_options=_parent_options(all_nodes),
        preselected_parent_id=preselected_parent_id,
    )


@bp.route("/<int:node_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(node_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)

    if request.method == "POST":
        f = request.form
        parent_id = int(f["parent_id"]) if f.get("parent_id") else None
        if parent_id is not None and parent_id in _descendant_ids(node):
            flash(_("Нельзя сделать родителем сам узел или его же потомка."), "danger")
            all_nodes = database.db_session.query(ControlMeter).all()
            return render_template(
                "control_meters/form.html", node=node,
                parent_options=_parent_options(all_nodes, exclude_ids=_descendant_ids(node)),
                preselected_parent_id=None,
            )

        node.name = f["name"]
        node.parent_id = parent_id
        node.comment = f.get("comment") or None
        audit.record(
            "control_meter.edit", f"Изменён узел контрольного счётчика «{node.name}»",
            entity_type="control_meter", entity_id=node.id,
        )
        database.db_session.commit()
        flash(_("Узел изменён."), "success")
        return redirect(url_for("control_meters.detail", node_id=node.id))

    all_nodes = database.db_session.query(ControlMeter).all()
    return render_template(
        "control_meters/form.html", node=node,
        parent_options=_parent_options(all_nodes, exclude_ids=_descendant_ids(node)),
        preselected_parent_id=None,
    )


@bp.route("/<int:node_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(node_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)
    if node.children:
        flash(_("Нельзя удалить узел — сначала перенесите или удалите его дочерние узлы."), "danger")
        return redirect(url_for("control_meters.detail", node_id=node.id))
    if node.garages:
        flash(_("Нельзя удалить узел — сначала отвяжите подключённые к нему гаражи."), "danger")
        return redirect(url_for("control_meters.detail", node_id=node.id))

    name = node.name
    audit.record("control_meter.delete", f"Удалён узел контрольного счётчика «{name}»")
    database.db_session.delete(node)
    database.db_session.commit()
    flash(_("Узел удалён."), "success")
    return redirect(url_for("control_meters.list_tree"))


@bp.route("/<int:node_id>")
@roles_required(RoleEnum.BOARD)
def detail(node_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)

    readings_desc = (
        database.db_session.query(ControlMeterReading)
        .filter_by(control_meter_id=node.id)
        .order_by(ControlMeterReading.reading_date.desc(), ControlMeterReading.id.desc())
        .all()
    )
    readings_with_deltas = _readings_with_deltas(readings_desc)

    from_id = request.args.get("from_reading_id", type=int)
    to_id = request.args.get("to_reading_id", type=int)
    reconciliation = None
    if from_id and to_id:
        from_r = database.db_session.get(ControlMeterReading, from_id)
        to_r = database.db_session.get(ControlMeterReading, to_id)
        if from_r and to_r and from_r.control_meter_id == node.id and to_r.control_meter_id == node.id:
            date_from, date_to = sorted([from_r.reading_date, to_r.reading_date])
            reconciliation = reconcile_node(node, date_from, date_to)
    if reconciliation is None:
        reconciliation = reconcile_node_default(node)

    attachable_garages = (
        database.db_session.query(Garage)
        .filter(or_(Garage.control_meter_id.is_(None), Garage.control_meter_id != node.id))
        .order_by(Garage.number)
        .all()
    )

    ancestor_ids = set()
    breadcrumbs = []
    cur = node.parent
    while cur is not None:
        ancestor_ids.add(cur.id)
        breadcrumbs.append(cur)
        cur = cur.parent
    breadcrumbs.reverse()

    all_nodes = database.db_session.query(ControlMeter).all()
    tree = _build_tree(all_nodes)

    return render_template(
        "control_meters/detail.html", node=node,
        readings_desc=readings_desc, readings_with_deltas=readings_with_deltas,
        reconciliation=reconciliation, attachable_garages=attachable_garages,
        ancestor_ids=ancestor_ids, breadcrumbs=breadcrumbs,
        tree=tree, reconcile_default=reconcile_node_default, today=dt.date.today(),
    )


# ---------------------------------------------------------------------------
# Показания узла
# ---------------------------------------------------------------------------

@bp.route("/<int:node_id>/readings/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_reading(node_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)

    f = request.form
    reading_date = dt.date.fromisoformat(f["reading_date"])
    reading_value = parse_decimal(f["reading"])

    previous = (
        database.db_session.query(ControlMeterReading)
        .filter_by(control_meter_id=node.id)
        .order_by(ControlMeterReading.reading_date.desc(), ControlMeterReading.id.desc())
        .first()
    )
    if previous is not None and reading_value < previous.reading:
        flash(_(
            "Показания не могут быть меньше предыдущих ({baseline}).",
            baseline=str(previous.reading.quantize(Decimal("0.01"))),
        ), "danger")
        return redirect(url_for("control_meters.detail", node_id=node.id))

    database.db_session.add(ControlMeterReading(
        control_meter_id=node.id, reading=reading_value, reading_date=reading_date,
        comment=f.get("comment") or None,
    ))
    audit.record(
        "control_meter.reading_add", f"Внесены показания узла «{node.name}»: {reading_value}",
        entity_type="control_meter", entity_id=node.id,
    )
    database.db_session.commit()
    flash(_("Показания внесены."), "success")
    return redirect(url_for("control_meters.detail", node_id=node.id))


@bp.route("/<int:node_id>/readings/<int:reading_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_reading(node_id, reading_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)
    reading = database.db_session.get(ControlMeterReading, reading_id)
    if reading is None or reading.control_meter_id != node_id:
        abort(404)

    # Показания образуют цепочку: дельта каждой зависит от предыдущей по
    # времени. Удаление из середины задним числом исказило бы уже
    # отображённые дельты последующих записей — разрешаем удалять только
    # самое последнее (по reading_date) показание (тот же принцип, что
    # power.delete_reading).
    latest = (
        database.db_session.query(ControlMeterReading)
        .filter_by(control_meter_id=node.id)
        .order_by(ControlMeterReading.reading_date.desc(), ControlMeterReading.id.desc())
        .first()
    )
    if latest is None or latest.id != reading.id:
        flash(_(
            "Можно удалить только самое последнее показание — иначе исказятся "
            "дельты уже сохранённых последующих записей."
        ), "danger")
        return redirect(url_for("control_meters.detail", node_id=node.id))

    audit.record(
        "control_meter.reading_delete",
        f"Удалено показание узла «{node.name}» от {audit.format_date(reading.reading_date)}",
        entity_type="control_meter", entity_id=node.id,
    )
    database.db_session.delete(reading)
    database.db_session.commit()
    flash(_("Показание удалено."), "success")
    return redirect(url_for("control_meters.detail", node_id=node.id))


# ---------------------------------------------------------------------------
# Привязка гаражей к узлу — управление со страницы узла, не с формы гаража.
# ---------------------------------------------------------------------------

@bp.route("/<int:node_id>/garages/attach", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def attach_garage(node_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)

    garage_id = request.form.get("garage_id", type=int)
    garage = database.db_session.get(Garage, garage_id) if garage_id else None
    if garage is None:
        flash(_("Выберите гараж для привязки."), "danger")
        return redirect(url_for("control_meters.detail", node_id=node.id))

    garage.control_meter_id = node.id
    audit.record(
        "garage.control_meter_attach", f"Гараж №{garage.number} подключён к узлу «{node.name}»",
        entity_type="garage", entity_id=garage.id,
    )
    database.db_session.commit()
    flash(_("Гараж подключён к узлу."), "success")
    return redirect(url_for("control_meters.detail", node_id=node.id))


@bp.route("/<int:node_id>/garages/<int:garage_id>/detach", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def detach_garage(node_id, garage_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)
    garage = database.db_session.get(Garage, garage_id)
    if garage is None or garage.control_meter_id != node_id:
        abort(404)

    garage.control_meter_id = None
    audit.record(
        "garage.control_meter_detach", f"Гараж №{garage.number} отключён от узла «{node.name}»",
        entity_type="garage", entity_id=garage.id,
    )
    database.db_session.commit()
    flash(_("Гараж отключён от узла."), "success")
    return redirect(url_for("control_meters.detail", node_id=node.id))
