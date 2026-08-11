import os
import uuid
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, g, abort,
    current_app, send_from_directory,
)

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .models import (
    Garage, Person, GarageOwnership, GarageContact, GaragePhoto, PersonalAccount,
    MemberAccount, FeeType, RoleEnum,
)
from .accounting import electricity_account_number, member_account_number, balance

bp = Blueprint("garages", __name__, url_prefix="/garages")


def _is_owner_or_board(garage: Garage) -> bool:
    """Правление/председатель — любой гараж; рядовой член — только свой (по владению)."""
    if g.user.role in (RoleEnum.CHAIRMAN, RoleEnum.BOARD, RoleEnum.ACCOUNTANT):
        return True
    if g.user.person_id is None:
        return False
    owner_ids = {o.person_id for o in garage.ownerships}
    return g.user.person_id in owner_ids


def _ensure_member_accounts(garage: Garage, person_id: int, owner_index: int):
    """
    Заводит члену кооператива лицевые счета на все виды взносов/налогов,
    для которых задан type_code (см. FeeType), по этому гаражу — если их
    ещё нет. Электричество сюда не входит — у него отдельный счёт на гараж.
    """
    fee_types = (
        database.db_session.query(FeeType)
        .filter(FeeType.type_code.isnot(None))
        .all()
    )
    for fee_type in fee_types:
        exists = (
            database.db_session.query(MemberAccount)
            .filter_by(person_id=person_id, garage_id=garage.id, fee_type_id=fee_type.id)
            .first()
        )
        if exists:
            continue
        number = member_account_number(fee_type.type_code, garage.number, owner_index, fee_type.is_penalty)
        database.db_session.add(MemberAccount(
            person_id=person_id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=number,
        ))


@bp.route("/")
@login_required
def list_garages():
    garages = database.db_session.query(Garage).order_by(Garage.number).all()
    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    preselect_person_id = request.args.get("new_person_id", type=int)
    balances = {garage.id: (balance(garage) if garage else None) for garage in garages}
    return render_template(
        "garages/list.html", garages=garages, all_persons=all_persons,
        preselect_person_id=preselect_person_id, balances=balances,
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        garage = Garage(
            number=f["number"],
            area_sqm=Decimal(f["area_sqm"]),
            coefficient=Decimal(f["coefficient"]) if f.get("coefficient") else Decimal("1"),
            land_privatized=bool(f.get("land_privatized")),
            cadastral_number=f.get("cadastral_number") or None,
            land_cadastral_number=f.get("land_cadastral_number") or None,
            privatized_land_area=Decimal(f["privatized_land_area"]) if f.get("privatized_land_area") else None,
            comment=f.get("comment") or None,
        )
        database.db_session.add(garage)
        database.db_session.flush()  # чтобы получить garage.id

        # лицевой счёт на электричество заводится автоматически вместе с гаражом
        account = PersonalAccount(garage_id=garage.id, account_number=electricity_account_number(garage.number))
        database.db_session.add(account)

        # собственники, указанные прямо в форме создания
        person_ids = request.form.getlist("owner_person_id")
        shares = request.form.getlist("owner_share")
        owner_index = 0
        for person_id, share_raw in zip(person_ids, shares):
            if not person_id or not share_raw:
                continue
            try:
                share = Decimal(share_raw)
            except InvalidOperation:
                continue
            if not (0 < share <= 1):
                continue
            database.db_session.add(GarageOwnership(garage_id=garage.id, person_id=int(person_id), share=share))
            database.db_session.flush()
            _ensure_member_accounts(garage, int(person_id), owner_index)
            owner_index += 1

        # фото гаража (необязательно)
        upload = request.files.get("photo")
        if upload and upload.filename:
            ext = os.path.splitext(upload.filename)[1].lower()
            if ext in ALLOWED_PHOTO_EXT:
                stored_name = f"{uuid.uuid4().hex}{ext}"
                upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))
                database.db_session.add(GaragePhoto(garage_id=garage.id, file_path=stored_name))
            else:
                flash(_("Фото не сохранено: поддерживаются только изображения (jpg, png, webp, gif)."), "warning")

        database.db_session.commit()
        flash(_("Гараж №{number} создан, лицевой счёт открыт.", number=garage.number), "success")
        return redirect(url_for("garages.detail", garage_id=garage.id))

    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    preselect_person_id = request.args.get("new_person_id", type=int)
    return render_template(
        "garages/form.html", garage=None, all_persons=all_persons, preselect_person_id=preselect_person_id
    )


