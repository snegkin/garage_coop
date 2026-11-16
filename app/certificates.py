"""
Справки о владении гаражом (`/certificates/`) — только перенаправления.

Сама справка — обычный документ раздела «Бланки и шаблоны»
(app/documents.py, шаблон с code="garage_certificate", составляет только
председатель); отдельного раздела и журнала у справок нет — `/certificates/`
и `/certificates/new` перенаправляют туда (старые ссылки, кнопки на
карточках гаража и человека).

Собственник просит справку обычным заявлением в правление вида
«справка о владении гаражом» (app/applications.py, ApplicationKind.
CERTIFICATE) — отдельной страницы запроса нет, `/certificates/my` (старые
ссылки и уведомления) ведёт в редактор такого заявления.
"""
from flask import Blueprint, request, redirect, url_for

from . import database
from .auth import login_required, roles_required
from .models import ApplicationKind, RoleEnum

bp = Blueprint("certificates", __name__, url_prefix="/certificates")


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_certificates():
    return redirect(url_for("documents.index"))


@bp.route("/new")
@roles_required(RoleEnum.CHAIRMAN)
def create():
    """Кнопки «Справка о владении» на карточках гаража/человека —
    в составление документа по шаблону справки."""
    from .documents import certificate_template
    template = certificate_template()
    database.db_session.commit()
    args = {k: v for k, v in request.args.items() if k in ("owner", "person_id", "garage_id")}
    return redirect(url_for("documents.create", template_id=template.id, **args))


@bp.route("/my")
@login_required
def my_certificates():
    args = {"garage_id": request.args["garage_id"]} if request.args.get("garage_id") else {}
    return redirect(url_for("applications.create", kind=ApplicationKind.CERTIFICATE.value, **args))
