"""
Заказные письма через Почту России (`/postal-letters/`) — задача минимум:
уведомления о задолженности как электронные заказные письма (ЭЗП).

Изучены три способа подключения к Почте России (см. temp/ в корне
репозитория и план в истории разработки):

1. ИС ЭПС «Система-Система» (temp/*.pdf) — прямой REST API для настоящей
   электронной доставки содержимого письма. Требует отдельного
   согласования с Почтой, отдельного ГОСТ TLS-сертификата (не совпадает с
   обычным КЭП!) и туннеля stunnel/VipNet — слишком тяжело для объёма
   кооператива.
2. REST API «Отправка» (otpravka-api.pochta.ru) — у кооператива уже есть
   токен приложения, изучена официальная спецификация (temp/pochta/).
   Проверено предметно: этот API целиком про ОБЫЧНЫЕ отправления с
   бумажной печатью (создание заказов, партии, печатные ярлыки, расчёт
   стоимости). Поля с "уведомлением о вручении" — это подтверждение
   доставки бумажного письма, а не электронная доставка текста. Отдельного
   денежного баланса лицевого счёта в документации тоже нет. Настоящий
   функционал ЭЗП виден в GET /1.0/settings как erl-customer-id/
   erl-customer-token — подключение к системе из п.1, не отдельный метод
   этого API. Этот API для задачи минимум не подходит.
3. Личный кабинет otpravka.pochta.ru (веб-интерфейс) — доступ уже есть,
   можно вручную загрузить Excel-реестр + PDF-письма, подписанные КЭП
   кооператива (КриптоПро уже есть) — единственный реалистичный канал без
   похода за отдельным ГОСТ-сертификатом.

Поэтому этот модуль НЕ отправляет письма сам — он готовит пакет (реестр
.xls + PDF по каждому письму, см. build_registry_workbook/export) для
ручной загрузки правлением в личный кабинет otpravka.pochta.ru, а сами
статусы (отправлено/доставлено, ШПИ) правление переносит в систему вручную
по данным того же личного кабинета (см. PostalDispatchStatus).

Формат колонок реестра — точно по temp/Инструкция.docx (поля
FILE_NAME/ADDRESSLINE_TO/RECIPIENT_TYPE/.../NOTIFICATIONTYPE) и образцам
temp/*.xls. Файл обязательно в старом формате .xls (Excel 97-2003/BIFF,
через xlwt) — везде в материалах Почты фигурирует только он, не .xlsx.

Первая версия — только типовые уведомления о задолженности (тот же текст,
что и у app.legal_docs.debt_notice, тот же список должников из
list_debtor_persons). Письма произвольного содержания — не в этой задаче.
"""
import datetime as dt
import io
import os
import uuid
import zipfile

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response, current_app, g

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .persons import build_statement
from .legal_docs import list_debtor_persons, build_yearly_debt_breakdown, debt_categories, parse_selected_categories
from .models import Person, Cooperative, RoleEnum, Document, DocumentType, PostalDispatch, PostalDispatchStatus

bp = Blueprint("postal_letters", __name__, url_prefix="/postal-letters")

_STATUS_LABELS_RAW = {
    PostalDispatchStatus.DRAFT: "Черновик",
    PostalDispatchStatus.EXPORTED: "Выгружено",
    PostalDispatchStatus.SENT: "Загружено в ЛК Почты",
    PostalDispatchStatus.DELIVERED: "Доставлено",
    PostalDispatchStatus.FAILED: "Не доставлено",
}
_STATUS_BADGES = {
    PostalDispatchStatus.DRAFT: "bg-secondary",
    PostalDispatchStatus.EXPORTED: "bg-info text-dark",
    PostalDispatchStatus.SENT: "bg-primary",
    PostalDispatchStatus.DELIVERED: "bg-success",
    PostalDispatchStatus.FAILED: "bg-danger",
}


def status_label(status: PostalDispatchStatus) -> str:
    return _(_STATUS_LABELS_RAW[status])


def status_badge(status: PostalDispatchStatus) -> str:
    return _STATUS_BADGES[status]


def _coop_and_chairman():
    coop = database.db_session.query(Cooperative).first()
    chairman = database.db_session.query(Person).filter(Person.is_chairman.is_(True)).first()
    return coop, chairman


def _selected_persons_or_redirect():
    person_ids = [int(x) for x in request.form.getlist("person_id")]
    if not person_ids:
        flash(_("Выберите хотя бы одного должника."), "danger")
        return None
    persons = database.db_session.query(Person).filter(Person.id.in_(person_ids)).order_by(Person.full_name).all()
    if not persons:
        abort(404)
    return persons


