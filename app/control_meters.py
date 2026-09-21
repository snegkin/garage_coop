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

Отдельный случай — узел физически запитан не от ввода и не от другого узла
дерева, а через щиток конкретного гаража (ControlMeter.parent_garage_id,
взаимоисключающий с parent_id): напр. общее освещение, подключённое к
абонентскому счётчику одного из гаражей. Тогда показания абонента включают
чужое потребление и должны быть уменьшены на дельту такого узла перед
начислением — см. garage_supplied_nodes_delta/reconcile_garage_supply и
garages.add_electricity_reading.

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

def _build_tree(all_nodes, gateway_garages=()):
    """Строит дерево {"kind": "node"|"garage", "node": ControlMeter|Garage,
    "children": [...]} из плоского списка узлов.

    gateway_garages — гаражи, через щиток которых физически запитан хотя бы
    один узел (ControlMeter.parent_garage_id, см. _gateway_garages) —
    встраиваются в дерево как точки подключения: сами занимают место среди
    детей узла, к которому подключены как обычный потребитель
    (garage.control_meter_id), а их «дочерние» узлы вкладываются уже под
    них. Без этого параметра (по умолчанию) ведёт себя как раньше — чистое
    дерево ControlMeter, используется _parent_options для выбора родителя
    узла, которому гаражи ни к чему."""
    by_id = {n.id: {"kind": "node", "node": n, "children": []} for n in all_nodes}
    garage_entries = {g.id: {"kind": "garage", "node": g, "children": []} for g in gateway_garages}
    roots = []
    for n in all_nodes:
        entry = by_id[n.id]
        if n.parent_id is not None and n.parent_id in by_id:
            by_id[n.parent_id]["children"].append(entry)
        elif n.parent_garage_id is not None and n.parent_garage_id in garage_entries:
            garage_entries[n.parent_garage_id]["children"].append(entry)
        else:
            roots.append(entry)

    for g in gateway_garages:
        entry = garage_entries[g.id]
        if g.control_meter_id is not None and g.control_meter_id in by_id:
            by_id[g.control_meter_id]["children"].append(entry)
        else:
            roots.append(entry)

    def sort_key(entry):
        obj = entry["node"]
        return obj.name.lower() if entry["kind"] == "node" else f"гараж №{obj.number}".lower()

    def sort_rec(items):
        items.sort(key=sort_key)
        for it in items:
            sort_rec(it["children"])

    sort_rec(roots)
    return roots


def _gateway_garages():
    """Гаражи, через щиток которых физически запитан хотя бы один
    контрольный узел (ControlMeter.parent_garage_id) — обычно единицы на
    весь кооператив (см. _build_tree)."""
    return (
        database.db_session.query(Garage)
        .join(ControlMeter, ControlMeter.parent_garage_id == Garage.id)
        .distinct()
        .order_by(Garage.number)
        .all()
    )


def _wrap_with_root(tree):
    """Оборачивает лес узлов верхнего уровня (parent_id IS NULL) в один
    синтетический корень {"node": None, "children": tree} — верхний узел
    дерева ВСЕГДА физически есть вводной счётчик кооператива (см.
    _tree.html: node=None рендерится как «Ввод», со ссылкой на /power/), а
    не отдельная сущность ControlMeter, поэтому узлы верхнего уровня в
    дереве показываются как его дети, а не как несвязанный лес."""
    return [{"kind": "node", "node": None, "children": tree}]


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
    node: ControlMeter | Garage | None  # None = виртуальный корень (ввод); Garage — см. reconcile_garage_supply
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
    NULL и НЕ запитанные через гараж, см. parent_garage_id) и гаражи без
    узла (control_meter_id IS NULL)."""
    if node is None:
        node_delta = _delta(_master_reading_as_of(date_from), _master_reading_as_of(date_to))
        child_nodes = (
            database.db_session.query(ControlMeter)
            .filter(ControlMeter.parent_id.is_(None), ControlMeter.parent_garage_id.is_(None))
            .order_by(ControlMeter.name).all()
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
# Схема (Mermaid) — альтернативный, графический вид того же дерева, что и
# cmtree.render_tree (_tree.html), переключатель «Дерево/Схема» на list.html.
# ---------------------------------------------------------------------------

def _mermaid_escape(text: str) -> str:
    """HTML-экранирование для подписи блока Mermaid — сами подписи (имя узла,
    номер гаража) в итоге попадают в HTML как есть (см. list.html:
    {{ scheme_mermaid | safe }}, экранирование только здесь, до | safe), и
    Mermaid-синтаксис тоже чувствителен к кавычкам внутри ["..."]."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("\n", " ")
    )


