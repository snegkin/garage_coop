"""
Мастер первого запуска — для председателя, единая точка входа в
первоначальное заполнение системы данными. Не отдельная параллельная
бизнес-логика: там, где уже есть полноценная страница (гараж с
собственниками — garages.create, состав правления — governance.term_detail
и т.д.), мастер только создаёт недостающую точку входа (например, самый
первый созыв правления без протокола общего собрания — на старте его
просто ещё не было) и ссылается на существующие страницы. Собственная
бизнес-логика в этом модуле — только там, где до сих пор не было вообще
никакой формы: импорт людей/гаражей из CSV.

Статус каждого шага (`wizard_status()`) считается на лету по данным в БД,
как и остальные агрегаты в проекте (см. accounting.cooperative_balance) —
отдельного «флага прохождения мастера» нет и не нужно: председатель может
открыть /setup/ в любой момент, не только при первом запуске, чтобы
дозаполнить то, что пропустил.
"""
import csv
import datetime as dt
import io
import json
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response, g

from . import database
from .i18n import translate as _
from .auth import roles_required
from .permissions import sync_user_role
from .models import (
    Cooperative, BankAccount, Counterparty, ElectricitySettings, ElectricityTariff,
    MasterMeterReading, Garage, GarageOwnership, Person, PersonalAccount, BoardTerm, RoleEnum,
    CsvImportProfile,
)
from .accounting import (
    get_electricity_settings, current_tariff, electricity_account_number,
)
from .garages import _ensure_member_accounts
from .governance import _current_term

bp = Blueprint("setup_wizard", __name__, url_prefix="/setup")


def _parse_decimal(raw):
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def wizard_status() -> dict:
    """Считает готовность каждого шага мастера по фактическим данным в БД."""
    coop = database.db_session.query(Cooperative).first()
    cooperative_done = bool(coop and coop.full_name and coop.inn and coop.kpp and coop.ogrn)

    bank_account_done = database.db_session.query(BankAccount).count() > 0

    settings = database.db_session.query(ElectricitySettings).first()
    counterparty_done = bool(settings and settings.supplier_id)

    tariff_done = database.db_session.query(ElectricityTariff).count() > 0
    meter_done = database.db_session.query(MasterMeterReading).count() > 0
    people_done = database.db_session.query(Person).count() > 0
    garages_done = database.db_session.query(Garage).count() > 0

    term = _current_term()
    board_done = bool(term and any(m.is_chairman for m in term.members))

    steps = [
        ("cooperative", _("Карточка кооператива"), cooperative_done),
        ("bank_account", _("Расчётный счёт"), bank_account_done),
        ("counterparty", _("Поставщик электроэнергии"), counterparty_done),
        ("tariff", _("Тариф на электроэнергию"), tariff_done),
        ("meter", _("Счётчик кооператива и начальные показания"), meter_done),
        ("people", _("Люди"), people_done),
        ("garages", _("Гаражи"), garages_done),
        ("board", _("Состав правления"), board_done),
    ]
    return {
        "steps": steps,
        "all_done": all(done for _key, _title, done in steps),
        "term": term,
    }


@bp.route("/")
@roles_required(RoleEnum.CHAIRMAN)
def index():
    status = wizard_status()
    return render_template("setup/index.html", status=status)


# ---------------------------------------------------------------------------
# Кооператив
# ---------------------------------------------------------------------------