def _render_debt_notice_pdf(
    person: Person, coop: Cooperative | None, chairman: Person | None, today: dt.date, categories: set[str],
) -> bytes | None:
    """PDF на одного получателя — переиспользует тот же шаблон/макрос, что
    и app.legal_docs.debt_notice (legal_docs/debt_notice_pdf.html поддерживает
    список docs, здесь всегда список из одного письма — Почте нужен
    отдельный PDF-файл на каждое отправление).

    categories — см. legal_docs.debt_categories(): по умолчанию без
    электричества (за него кооператив не судится и не рассылает
    претензии, см. докстринг модуля) — письмо по Почте формируется на ту
    же задолженность, что попала бы в уведомление/иск, а не на вообще
    весь баланс человека."""
    docs = [{
        "person": person, "summary": build_statement(person, categories=categories),
        "years": build_yearly_debt_breakdown(person, categories),
    }]
    html_str = render_template(
        "legal_docs/debt_notice_pdf.html", docs=docs, coop=coop, chairman=chairman, today=today, hide_chat_widgets=True,
    )
    try:
        import weasyprint
    except ImportError:
        flash(_("Для формирования PDF нужна библиотека weasyprint. Установите: pip install weasyprint"), "danger")
        return None
    return weasyprint.HTML(string=html_str, base_url=request.url_root).write_pdf()


def _save_generated_pdf(pdf_bytes: bytes) -> str:
    stored_name = f"{uuid.uuid4().hex}.pdf"
    with open(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name), "wb") as f:
        f.write(pdf_bytes)
    return stored_name


def build_registry_workbook(dispatches: list[PostalDispatch], coop: Cooperative | None) -> bytes:
    """Реестр для загрузки в otpravka.pochta.ru, формат — temp/Инструкция.docx.
    RECIPIENT_TYPE=0 (физлицо), MAILCATEGORY=1 (заказное), NOTIFICATIONTYPE="E"
    (электронное уведомление о вручении) — фиксированные значения для этой
    задачи, других типов отправлений первая версия не поддерживает."""
    import xlwt

    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet("Реестр")
    columns = [
        "FILE_NAME", "ADDRESSLINE_TO", "RECIPIENT_TYPE", "RECIPIENT",
        "LETTER_REG_NUMBER", "LETTER_TITLE", "MAILCATEGORY",
        "ADDRESSLINE_RETURN", "NOTIFICATIONTYPE", "NO_RETURN",
    ]
    for col, name in enumerate(columns):
        ws.write(0, col, name)

    return_address = (coop.postal_address or coop.legal_address or "") if coop else ""
    for row, dispatch in enumerate(dispatches, start=1):
        person = dispatch.person
        address = person.residence_address or person.registration_address or ""
        ws.write(row, 0, f"{dispatch.reg_number}.pdf")
        ws.write(row, 1, address)
        ws.write(row, 2, 0)
        ws.write(row, 3, person.full_name)
        ws.write(row, 4, dispatch.reg_number)
        ws.write(row, 5, dispatch.title)
        ws.write(row, 6, 1)
        ws.write(row, 7, return_address)
        ws.write(row, 8, "E")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_export_zip(dispatches: list[PostalDispatch], coop: Cooperative | None) -> bytes:
    """Реестр .xls + PDF-письма (имена файлов = FILE_NAME из реестра) —
    один ZIP-архив, готовый для загрузки в otpravka.pochta.ru."""
    xls_bytes = build_registry_workbook(dispatches, coop)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("reestr.xls", xls_bytes)
        for dispatch in dispatches:
            if dispatch.document and dispatch.document.file_path:
                pdf_path = os.path.join(current_app.config["UPLOAD_FOLDER"], dispatch.document.file_path)
                if os.path.exists(pdf_path):
                    zf.write(pdf_path, arcname=f"{dispatch.reg_number}.pdf")
    return buf.getvalue()


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_view():
    status_filter = request.args.get("status") or None
    query = database.db_session.query(PostalDispatch).order_by(PostalDispatch.created_at.desc())
    if status_filter:
        try:
            query = query.filter(PostalDispatch.status == PostalDispatchStatus(status_filter))
        except ValueError:
            status_filter = None
    dispatches = query.all()
    return render_template(
        "postal_letters/list.html", dispatches=dispatches, statuses=PostalDispatchStatus, status_filter=status_filter,
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def new():
    if request.method == "GET":
        debtors = list_debtor_persons()
        return render_template("postal_letters/new.html", debtors=debtors, categories=debt_categories())

    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("postal_letters.new"))

    selected = parse_selected_categories(request.form)
    coop, chairman = _coop_and_chairman()
    today = dt.date.today()
    created = 0
    for person in persons:
        pdf_bytes = _render_debt_notice_pdf(person, coop, chairman, today, selected)
        if pdf_bytes is None:
            database.db_session.rollback()
            return redirect(url_for("postal_letters.new"))

        title = _("Уведомление о задолженности — {name}", name=person.full_name)
        dispatch = PostalDispatch(
            person_id=person.id, reg_number="", title=title,
            status=PostalDispatchStatus.DRAFT, created_by_user_id=g.user.id,
        )
        database.db_session.add(dispatch)
        database.db_session.flush()
        dispatch.reg_number = f"ГСК-{dispatch.id}"

        stored_name = _save_generated_pdf(pdf_bytes)
        document = Document(
            doc_type=DocumentType.LETTER, date=today, title=title,
            file_path=stored_name, file_name=f"{dispatch.reg_number}.pdf", is_internal=True,
        )
        database.db_session.add(document)
        database.db_session.flush()
        dispatch.document_id = document.id

        audit.record(
            "postal_letter.create", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Сформировано заказное письмо «{title}»",
        )
        created += 1

    database.db_session.commit()
    flash(_("Сформировано писем: {n}. Теперь скачайте пакет и загрузите его в otpravka.pochta.ru.", n=created), "success")
    return redirect(url_for("postal_letters.list_view"))