def _mermaid_status_class(rec: NodeReconciliation | None) -> str:
    """Тот же набор статусов, что и бейджи в _tree.html (см. render_tree) —
    цвет блока схемы вместо текста бейджа."""
    if rec is None or rec.loss is None:
        return "cmMuted"
    if rec.is_negative:
        return "cmBad"
    if rec.loss == 0:
        return "cmOk"
    return "cmWarn" if rec.is_partial else "cmInfo"


def build_scheme_mermaid(tree, reconcile_default, reconcile_garage_default) -> str:
    """Текст Mermaid flowchart по тому же дереву {"kind", "node", "children"},
    что и cmtree.render_tree (_tree.html) — с той же подсветкой статуса
    сверки и кликом на карточку узла/гаража-точки подключения (см.
    _mermaid_status_class).

    Обычные подключённые гаражи (Garage.control_meter_id) отдельными
    блоками НЕ рисуются — как и в текстовом дереве, только их количество в
    подписи узла (n.garages) — иначе схема на большом кооперативе раздулась
    бы до сотен блоков и стала бы нечитаемой."""
    lines = [
        "flowchart TD",
        "classDef cmOk fill:#d1e7dd,stroke:#0f5132,color:#0f5132",
        "classDef cmWarn fill:#fff3cd,stroke:#997404,color:#664d03",
        "classDef cmBad fill:#f8d7da,stroke:#842029,color:#842029",
        "classDef cmInfo fill:#cff4fc,stroke:#055160,color:#055160",
        "classDef cmMuted fill:#e9ecef,stroke:#495057,color:#495057",
    ]

    def dom_id(kind, n):
        if kind == "node" and n is None:
            return "cmRoot"
        return f"{'cmGw' if kind == 'garage' else 'cmNode'}{n.id}"

    def walk(entries, parent_id):
        for entry in entries:
            n, kind = entry["node"], entry["kind"]
            this_id = dom_id(kind, n)

            if kind == "node" and n is None:
                label, url = _("Ввод (общий счётчик)"), url_for("power.view")
                status = _mermaid_status_class(reconcile_default(None))
            elif kind == "garage":
                label, url = _("Гараж №{number}", number=n.number), url_for("garages.detail", garage_id=n.id)
                status = _mermaid_status_class(reconcile_garage_default(n))
            else:
                garage_count = len(n.garages)
                label = f"{n.name} ({garage_count})" if garage_count else n.name
                url = url_for("control_meters.detail", node_id=n.id)
                status = _mermaid_status_class(reconcile_default(n))

            lines.append(f'{this_id}["{_mermaid_escape(label)}"]')
            lines.append(f'click {this_id} "{url}"')
            lines.append(f"class {this_id} {status}")
            if parent_id is not None:
                lines.append(f"{parent_id} --> {this_id}")

            walk(entry["children"], this_id)

    walk(tree, None)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Узлы, запитанные через абонентский счётчик гаража (ControlMeter.
# parent_garage_id) — напр. общее освещение, подключённое не к вводу и не к
# другому узлу дерева, а прямо к щитку конкретного гаража. Показания
# абонентского счётчика такого гаража включают в себя чужое потребление —
# при начислении (см. garages.add_electricity_reading) его нужно вычесть.
# ---------------------------------------------------------------------------

def garage_supplied_nodes_delta(garage: Garage, date_from: dt.date, date_to: dt.date) -> tuple[Decimal, bool]:
    """Сумма дельт контрольных узлов, физически запитанных через щиток этого
    гаража, за интервал. (0, False), если таких узлов нет.

    is_partial=True — хотя бы у одного из узлов нет показания на одну из
    границ интервала: его дельта НЕ входит в сумму (недобор, а не 0) —
    вызывающий код обязан явно показать это как «вычтено не полностью»,
    иначе начисление молча занижает то, что реально вычтено."""
    nodes = garage.supplied_control_meters
    if not nodes:
        return Decimal("0"), False
    total = Decimal("0")
    is_partial = False
    for node in nodes:
        delta = _delta(_node_reading_as_of(node, date_from), _node_reading_as_of(node, date_to))
        if delta is None:
            is_partial = True
            continue
        total += delta
    return total, is_partial


def reconcile_garage_supply(garage: Garage, date_from: dt.date, date_to: dt.date) -> NodeReconciliation:
    """Аналог reconcile_node, но «родитель» — абонентский счётчик гаража, а
    не узел дерева: node_delta — дельта его показаний, потребители — узлы из
    garage.supplied_control_meters. В отличие от reconcile_node, «loss» тут
    не потери в проводке для разноски поровну, а чистое потребление самого
    гаража (то, что реально уходит в его начисление, см.
    garages.add_electricity_reading) — поэтому share_of_loss/
    loss_per_consumer намеренно не проставляются: делить тут нечего, узел
    либо целиком «съедает» свою измеренную дельту, либо остаток целиком
    достаётся гаражу."""
    node_delta = _garage_delta(garage, date_from, date_to)
    child_nodes = garage.supplied_control_meters

    consumers = [
        ConsumerReconciliation(
            kind="node", ref=child,
            delta=_delta(_node_reading_as_of(child, date_from), _node_reading_as_of(child, date_to)),
        )
        for child in child_nodes
    ]

    consumers_total = len(consumers)
    with_data = [c for c in consumers if c.delta is not None]
    consumers_with_data = len(with_data)

    sum_children_delta = sum((c.delta for c in with_data), Decimal("0")) if consumers_with_data else None

    if node_delta is None or sum_children_delta is None:
        loss = None
    else:
        loss = (node_delta - sum_children_delta).quantize(Decimal("0.01"))

    is_partial = node_delta is None or consumers_with_data < consumers_total
    is_negative = loss is not None and loss < 0

    return NodeReconciliation(
        node=garage, date_from=date_from, date_to=date_to, node_delta=node_delta,
        consumers=consumers, consumers_with_data=consumers_with_data, consumers_total=consumers_total,
        sum_children_delta=sum_children_delta, loss=loss, loss_per_consumer=None,
        is_partial=is_partial, is_negative=is_negative,
    )


def reconcile_garage_supply_default(garage: Garage) -> NodeReconciliation | None:
    """Сверка по двум последним показаниям абонентского счётчика гаража.
    None, если счётчика нет или показаний меньше двух."""
    meter = _current_meter(garage)
    if meter is None:
        return None
    q = database.db_session.query(ElectricityReading).filter_by(meter_id=meter.id).order_by(
        ElectricityReading.reading_date.desc(), ElectricityReading.id.desc()
    )
    dates = _last_two_dates(q)
    if dates is None:
        return None
    date_from, date_to = dates
    return reconcile_garage_supply(garage, date_from, date_to)


# ---------------------------------------------------------------------------
# CRUD дерева узлов
# ---------------------------------------------------------------------------

@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_tree():
    all_nodes = database.db_session.query(ControlMeter).all()
    tree = _wrap_with_root(_build_tree(all_nodes, _gateway_garages()))
    root_reconciliation = reconcile_node_default(None)
    scheme_mermaid = build_scheme_mermaid(tree, reconcile_node_default, reconcile_garage_supply_default)
    return render_template(
        "control_meters/list.html", tree=tree,
        root_reconciliation=root_reconciliation, scheme_mermaid=scheme_mermaid,
        reconcile_default=reconcile_node_default, reconcile_garage_default=reconcile_garage_supply_default,
    )


def _parent_from_form(f):
    """(parent_id, parent_garage_id) из формы create/edit — ровно одно из
    двух заполнено, по значению radio parent_kind (см. form.html).

    Без parent_kind в запросе (старые тесты, прямые POST) — обратная
    совместимость: трактуем как раньше, просто parent_id."""
    kind = f.get("parent_kind")
    if kind is None:
        return (int(f["parent_id"]) if f.get("parent_id") else None), None
    if kind == "node":
        return (int(f["parent_id"]) if f.get("parent_id") else None), None
    if kind == "garage":
        return None, (int(f["parent_garage_id"]) if f.get("parent_garage_id") else None)
    return None, None


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        parent_id, parent_garage_id = _parent_from_form(f)
        node = ControlMeter(
            name=f["name"], parent_id=parent_id, parent_garage_id=parent_garage_id,
            comment=f.get("comment") or None,
        )
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
    all_garages = database.db_session.query(Garage).order_by(Garage.number).all()
    preselected_parent_id = request.args.get("parent_id", type=int)
    preselected_parent_garage_id = request.args.get("parent_garage_id", type=int)
    return render_template(
        "control_meters/form.html", node=None, parent_options=_parent_options(all_nodes), all_garages=all_garages,
        preselected_parent_id=preselected_parent_id, preselected_parent_garage_id=preselected_parent_garage_id,
    )


@bp.route("/<int:node_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(node_id):
    node = database.db_session.get(ControlMeter, node_id)
    if node is None:
        abort(404)

    if request.method == "POST":
        f = request.form
        parent_id, parent_garage_id = _parent_from_form(f)
        if parent_id is not None and parent_id in _descendant_ids(node):
            flash(_("Нельзя сделать родителем сам узел или его же потомка."), "danger")
            all_nodes = database.db_session.query(ControlMeter).all()
            all_garages = database.db_session.query(Garage).order_by(Garage.number).all()
            return render_template(
                "control_meters/form.html", node=node,
                parent_options=_parent_options(all_nodes, exclude_ids=_descendant_ids(node)), all_garages=all_garages,
                preselected_parent_id=None, preselected_parent_garage_id=None,
            )

        node.name = f["name"]
        node.parent_id = parent_id
        node.parent_garage_id = parent_garage_id
        node.comment = f.get("comment") or None
        audit.record(
            "control_meter.edit", f"Изменён узел контрольного счётчика «{node.name}»",
            entity_type="control_meter", entity_id=node.id,
        )
        database.db_session.commit()
        flash(_("Узел изменён."), "success")
        return redirect(url_for("control_meters.detail", node_id=node.id))

    all_nodes = database.db_session.query(ControlMeter).all()
    all_garages = database.db_session.query(Garage).order_by(Garage.number).all()
    return render_template(
        "control_meters/form.html", node=node,
        parent_options=_parent_options(all_nodes, exclude_ids=_descendant_ids(node)), all_garages=all_garages,
        preselected_parent_id=None, preselected_parent_garage_id=None,
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

    # ("node", ControlMeter) | ("garage", Garage) — цепочка предков обычно
    # идёт вверх по ControlMeter.parent, но может оборваться на гараже
    # (parent_garage_id, см. модуль-докстринг) — дальше вверх для гаража
    # пути в этой иерархии нет.
    ancestor_ids = set()
    breadcrumbs = []
    cur = node
    while True:
        if cur.parent_id is not None and cur.parent is not None:
            cur = cur.parent
            ancestor_ids.add(("node", cur.id))
            breadcrumbs.append(("node", cur))
        elif cur.parent_garage_id is not None and cur.parent_garage is not None:
            ancestor_ids.add(("garage", cur.parent_garage.id))
            breadcrumbs.append(("garage", cur.parent_garage))
            break
        else:
            break
    breadcrumbs.reverse()

    all_nodes = database.db_session.query(ControlMeter).all()
    tree = _wrap_with_root(_build_tree(all_nodes, _gateway_garages()))

    return render_template(
        "control_meters/detail.html", node=node,
        readings_desc=readings_desc, readings_with_deltas=readings_with_deltas,
        reconciliation=reconciliation, attachable_garages=attachable_garages,
        ancestor_ids=ancestor_ids, breadcrumbs=breadcrumbs,
        tree=tree, reconcile_default=reconcile_node_default, reconcile_garage_default=reconcile_garage_supply_default,
        today=dt.date.today(),
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