@bp.route("/<int:garage_id>")
@login_required
def detail(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        flash(_("Гараж не найден."), "danger")
        return redirect(url_for("garages.list_garages"))
    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    total_share = sum((o.share for o in garage.ownerships), Decimal("0"))
    preselect_contact_person_id = request.args.get("new_person_id", type=int)
    return render_template(
        "garages/detail.html",
        garage=garage,
        all_persons=all_persons,
        total_share=total_share,
        preselect_contact_person_id=preselect_contact_person_id,
    )


@bp.route("/<int:garage_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        flash(_("Гараж не найден."), "danger")
        return redirect(url_for("garages.list_garages"))

    if request.method == "POST":
        f = request.form
        garage.number = f["number"]
        garage.area_sqm = Decimal(f["area_sqm"])
        garage.coefficient = Decimal(f["coefficient"]) if f.get("coefficient") else Decimal("1")
        garage.land_privatized = bool(f.get("land_privatized"))
        garage.cadastral_number = f.get("cadastral_number") or None
        garage.land_cadastral_number = f.get("land_cadastral_number") or None
        garage.privatized_land_area = Decimal(f["privatized_land_area"]) if f.get("privatized_land_area") else None
        garage.comment = f.get("comment") or None
        database.db_session.commit()
        flash(_("Изменения сохранены."), "success")
        return redirect(url_for("garages.detail", garage_id=garage.id))

    return render_template("garages/form.html", garage=garage, all_persons=[], preselect_person_id=None)


@bp.route("/<int:garage_id>/owners/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_owner(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    person_id = int(request.form["person_id"])
    try:
        share = Decimal(request.form["share"])
    except InvalidOperation:
        flash(_("Доля должна быть числом (например 0.5)."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    if not (0 < share <= 1):
        flash(_("Доля должна быть в диапазоне от 0 (не включая) до 1."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    existing = (
        database.db_session.query(GarageOwnership)
        .filter_by(garage_id=garage.id, person_id=person_id)
        .first()
    )
    if existing:
        existing.share = share
    else:
        owner_index = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).count()
        database.db_session.add(GarageOwnership(garage_id=garage.id, person_id=person_id, share=share))
        database.db_session.flush()
        _ensure_member_accounts(garage, person_id, owner_index)
    database.db_session.commit()
    flash(_("Собственник добавлен/обновлён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/owners/<int:ownership_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def remove_owner(garage_id, ownership_id):
    ownership = database.db_session.get(GarageOwnership, ownership_id)
    if ownership and ownership.garage_id == garage_id:
        database.db_session.delete(ownership)
        database.db_session.commit()
        flash(_("Собственник удалён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/contacts/add", methods=["POST"])
@login_required
def add_contact(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _is_owner_or_board(garage):
        abort(403)
    person_id = int(request.form["person_id"])
    relation = request.form.get("relation") or None
    database.db_session.add(GarageContact(garage_id=garage_id, person_id=person_id, relation=relation))
    database.db_session.commit()
    flash(_("Контактное лицо добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/contacts/<int:contact_id>/remove", methods=["POST"])
@login_required
def remove_contact(garage_id, contact_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _is_owner_or_board(garage):
        abort(403)
    contact = database.db_session.get(GarageContact, contact_id)
    if contact and contact.garage_id == garage_id:
        database.db_session.delete(contact)
        database.db_session.commit()
        flash(_("Контактное лицо удалено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/comment", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def update_comment(garage_id):
    """Комментарий к гаражу видит и меняет только правление — это внутренние заметки, не для собственников."""
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    garage.comment = request.form.get("comment") or None
    database.db_session.commit()
    flash(_("Комментарий обновлён."), "success")
    return redirect(request.referrer or url_for("garages.detail", garage_id=garage_id))


# ---------------------------------------------------------------------------
# Фото гаража
# ---------------------------------------------------------------------------

ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@bp.route("/<int:garage_id>/photos/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_photo(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash(_("Выберите файл фотографии."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_PHOTO_EXT:
        flash(_("Поддерживаются только изображения (jpg, png, webp, gif)."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    stored_name = f"{uuid.uuid4().hex}{ext}"
    upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))

    photo = GaragePhoto(garage_id=garage.id, file_path=stored_name, caption=request.form.get("caption") or None)
    database.db_session.add(photo)
    database.db_session.commit()
    flash(_("Фото добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/photos/<int:photo_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_photo(garage_id, photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None or photo.garage_id != garage_id:
        abort(404)

    photo.caption = request.form.get("caption") or None

    upload = request.files.get("file")
    if upload and upload.filename:
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in ALLOWED_PHOTO_EXT:
            flash(_("Поддерживаются только изображения (jpg, png, webp, gif)."), "danger")
            return redirect(url_for("garages.detail", garage_id=garage_id))
        old_path = os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path)
        if os.path.exists(old_path):
            os.remove(old_path)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))
        photo.file_path = stored_name

    database.db_session.commit()
    flash(_("Фото обновлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/photos/<int:photo_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def remove_photo(garage_id, photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None or photo.garage_id != garage_id:
        abort(404)

    file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path)
    if os.path.exists(file_path):
        os.remove(file_path)
    database.db_session.delete(photo)
    database.db_session.commit()
    flash(_("Фото удалено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/photos/<int:photo_id>/file")
@login_required
def photo_file(photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], photo.file_path)