@bp.route("/cooperative", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def cooperative_step():
    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        coop = Cooperative(full_name="", inn="", kpp="", ogrn="")
        database.db_session.add(coop)

    if request.method == "POST":
        f = request.form
        coop.full_name = f["full_name"]
        coop.short_name = f.get("short_name") or None
        coop.inn = f["inn"]
        coop.kpp = f["kpp"]
        coop.ogrn = f["ogrn"]
        coop.legal_address = f.get("legal_address") or None
        coop.postal_address = f.get("postal_address") or None
        reg_date = f.get("registration_date")
        coop.registration_date = dt.date.fromisoformat(reg_date) if reg_date else None
        coop.total_area = _parse_decimal(f.get("total_area"))
        coop.garage_area = _parse_decimal(f.get("garage_area"))
        coop.common_area = _parse_decimal(f.get("common_area"))
        dues_due_day = f.get("dues_due_day")
        dues_due_month = f.get("dues_due_month")
        coop.dues_due_day = int(dues_due_day) if dues_due_day else None
        coop.dues_due_month = int(dues_due_month) if dues_due_month else None
        database.db_session.commit()
        flash(_("Карточка кооператива сохранена."), "success")
        return redirect(url_for("setup_wizard.index"))

    return render_template("setup/cooperative.html", coop=coop)


# ---------------------------------------------------------------------------
# Расчётный счёт
# ---------------------------------------------------------------------------

@bp.route("/bank-account", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def bank_account_step():
    if request.method == "POST":
        f = request.form
        is_primary = bool(f.get("is_primary")) or database.db_session.query(BankAccount).count() == 0
        if is_primary:
            database.db_session.query(BankAccount).update({"is_primary": False})
        database.db_session.add(BankAccount(
            bank_name=f["bank_name"],
            bik=f.get("bik") or None,
            checking_account=f["checking_account"],
            correspondent_account=f.get("correspondent_account") or None,
            is_primary=is_primary,
            comment=f.get("comment") or None,
            balance=_parse_decimal(f.get("balance")),
            balance_updated_at=dt.date.fromisoformat(f["balance_updated_at"]) if f.get("balance_updated_at") else None,
        ))
        database.db_session.commit()
        flash(_("Расчётный счёт добавлен."), "success")
        return redirect(url_for("setup_wizard.index"))

    accounts = database.db_session.query(BankAccount).order_by(BankAccount.is_primary.desc()).all()
    return render_template("setup/bank_account.html", accounts=accounts, today=dt.date.today())


# ---------------------------------------------------------------------------
# Поставщик электроэнергии (контрагент)
# ---------------------------------------------------------------------------

@bp.route("/counterparty", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def counterparty_step():
    settings = get_electricity_settings()

    if request.method == "POST":
        f = request.form
        action = f.get("action")

        if action == "create":
            counterparty = Counterparty(
                name=f["name"],
                inn=f.get("inn") or None,
                kpp=f.get("kpp") or None,
                category=_("электрика"),
                phone=f.get("phone") or None,
                email=f.get("email") or None,
                address=f.get("address") or None,
            )
            database.db_session.add(counterparty)
            database.db_session.flush()
            settings.supplier_id = counterparty.id
            database.db_session.commit()
            flash(_("Контрагент-поставщик электроэнергии создан и привязан."), "success")
        elif action == "link":
            counterparty_id = f.get("counterparty_id")
            settings.supplier_id = int(counterparty_id) if counterparty_id else None
            database.db_session.commit()
            flash(_("Поставщик электроэнергии сохранён."), "success")
        return redirect(url_for("setup_wizard.index"))

    other_counterparties = database.db_session.query(Counterparty).order_by(Counterparty.name).all()
    return render_template("setup/counterparty.html", settings=settings, other_counterparties=other_counterparties)


# ---------------------------------------------------------------------------
# Тариф на электроэнергию
# ---------------------------------------------------------------------------

@bp.route("/tariff", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def tariff_step():
    if request.method == "POST":
        f = request.form
        database.db_session.add(ElectricityTariff(
            rate=Decimal(f["rate"]),
            effective_date=dt.date.fromisoformat(f["effective_date"]),
            comment=f.get("comment") or None,
        ))
        database.db_session.commit()
        flash(_("Тариф добавлен."), "success")
        return redirect(url_for("setup_wizard.index"))

    tariffs = database.db_session.query(ElectricityTariff).order_by(ElectricityTariff.effective_date.desc()).all()
    return render_template("setup/tariff.html", tariffs=tariffs, today=dt.date.today())


# ---------------------------------------------------------------------------
# Счётчик кооператива (общий/вводный) и начальные показания
# ---------------------------------------------------------------------------

@bp.route("/meter", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def meter_step():
    tariff = current_tariff()

    if request.method == "POST":
        if tariff is None:
            flash(_("Сначала добавьте тариф на электроэнергию — без него нельзя рассчитать сумму по показаниям."), "danger")
            return redirect(url_for("setup_wizard.meter_step"))

        f = request.form
        year = int(f["year"])
        month = int(f["month"])
        existing = database.db_session.query(MasterMeterReading).filter_by(year=year, month=month).first()
        if existing:
            flash(_("Запись за {year}-{month:02d} уже существует.", year=year, month=month), "warning")
            return redirect(url_for("setup_wizard.meter_step"))

        # Начальные (первые в истории) показания — сумму считать не с чем
        # сравнивать, поэтому amount не заполняется; со следующего показания
        # (уже на обычной странице «Электроэнергия») сумма начнёт считаться
        # как обычно, от этой точки.
        database.db_session.add(MasterMeterReading(
            year=year,
            month=month,
            reading_date=dt.date(year, month, 1),
            reading=Decimal(f["reading"]),
            tariff_id=tariff.id,
            comment=f.get("comment") or _("Начальные показания (внесены через мастер первого запуска)"),
        ))
        database.db_session.commit()
        flash(_("Начальные показания общего счётчика внесены."), "success")
        return redirect(url_for("setup_wizard.index"))

    has_readings = database.db_session.query(MasterMeterReading).count() > 0
    today = dt.date.today()
    return render_template("setup/meter.html", tariff=tariff, has_readings=has_readings, today=today)


# ---------------------------------------------------------------------------
# Настраиваемый формат CSV — общая инфраструктура для шагов «Люди» и
# «Гаражи». Председатель сам решает, какие колонки и в каком порядке у
# него выгружены во внешнем файле (импорт из 1С/Excel/чего угодно редко
# совпадает по составу колонок с тем, что нужно системе) — вместо жёстко
# зашитого порядка колонок это настройка (CsvImportProfile), по одной
# записи на тип импорта, сохраняется и переиспользуется при повторных
# импортах.
# ---------------------------------------------------------------------------

# Каталог: (ключ поля, подпись для UI, обязательна ли колонка в формате)
PEOPLE_COLUMN_CATALOG = [
    ("full_name", "ФИО", True),
    ("phone", "Телефон", False),
    ("email", "Email", False),
    ("registration_address", "Адрес регистрации", False),
    ("residence_address", "Адрес проживания", False),
    ("comment", "Комментарий", False),
]
PEOPLE_EXAMPLE_VALUES = {
    "full_name": "Иванов Иван Иванович", "phone": "+7 900 123-45-67",
    "email": "ivanov@example.com", "registration_address": "", "residence_address": "", "comment": "",
}

GARAGES_COLUMN_CATALOG = [
    ("number", "Номер гаража", True),
    ("area_sqm", "Площадь, м²", True),
    ("coefficient", "Коэффициент", False),
    ("land_privatized", "Участок приватизирован (1/0)", False),
    ("cadastral_number", "Кадастровый номер гаража", False),
    ("land_cadastral_number", "Кадастровый номер участка", False),
    ("privatized_land_area", "Площадь участка, м²", False),
    ("owner_full_name", "ФИО собственника", True),
    ("owner_share", "Доля собственника", False),
]
GARAGES_EXAMPLE_ROWS = [
    {"number": "95", "area_sqm": "24.5", "coefficient": "1", "land_privatized": "0",
     "cadastral_number": "", "land_cadastral_number": "", "privatized_land_area": "",
     "owner_full_name": "Иванов Иван Иванович", "owner_share": "1"},
    {"number": "96", "area_sqm": "24.5", "coefficient": "1", "land_privatized": "1",
     "cadastral_number": "77:01:0001001:123", "land_cadastral_number": "77:01:0001001:124",
     "privatized_land_area": "18", "owner_full_name": "Петров Пётр Петрович", "owner_share": "0.5"},
    {"number": "96", "area_sqm": "24.5", "coefficient": "1", "land_privatized": "1",
     "cadastral_number": "77:01:0001001:123", "land_cadastral_number": "77:01:0001001:124",
     "privatized_land_area": "18", "owner_full_name": "Сидорова Анна Ильинична", "owner_share": "0.5"},
]


def _get_import_columns(import_type: str, catalog) -> list[str]:
    """Активный (сохранённый) формат — список ключей колонок в порядке файла.
    Если формат ещё не настраивался, по умолчанию — полный каталог в
    исходном порядке (это же и был жёсткий формат до появления настройки,
    так что старые инструкции/примеры остаются рабочими без изменений)."""
    valid_keys = {k for k, _l, _r in catalog}
    profile = database.db_session.query(CsvImportProfile).filter_by(import_type=import_type).first()
    if profile:
        try:
            cols = [c for c in json.loads(profile.columns) if c in valid_keys]
        except (ValueError, TypeError):
            cols = []
        if cols:
            return cols
    return [k for k, _l, _r in catalog]


def _save_import_columns(import_type: str, columns: list[str]) -> None:
    profile = database.db_session.query(CsvImportProfile).filter_by(import_type=import_type).first()
    payload = json.dumps(columns, ensure_ascii=False)
    if profile is None:
        database.db_session.add(CsvImportProfile(import_type=import_type, columns=payload))
    else:
        profile.columns = payload
    database.db_session.commit()


def _parse_import_format_form(catalog) -> list[str]:
    """Из полей вида col_<key> (чекбокс) / pos_<key> (номер позиции в файле)
    собирает итоговый порядок колонок. Позиция — просто число для сортировки,
    вводится в обычное текстовое/числовое поле, без drag-and-drop и JS."""
    catalog_index = {k: i for i, (k, _l, _r) in enumerate(catalog)}
    picked = []
    for key, _label, _required in catalog:
        if not request.form.get(f"col_{key}"):
            continue
        try:
            pos = int(request.form.get(f"pos_{key}") or 0)
        except ValueError:
            pos = 0
        picked.append((pos, catalog_index[key], key))
    picked.sort(key=lambda t: (t[0], t[1]))
    return [key for _pos, _idx, key in picked]


def _looks_like_header_row(row, catalog) -> bool:
    """Первая строка файла — заголовок, если хотя бы одна ячейка совпадает
    (без учёта регистра) с подписью какой-либо колонки из каталога. Раньше,
    при жёстком формате, проверялась только первая ячейка на конкретное имя
    («ФИО»/«Номер гаража») — с настраиваемым форматом это уже не работает,
    т.к. первая колонка может быть любой."""
    labels_lower = {lbl.strip().lower() for _k, lbl, _r in catalog}
    return any(cell.strip().lower() in labels_lower for cell in row)


def _build_template_csv(columns: list[str], catalog, example_rows: list[dict]) -> str:
    labels = dict((k, lbl) for k, lbl, _r in catalog)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([labels[k] for k in columns])
    for example in example_rows:
        writer.writerow([example.get(k, "") for k in columns])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Люди — добавление по одному (переиспользует persons.create) или импорт из CSV
# ---------------------------------------------------------------------------

@bp.route("/people")
@roles_required(RoleEnum.CHAIRMAN)
def people_step():
    persons = database.db_session.query(Person).order_by(Person.id.desc()).limit(50).all()
    total = database.db_session.query(Person).count()
    active_columns = _get_import_columns("people", PEOPLE_COLUMN_CATALOG)
    labels = dict((k, lbl) for k, lbl, _r in PEOPLE_COLUMN_CATALOG)
    return render_template(
        "setup/people.html", persons=persons, total=total,
        active_columns=[labels[k] for k in active_columns],
        catalog=PEOPLE_COLUMN_CATALOG, active_keys=set(active_columns),
        positions={k: i + 1 for i, k in enumerate(active_columns)},
        format_action=url_for("setup_wizard.people_import_format"),
        modal_id="peopleFormatModal",
    )


@bp.route("/people/format", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def people_import_format():
    columns = _parse_import_format_form(PEOPLE_COLUMN_CATALOG)
    required = {k for k, _l, r in PEOPLE_COLUMN_CATALOG if r}
    if not required.issubset(set(columns)):
        flash(_("В формате обязательно должна быть колонка «ФИО»."), "danger")
        return redirect(url_for("setup_wizard.people_step"))
    _save_import_columns("people", columns)
    flash(_("Формат CSV для импорта людей сохранён."), "success")
    return redirect(url_for("setup_wizard.people_step"))


@bp.route("/people/template.csv")
@roles_required(RoleEnum.CHAIRMAN)
def people_template():
    columns = _get_import_columns("people", PEOPLE_COLUMN_CATALOG)
    content = _build_template_csv(columns, PEOPLE_COLUMN_CATALOG, [PEOPLE_EXAMPLE_VALUES])
    return Response(
        content, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=people_template.csv"},
    )


@bp.route("/people/import", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def people_import():
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        flash(_("Выберите CSV-файл."), "danger")
        return redirect(url_for("setup_wizard.people_step"))

    try:
        text = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash(_("Не удалось прочитать файл — сохраните его в кодировке UTF-8."), "danger")
        return redirect(url_for("setup_wizard.people_step"))

    columns = _get_import_columns("people", PEOPLE_COLUMN_CATALOG)
    rows = list(csv.reader(io.StringIO(text)))
    if rows and _looks_like_header_row(rows[0], PEOPLE_COLUMN_CATALOG):
        rows = rows[1:]  # первая строка — заголовок, пропускаем

    existing_names = {
        p.full_name.strip().lower()
        for p in database.db_session.query(Person).all()
    }
    seen_in_file: set[str] = set()

    created = 0
    skipped_duplicate = 0
    skipped_invalid = 0

    for row in rows:
        if not row or not any(cell.strip() for cell in row):
            continue
        cells = [c.strip() for c in row] + [""] * max(0, len(columns) - len(row))
        values = dict(zip(columns, cells))

        full_name = values.get("full_name", "")
        if not full_name:
            skipped_invalid += 1
            continue

        key = full_name.lower()
        if key in existing_names or key in seen_in_file:
            skipped_duplicate += 1
            continue
        seen_in_file.add(key)

        person = Person(
            full_name=full_name,
            email=values.get("email") or None,
            registration_address=values.get("registration_address") or None,
            residence_address=values.get("residence_address") or None,
            comment=values.get("comment") or None,
        )
        database.db_session.add(person)
        database.db_session.flush()
        phone = values.get("phone") or ""
        if phone:
            from .models import Phone
            for number in phone.split(";"):
                number = number.strip()
                if number:
                    database.db_session.add(Phone(person_id=person.id, number=number))
        created += 1

    database.db_session.commit()
    flash(_(
        "Импорт людей завершён: добавлено {created}, пропущено дублей {dup}, пропущено с ошибками {inv}.",
        created=created, dup=skipped_duplicate, inv=skipped_invalid,
    ), "success" if created else "warning")
    return redirect(url_for("setup_wizard.people_step"))


# ---------------------------------------------------------------------------
# Гаражи — добавление по одному (переиспользует garages.create) или импорт из CSV
# ---------------------------------------------------------------------------

@bp.route("/garages")
@roles_required(RoleEnum.CHAIRMAN)
def garages_step():
    garages = database.db_session.query(Garage).order_by(Garage.id.desc()).limit(50).all()
    total = database.db_session.query(Garage).count()
    active_columns = _get_import_columns("garages", GARAGES_COLUMN_CATALOG)
    labels = dict((k, lbl) for k, lbl, _r in GARAGES_COLUMN_CATALOG)
    return render_template(
        "setup/garages.html", garages=garages, total=total,
        active_columns=[labels[k] for k in active_columns],
        catalog=GARAGES_COLUMN_CATALOG, active_keys=set(active_columns),
        positions={k: i + 1 for i, k in enumerate(active_columns)},
        format_action=url_for("setup_wizard.garages_import_format"),
        modal_id="garagesFormatModal",
    )


@bp.route("/garages/format", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def garages_import_format():
    columns = _parse_import_format_form(GARAGES_COLUMN_CATALOG)
    required = {k for k, _l, r in GARAGES_COLUMN_CATALOG if r}
    if not required.issubset(set(columns)):
        flash(_("В формате обязательно должны быть колонки «Номер гаража», «Площадь, м²» и «ФИО собственника»."), "danger")
        return redirect(url_for("setup_wizard.garages_step"))
    _save_import_columns("garages", columns)
    flash(_("Формат CSV для импорта гаражей сохранён."), "success")
    return redirect(url_for("setup_wizard.garages_step"))


@bp.route("/garages/template.csv")
@roles_required(RoleEnum.CHAIRMAN)
def garages_template():
    columns = _get_import_columns("garages", GARAGES_COLUMN_CATALOG)
    content = _build_template_csv(columns, GARAGES_COLUMN_CATALOG, GARAGES_EXAMPLE_ROWS)
    return Response(
        content, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=garages_template.csv"},
    )


def _find_or_create_person_by_name(full_name: str, cache: dict[str, Person]) -> Person:
    key = full_name.strip().lower()
    if key in cache:
        return cache[key]
    person = (
        database.db_session.query(Person)
        .filter(Person.full_name.ilike(full_name.strip()))
        .first()
    )
    if person is None:
        person = Person(full_name=full_name.strip())
        database.db_session.add(person)
        database.db_session.flush()
    cache[key] = person
    return person


@bp.route("/garages/import", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def garages_import():
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        flash(_("Выберите CSV-файл."), "danger")
        return redirect(url_for("setup_wizard.garages_step"))

    try:
        text = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash(_("Не удалось прочитать файл — сохраните его в кодировке UTF-8."), "danger")
        return redirect(url_for("setup_wizard.garages_step"))

    columns = _get_import_columns("garages", GARAGES_COLUMN_CATALOG)
    rows = list(csv.reader(io.StringIO(text)))
    if rows and _looks_like_header_row(rows[0], GARAGES_COLUMN_CATALOG):
        rows = rows[1:]

    garages_by_number: dict[str, Garage] = {
        g.number: g for g in database.db_session.query(Garage).all()
    }
    person_cache: dict[str, Person] = {}

    garages_created = 0
    owners_added = 0
    skipped_invalid = 0

    for row in rows:
        if not row or not any(cell.strip() for cell in row):
            continue
        cells = [c.strip() for c in row] + [""] * max(0, len(columns) - len(row))
        values = dict(zip(columns, cells))

        number = values.get("number", "")
        area_raw = values.get("area_sqm", "")
        owner_full_name = values.get("owner_full_name", "")

        if not number or not area_raw:
            skipped_invalid += 1
            continue
        try:
            area_sqm = Decimal(area_raw)
        except InvalidOperation:
            skipped_invalid += 1
            continue
        coefficient = _parse_decimal(values.get("coefficient")) or Decimal("1")
        land_privatized = (values.get("land_privatized") or "").strip() in {"1", "true", "да", "yes"}
        privatized_land_area = _parse_decimal(values.get("privatized_land_area"))
        cadastral_number = values.get("cadastral_number") or None
        land_cadastral_number = values.get("land_cadastral_number") or None

        garage = garages_by_number.get(number)
        if garage is None:
            garage = Garage(
                number=number,
                area_sqm=area_sqm,
                coefficient=coefficient,
                land_privatized=land_privatized,
                cadastral_number=cadastral_number,
                land_cadastral_number=land_cadastral_number,
                privatized_land_area=privatized_land_area,
            )
            database.db_session.add(garage)
            database.db_session.flush()
            database.db_session.add(PersonalAccount(
                garage_id=garage.id, account_number=electricity_account_number(garage.number),
            ))
            garages_by_number[number] = garage
            garages_created += 1

        if owner_full_name:
            person = _find_or_create_person_by_name(owner_full_name, person_cache)
            owner_share_raw = values.get("owner_share") or ""
            try:
                share = Decimal(owner_share_raw) if owner_share_raw else Decimal("1")
            except InvalidOperation:
                share = Decimal("1")
            if not (0 < share <= 1):
                share = Decimal("1")

            existing_ownership = (
                database.db_session.query(GarageOwnership)
                .filter_by(garage_id=garage.id, person_id=person.id)
                .first()
            )
            if existing_ownership is None:
                owner_index = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).count()
                database.db_session.add(GarageOwnership(garage_id=garage.id, person_id=person.id, share=share))
                database.db_session.flush()
                _ensure_member_accounts(garage, person.id, owner_index)
                owners_added += 1
            else:
                existing_ownership.share = share

    database.db_session.commit()
    flash(_(
        "Импорт гаражей завершён: создано гаражей {garages}, записей о собственниках добавлено {owners}, пропущено строк с ошибками {inv}.",
        garages=garages_created, owners=owners_added, inv=skipped_invalid,
    ), "success" if garages_created or owners_added else "warning")
    return redirect(url_for("setup_wizard.garages_step"))


# ---------------------------------------------------------------------------
# Состав правления — самый первый созыв (без протокола общего собрания,
# см. governance.create_term(), где протокол обязателен для последующих
# созывов; на старте системы избирать пока было нечем — состав просто
# фиксируется как есть). Дальше — полностью существующий интерфейс
# governance.term_detail (добавление членов, применение состава к правам).
# ---------------------------------------------------------------------------

@bp.route("/board")
@roles_required(RoleEnum.CHAIRMAN)
def board_step():
    term = _current_term()
    persons = database.db_session.query(Person).order_by(Person.full_name).all()
    return render_template(
        "setup/board.html", term=term, persons=persons,
        current_user_unlinked=(g.user.person_id is None),
    )


@bp.route("/board/init", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def board_init():
    if _current_term() is not None:
        flash(_("Действующий созыв правления уже есть."), "warning")
        return redirect(url_for("setup_wizard.board_step"))

    start_date_raw = request.form.get("start_date")
    start_date = dt.date.fromisoformat(start_date_raw) if start_date_raw else dt.date.today()
    term = BoardTerm(start_date=start_date, elected_by_meeting_id=None)
    database.db_session.add(term)
    database.db_session.commit()
    flash(_("Созыв правления создан. Теперь внесите его состав."), "success")
    return redirect(url_for("governance.term_detail", term_id=term.id))


@bp.route("/board/link-me", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def board_link_me():
    """
    Учётная запись первого председателя (см. seed.py) создаётся без связи
    с карточкой человека — её физически ещё не было. Без такой связи
    применение состава созыва (governance._apply_board_term_flags →
    sync_user_role) не повлияет на роль ЭТОЙ сессии входа, только на новые
    учётные записи. Быстрая привязка прямо из мастера — чтобы не уходить
    отдельно в «Учётные записи» на первом шаге настройки.
    """
    if g.user.person_id is not None:
        flash(_("К вашей учётной записи уже привязан человек."), "warning")
        return redirect(url_for("setup_wizard.board_step"))

    full_name = request.form.get("full_name", "").strip()
    if not full_name:
        flash(_("Укажите ФИО."), "danger")
        return redirect(url_for("setup_wizard.board_step"))

    person = Person(full_name=full_name, is_chairman=True)
    database.db_session.add(person)
    database.db_session.flush()
    g.user.person_id = person.id
    database.db_session.commit()
    flash(_("Карточка человека создана и привязана к вашей учётной записи."), "success")
    return redirect(url_for("setup_wizard.board_step"))