@bp.route("/export", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def export():
    dispatch_ids = [int(x) for x in request.form.getlist("dispatch_id")]
    if not dispatch_ids:
        flash(_("Выберите хотя бы одно письмо для выгрузки."), "danger")
        return redirect(url_for("postal_letters.list_view"))

    dispatches = (
        database.db_session.query(PostalDispatch)
        .filter(PostalDispatch.id.in_(dispatch_ids), PostalDispatch.status == PostalDispatchStatus.DRAFT)
        .order_by(PostalDispatch.id)
        .all()
    )
    if not dispatches:
        flash(_("Среди выбранного нет писем в статусе «черновик» — возможно, они уже выгружены."), "danger")
        return redirect(url_for("postal_letters.list_view"))

    coop = database.db_session.query(Cooperative).first()
    zip_bytes = build_export_zip(dispatches, coop)

    now = dt.datetime.utcnow()
    for dispatch in dispatches:
        dispatch.status = PostalDispatchStatus.EXPORTED
        dispatch.exported_at = now
        audit.record(
            "postal_letter.export", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Письмо «{dispatch.title}» включено в пакет для otpravka.pochta.ru",
        )
    database.db_session.commit()

    filename = f"otpravka_{now.strftime('%Y%m%d_%H%M%S')}.zip"
    return Response(
        zip_bytes, mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.route("/<int:dispatch_id>/mark-sent", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def mark_sent(dispatch_id):
    dispatch = database.db_session.get(PostalDispatch, dispatch_id)
    if dispatch is None:
        abort(404)
    dispatch.status = PostalDispatchStatus.SENT
    dispatch.sent_at = dt.datetime.utcnow()
    audit.record(
        "postal_letter.sent", entity_type="postal_dispatch", entity_id=dispatch.id,
        summary=f"Письмо «{dispatch.title}» отмечено как загруженное в otpravka.pochta.ru",
    )
    database.db_session.commit()
    flash(_("Отмечено как отправленное."), "success")
    return redirect(url_for("postal_letters.list_view"))


@bp.route("/<int:dispatch_id>/mark-delivered", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def mark_delivered(dispatch_id):
    dispatch = database.db_session.get(PostalDispatch, dispatch_id)
    if dispatch is None:
        abort(404)
    tracking_number = (request.form.get("tracking_number") or "").strip()
    dispatch.tracking_number = tracking_number or None
    dispatch.status = PostalDispatchStatus.DELIVERED
    dispatch.delivered_at = dt.datetime.utcnow()
    audit.record(
        "postal_letter.delivered", entity_type="postal_dispatch", entity_id=dispatch.id,
        summary=f"Письмо «{dispatch.title}» отмечено как доставленное (ШПИ {tracking_number or '—'})",
    )
    database.db_session.commit()
    flash(_("Отмечено как доставленное."), "success")
    return redirect(url_for("postal_letters.list_view"))


@bp.route("/<int:dispatch_id>/mark-failed", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def mark_failed(dispatch_id):
    dispatch = database.db_session.get(PostalDispatch, dispatch_id)
    if dispatch is None:
        abort(404)
    comment = (request.form.get("comment") or "").strip()
    dispatch.comment = comment or None
    dispatch.status = PostalDispatchStatus.FAILED
    audit.record(
        "postal_letter.failed", entity_type="postal_dispatch", entity_id=dispatch.id,
        summary=f"Письмо «{dispatch.title}» отмечено как неудачное ({comment or 'без комментария'})",
    )
    database.db_session.commit()
    flash(_("Отмечено как неудачное."), "success")
    return redirect(url_for("postal_letters.list_view"))
