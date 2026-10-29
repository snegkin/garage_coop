"""
Заказные письма через Почту России (`/postal-letters/`) — уведомления о
задолженности как электронные заказные письма (ЭЗП).

Изучены три способа подключения к Почте России (см. temp/ в корне
репозитория и ezp_agent/docs/ — инструкция ИС ЭПС):

1. ИС ЭПС «Система-Система» — прямой REST API (POST /api/v2.3/letters):
   PDF + отсоединённая подпись PDF (PKCS#7 DER) по двустороннему ГОСТ-TLS
   через stunnel КриптоПро. Сначала считалось, что для TLS нужен отдельный
   сертификат, но подходит обычный КЭП кооператива от ФНС: у него есть
   EKU Client Authentication (1.3.6.1.5.5.7.3.2) и встроенная лицензия
   КриптоПро CSP. Это ОСНОВНОЙ канал. Ключ КЭП неэкспортируемый и лежит
   на Рутокене у председателя, а сайт — на VPS, поэтому и подпись, и
   туннель до Почты делает агент ЭЗП на ПК председателя (ezp_agent/): он
   сам забирает с сайта очередь (QUEUED) и метаданные письма
   (build_common_info), подписывает PDF, отправляет в Почту и возвращает
   на сайт requestCode, а потом статусы и ШПИ (blueprint agent_bp ниже).
   ID и токен отправителя от Почты есть только в конфиге агента.
2. REST API «Отправка» (otpravka-api.pochta.ru) — у кооператива уже есть
   токен приложения, изучена официальная спецификация (temp/pochta/).
   Проверено предметно: этот API целиком про ОБЫЧНЫЕ отправления с
   бумажной печатью (создание заказов, партии, печатные ярлыки, расчёт
   стоимости). Поля с "уведомлением о вручении" — это подтверждение
   доставки бумажного письма, а не электронная доставка текста. Отдельного
   денежного баланса лицевого счёта в документации тоже нет. Настоящий
   функционал ЭЗП виден в GET /1.0/settings как erl-customer-id/
   erl-customer-token — подключение к системе из п.1, не отдельный метод
   этого API. Этот API не подходит.
3. Личный кабинет otpravka.pochta.ru (веб-интерфейс) — можно вручную
   загрузить Excel-реестр + PDF-письма, подписанные КЭП кооператива.
   ЗАПАСНОЙ канал на случай, когда агент недоступен: модуль готовит пакет
   (реестр .xls + PDF по каждому письму, см. build_registry_workbook/
   export), а статусы (отправлено/доставлено, ШПИ) правление переносит в
   систему вручную по данным того же личного кабинета.

Формат колонок реестра — точно по temp/Инструкция.docx (поля
FILE_NAME/ADDRESSLINE_TO/RECIPIENT_TYPE/.../NOTIFICATIONTYPE) и образцам
temp/*.xls. Файл обязательно в старом формате .xls (Excel 97-2003/BIFF,
через xlwt) — везде в материалах Почты фигурирует только он, не .xlsx.

Первая версия — только типовые уведомления о задолженности (тот же текст,
что и у app.legal_docs.debt_notice, тот же список должников из
list_debtor_persons). Письма произвольного содержания — не в этой задаче.
"""
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import uuid
import zipfile
from decimal import Decimal
from types import SimpleNamespace

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, Response, current_app, g, jsonify, send_file,
)

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .persons import build_statement
from .legal_docs import (
    build_yearly_debt_breakdown, parse_selected_categories, debt_notice_default_intro, debt_notice_default_closing,
    default_debt_categories,
)
from .models import (
    Person, Cooperative, RoleEnum, Document, DocumentType, PostalDispatch, PostalDispatchStatus, PostalLetterCounter,
    PostalApiSettings,
)

bp = Blueprint("postal_letters", __name__, url_prefix="/postal-letters")
# API для агента ЭЗП — отдельный blueprint: без входа на сайт и без CSRF
# (csrf.exempt в app/__init__.py), авторизация — Bearer-токен агента.
agent_bp = Blueprint("postal_agent", __name__, url_prefix="/postal-letters/agent")

_STATUS_LABELS_RAW = {
    PostalDispatchStatus.DRAFT: "Черновик",
    PostalDispatchStatus.EXPORTED: "Выгружено",
    PostalDispatchStatus.QUEUED: "В очереди на отправку",
    PostalDispatchStatus.SENT: "Отправлено",
    PostalDispatchStatus.DELIVERED: "Доставлено",
    PostalDispatchStatus.FAILED: "Не доставлено",
}
_STATUS_BADGES = {
    PostalDispatchStatus.DRAFT: "bg-secondary",
    PostalDispatchStatus.EXPORTED: "bg-info text-dark",
    PostalDispatchStatus.QUEUED: "bg-warning text-dark",
    PostalDispatchStatus.SENT: "bg-primary",
    PostalDispatchStatus.DELIVERED: "bg-success",
    PostalDispatchStatus.FAILED: "bg-danger",
}

# Статусы ИС ЭПС (GET /letters/{requestCode}/status, п. 6.2 инструкции),
# после которых опрашивать больше нечего.
API_FINAL_STATUSES = ("ONLINE_DELIVERED", "DELIVERED_TO_PRINT", "FAILED")
_API_STATUS_LABELS_RAW = {
    "UNKNOWN": "Почта ещё не видит письмо",
    "ACCEPTED": "Принято Почтой",
    "IN_PROGRESS": "Обрабатывается Почтой",
    "ONLINE_DELIVERED": "Доставлено в электронном виде",
    "DELIVERED_TO_PRINT": "Передано Почтой на печать",
    "FAILED": "Отклонено Почтой",
}
# Пока письмо в IN_PROGRESS, Почта просит спрашивать статус не чаще раза в
# 30 минут (п. 7.6.1); до этого (ACCEPTED, только что отправлено) — на
# каждом проходе агента, он сам ходит раз в несколько минут.
API_POLL_INTERVAL_IN_PROGRESS = dt.timedelta(minutes=30)
# Агент молчит дольше — на странице писем предупреждение.
AGENT_STALE_AFTER = dt.timedelta(hours=1)

# Тестовые получатели для интеграционного тестирования ИС ЭПС (п. 6.12.1.8
# инструкции) — ровно эти три, Почта проверяет по ним.
INTEGRATION_TEST_RECIPIENTS = (
    {"name": "Кузнецов Кирилл Викторович", "address": "644077, Омская обл., г. Омск, пр. Мира, дом 57, корп. 1, кв. 66."},
    {"name": "Николаев Игорь Юрьевич", "address": "644077, Российская Федерация, обл Омская, г Омск, пр-кт Мира, д. 57, кв. 66."},
    {"name": "Тестовый ЮЛ ПР 3", "inn": "7700000051", "address": "622001, Свердловская обл., г. Нижний Тагил, ул. Красноармейская, д. 42."},
)


def status_label(status: PostalDispatchStatus) -> str:
    return _(_STATUS_LABELS_RAW[status])


def status_badge(status: PostalDispatchStatus) -> str:
    return _STATUS_BADGES[status]


def api_status_label(api_status: str | None) -> str:
    if not api_status:
        return ""
    raw = _API_STATUS_LABELS_RAW.get(api_status)
    return _(raw) if raw else api_status


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
    intro_text: str | None = None, closing_text: str | None = None,
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
        "intro_text": intro_text, "closing_text": closing_text,
    }]
    return _render_letter_pdf(docs, coop, chairman, today)


def _render_letter_pdf(docs: list[dict], coop, chairman, today: dt.date) -> bytes | None:
    """PDF/A-1b — ИС ЭПС принимает только PDF/A-1 со встроенными шрифтами
    (п. 7.5 инструкции); для ручной загрузки в ЛК Почты он тоже годится."""
    html_str = render_template(
        "legal_docs/debt_notice_pdf.html", docs=docs, coop=coop, chairman=chairman, today=today, hide_chat_widgets=True,
    )
    try:
        import weasyprint
    except ImportError:
        flash(_("Для формирования PDF нужна библиотека weasyprint. Установите: pip install weasyprint"), "danger")
        return None
    return weasyprint.HTML(string=html_str, base_url=request.url_root).write_pdf(pdf_variant="pdf/a-1b")


def _save_generated_pdf(pdf_bytes: bytes) -> str:
    stored_name = f"{uuid.uuid4().hex}.pdf"
    with open(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name), "wb") as f:
        f.write(pdf_bytes)
    return stored_name


def next_reg_number(year: int) -> str:
    """Номер нового письма — «<год>-<порядковый номер в этом году>». Счётчик
    — в PostalLetterCounter, а не по уже существующим письмам: удалённое
    письмо могло уйти в ЛК Почты под своим номером, повторять его нельзя.
    Без commit — номер «тратится» вместе с созданием письма в той же
    транзакции. Если счётчика за год ещё нет — начинаем после самого
    большого номера этого года среди писем (на случай писем, созданных до
    появления счётчика)."""
    counter = database.db_session.get(PostalLetterCounter, year)
    if counter is None:
        prefix = f"{year}-"
        existing = [
            int(reg[len(prefix):]) for (reg,) in
            database.db_session.query(PostalDispatch.reg_number).filter(PostalDispatch.reg_number.like(f"{prefix}%"))
            if reg[len(prefix):].isdigit()
        ]
        counter = PostalLetterCounter(year=year, last_number=max(existing, default=0))
        database.db_session.add(counter)
    counter.last_number += 1
    return f"{year}-{counter.last_number}"


def dispatch_categories(dispatch: PostalDispatch) -> set[str]:
    """Виды задолженности, на которые формировалось письмо; у писем,
    созданных до появления поля, — набор по умолчанию."""
    if dispatch.categories:
        try:
            return set(json.loads(dispatch.categories))
        except ValueError:
            pass
    return default_debt_categories()


def _regenerate_pdf(dispatch: PostalDispatch) -> bool:
    """Пересобирает PDF письма (после правки текста) на сегодняшнюю дату —
    старый файл удаляется, Document переписывается на новый."""
    coop, chairman = _coop_and_chairman()
    today = dt.date.today()
    pdf_bytes = _render_debt_notice_pdf(
        dispatch.person, coop, chairman, today, dispatch_categories(dispatch),
        dispatch.intro_text, dispatch.closing_text,
    )
    if pdf_bytes is None:
        return False
    stored_name = _save_generated_pdf(pdf_bytes)
    document = dispatch.document
    if document is None:
        document = Document(
            doc_type=DocumentType.LETTER, date=today, title=dispatch.title,
            file_path=stored_name, file_name=f"{dispatch.reg_number}.pdf", is_internal=True,
        )
        database.db_session.add(document)
        database.db_session.flush()
        dispatch.document_id = document.id
    else:
        _remove_upload(document.file_path)
        document.file_path = stored_name
        document.date = today
    return True


def _remove_upload(stored_name: str | None) -> None:
    if not stored_name:
        return
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)
    if os.path.exists(path):
        os.remove(path)


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

    return_address = _return_address(coop)
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


def _return_address(coop: Cooperative | None) -> str:
    return (coop.postal_address or coop.legal_address or "") if coop else ""


def _recipient_address(person: Person) -> str:
    return person.residence_address or person.registration_address or ""


def split_full_name(full_name: str) -> tuple[str, str, str | None]:
    """«Фамилия Имя Отчество» → части для recipient.person в метаданных ИС
    ЭПС. Всё после имени — отчество (бывает из нескольких слов: «… оглы»)."""
    parts = full_name.split()
    last = parts[0] if parts else ""
    first = parts[1] if len(parts) > 1 else ""
    patronymic = " ".join(parts[2:]) or None
    return last, first, patronymic


def get_api_settings() -> PostalApiSettings:
    settings = database.db_session.query(PostalApiSettings).first()
    if settings is None:
        settings = PostalApiSettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


def _common_info(
    *, document_id: str, reg_number: str, reg_date: dt.date, title: str, recipient_name: str, recipient_address: str,
    recipient_inn: str | None, coop: Cooperative | None, sender_id: str,
) -> dict:
    """Метаданные письма (commonInfo) для POST /api/v2.3/letters — по п. 6.1
    и образцу 6.10.2 инструкции ИС ЭПС. Заказное (REGISTERED) с электронным
    уведомлением о вручении (postMark 16384 — как NOTIFICATIONTYPE=E в
    реестре для ЛК), разряд 0 — не судебное и не административное. ШПИ
    (mailId) не передаём — его присваивает Почта и возвращает в статусе.
    Получатель и отправитель — те же, что в самом PDF: Почта требует, чтобы
    метаданные совпадали с адресными блоками письма."""
    sender_code = f"CODE_{sender_id}"
    sender_name = (coop.full_name if coop else "") or ""
    if recipient_inn:
        recipient_party = {"organization": {"name": recipient_name, "inn": recipient_inn}}
    else:
        last, first, patronymic = split_full_name(recipient_name)
        person = {"lastName": last, "firstName": first}
        if patronymic:
            person["patronymic"] = patronymic
        recipient_party = {"person": person}
    return {
        "type": "REGISTERED",
        "mailRank": 0,
        "postMark": 16384,
        "attachments": {"pdf": {"attachmentId": "letter.pdf", "attachmentSignatureId": "letter.pdf.sig"}},
        "document": {
            "id": document_id,
            "registrationDate": f"{reg_date.isoformat()}T00:00:00",
            "registrationNumber": reg_number,
            "title": title,
        },
        "sender": {
            "code": sender_code, "name": sender_name,
            "departmentCode": sender_code, "departmentName": sender_name,
            "address": _return_address(coop),
        },
        "recipient": {"countryCodeOKSM": 643, "address": recipient_address, **recipient_party},
    }


def build_common_info(dispatch: PostalDispatch, coop: Cooperative | None, settings: PostalApiSettings) -> dict:
    """commonInfo для письма из журнала. document.id — «gsk-<id письма>»:
    Почта отбивает повтор documentId (ошибка 42), так что случайная
    повторная отправка того же письма агентом не уйдёт адресату дважды."""
    reg_date = dispatch.document.date if dispatch.document else dispatch.created_at.date()
    return _common_info(
        document_id=f"gsk-{dispatch.id}", reg_number=dispatch.reg_number, reg_date=reg_date, title=dispatch.title,
        recipient_name=dispatch.person.full_name, recipient_address=_recipient_address(dispatch.person),
        recipient_inn=None, coop=coop, sender_id=settings.sender_id or "",
    )


_POSTAL_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


def api_problems(dispatch: PostalDispatch, settings: PostalApiSettings) -> list[str]:
    """Почему письмо нельзя ставить в очередь на отправку через API — то,
    на чём ИС ЭПС точно отобьёт письмо (адрес проверяется по ФИАС, без
    индекса — ошибка 61), лучше поймать до отправки."""
    problems = []
    if not settings.sender_id:
        problems.append(_("не задан ID отправителя ИС ЭПС (настройки API)"))
    if dispatch.document is None or not dispatch.document.file_path:
        problems.append(_("нет PDF письма"))
    last, first, _patronymic = split_full_name(dispatch.person.full_name)
    if not (last and first):
        problems.append(_("в ФИО нет фамилии и имени"))
    address = _recipient_address(dispatch.person)
    if not address:
        problems.append(_("нет адреса"))
    elif not _POSTAL_CODE_RE.search(address):
        problems.append(_("в адресе нет почтового индекса"))
    return problems


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
    settings = database.db_session.query(PostalApiSettings).first()
    api_configured = bool(settings and settings.sender_id and settings.agent_token_hash)
    last_seen = settings.agent_last_seen_at if settings else None
    agent_stale = api_configured and (last_seen is None or dt.datetime.utcnow() - last_seen > AGENT_STALE_AFTER)
    return render_template(
        "postal_letters/list.html", dispatches=dispatches, statuses=PostalDispatchStatus, status_filter=status_filter,
        api_configured=api_configured, agent_minutes_ago=_minutes_ago(last_seen), agent_stale=agent_stale,
    )


def _minutes_ago(moment: dt.datetime | None) -> int | None:
    return None if moment is None else int((dt.datetime.utcnow() - moment).total_seconds() // 60)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def new():
    if request.method == "GET":
        return redirect(url_for("legal_docs.debtors"))  # выбор должников — общая страница

    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("legal_docs.debtors"))

    selected = parse_selected_categories(request.form)
    coop, chairman = _coop_and_chairman()
    today = dt.date.today()
    created = 0
    for person in persons:
        pdf_bytes = _render_debt_notice_pdf(person, coop, chairman, today, selected)
        if pdf_bytes is None:
            database.db_session.rollback()
            return redirect(url_for("legal_docs.debtors"))

        title = _("Уведомление о задолженности — {name}", name=person.full_name)
        dispatch = PostalDispatch(
            person_id=person.id, reg_number=next_reg_number(today.year), title=title,
            status=PostalDispatchStatus.DRAFT, created_by_user_id=g.user.id,
            categories=json.dumps(sorted(selected)),
        )
        database.db_session.add(dispatch)
        database.db_session.flush()

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
    flash(_("Сформировано писем: {n}. Проверьте текст черновиков, затем отправьте их через Почту (API) или скачайте пакет для otpravka.pochta.ru.", n=created), "success")
    return redirect(url_for("postal_letters.list_view"))


# Пакет можно скачать и повторно — для уже выгруженных писем (файлы
# потерялись при подписи и т.п.); отправленные/доставленные в пакет больше
# не попадают — они уже ушли через Почту.
EXPORTABLE_STATUSES = (PostalDispatchStatus.DRAFT, PostalDispatchStatus.EXPORTED)
# Удалить/вернуть в черновики можно, пока письмо не загружено в ЛК Почты.
EDITABLE_STATUSES = (PostalDispatchStatus.DRAFT, PostalDispatchStatus.EXPORTED)


def _selected_dispatches(statuses) -> list[PostalDispatch]:
    dispatch_ids = [int(x) for x in request.form.getlist("dispatch_id") if x.isdigit()]
    if not dispatch_ids:
        return []
    return (
        database.db_session.query(PostalDispatch)
        .filter(PostalDispatch.id.in_(dispatch_ids), PostalDispatch.status.in_(statuses))
        .order_by(PostalDispatch.id)
        .all()
    )


@bp.route("/export", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def export():
    dispatches = _selected_dispatches(EXPORTABLE_STATUSES)
    if not dispatches:
        flash(_("Отметьте письма в статусе «черновик» или «выгружено» — для них можно скачать пакет."), "danger")
        return redirect(url_for("postal_letters.list_view"))

    coop = database.db_session.query(Cooperative).first()
    zip_bytes = build_export_zip(dispatches, coop)

    now = dt.datetime.utcnow()
    for dispatch in dispatches:
        again = dispatch.status == PostalDispatchStatus.EXPORTED
        dispatch.status = PostalDispatchStatus.EXPORTED
        dispatch.exported_at = now
        audit.record(
            "postal_letter.export", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Письмо {dispatch.reg_number} «{dispatch.title}» "
                    f"{'повторно включено' if again else 'включено'} в пакет для otpravka.pochta.ru",
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


@bp.route("/to-draft", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def to_draft():
    """Выгруженные письма — обратно в черновики (поправить текст и выгрузить
    заново). Скачанный раньше пакет с ними после этого использовать не надо."""
    dispatches = _selected_dispatches((PostalDispatchStatus.EXPORTED,))
    if not dispatches:
        flash(_("Отметьте выгруженные письма, которые нужно вернуть в черновики."), "danger")
        return redirect(url_for("postal_letters.list_view"))
    for dispatch in dispatches:
        dispatch.status = PostalDispatchStatus.DRAFT
        dispatch.exported_at = None
        audit.record(
            "postal_letter.to_draft", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Письмо {dispatch.reg_number} «{dispatch.title}» возвращено в черновики",
        )
    database.db_session.commit()
    flash(_("Возвращено в черновики: {n}.", n=len(dispatches)), "success")
    return redirect(url_for("postal_letters.list_view"))


@bp.route("/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete():
    """Удаление черновиков и выгруженных писем — вместе с PDF. Загруженные в
    ЛК Почты, доставленные и недоставленные не удаляются: это след реально
    отправленного письма."""
    dispatches = _selected_dispatches(EDITABLE_STATUSES)
    if not dispatches:
        flash(_("Удалить можно только черновики и выгруженные письма — отметьте их."), "danger")
        return redirect(url_for("postal_letters.list_view"))
    for dispatch in dispatches:
        audit.record(
            "postal_letter.delete", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Удалено письмо {dispatch.reg_number} «{dispatch.title}»",
        )
        document = dispatch.document
        database.db_session.delete(dispatch)
        if document is not None:
            _remove_upload(document.file_path)
            database.db_session.delete(document)
    database.db_session.commit()
    flash(_("Удалено писем: {n}.", n=len(dispatches)), "success")
    return redirect(url_for("postal_letters.list_view"))


@bp.route("/<int:dispatch_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(dispatch_id):
    """Текст черновика письма — вводный и заключительный абзацы (таблицы с
    расчётом долга не редактируются — они считаются). Сохранение пересобирает
    PDF на сегодняшнюю дату. Только черновики: выгруженное письмо сначала
    вернуть в черновики (to_draft) — иначе разошлось бы со скачанным пакетом."""
    dispatch = database.db_session.get(PostalDispatch, dispatch_id)
    if dispatch is None:
        abort(404)
    if dispatch.status != PostalDispatchStatus.DRAFT:
        flash(_("Править можно только черновик — выгруженное письмо сначала верните в черновики."), "warning")
        return redirect(url_for("postal_letters.list_view"))

    coop, _chairman = _coop_and_chairman()
    today = dt.date.today()
    default_intro = debt_notice_default_intro(coop, today)
    default_closing = debt_notice_default_closing()

    if request.method == "POST":
        if request.form.get("reset"):
            dispatch.intro_text = dispatch.closing_text = None
        else:
            intro = (request.form.get("intro_text") or "").strip()
            closing = (request.form.get("closing_text") or "").strip()
            # Совпадает со стандартным (или пусто) — храним NULL: стандартный
            # текст подставляется на дату пересборки, а не застывает.
            dispatch.intro_text = intro if intro and intro != default_intro else None
            dispatch.closing_text = closing if closing and closing != default_closing else None
        if not _regenerate_pdf(dispatch):
            database.db_session.rollback()
            return redirect(url_for("postal_letters.edit", dispatch_id=dispatch.id))
        audit.record(
            "postal_letter.edit", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Изменён текст письма {dispatch.reg_number} «{dispatch.title}»",
        )
        database.db_session.commit()
        flash(_("Текст письма сохранён, PDF пересобран."), "success")
        return redirect(url_for("postal_letters.list_view"))

    return render_template(
        "postal_letters/edit.html", dispatch=dispatch,
        intro_text=dispatch.intro_text or default_intro, closing_text=dispatch.closing_text or default_closing,
        is_custom=bool(dispatch.intro_text or dispatch.closing_text),
    )



# ---------------------------------------------------------------------------
# Отправка через API ИС ЭПС (агент ЭЗП, см. ezp_agent/)
# ---------------------------------------------------------------------------

QUEUEABLE_STATUSES = (PostalDispatchStatus.DRAFT, PostalDispatchStatus.EXPORTED)


@bp.route("/queue", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def queue():
    """Отмеченные черновики/выгруженные — в очередь агенту ЭЗП. Письма, которые
    ИС ЭПС заведомо отобьёт (api_problems), остаются на месте с причиной."""
    dispatches = _selected_dispatches(QUEUEABLE_STATUSES)
    if not dispatches:
        flash(_("Отметьте черновики или выгруженные письма — их можно отправить через Почту."), "danger")
        return redirect(url_for("postal_letters.list_view"))
    settings = get_api_settings()
    now = dt.datetime.utcnow()
    queued = 0
    for dispatch in dispatches:
        problems = api_problems(dispatch, settings)
        if problems:
            flash(_("Письмо {reg} ({name}) не поставлено в очередь: {problems}.",
                    reg=dispatch.reg_number, name=dispatch.person.full_name, problems="; ".join(problems)), "warning")
            continue
        dispatch.status = PostalDispatchStatus.QUEUED
        dispatch.queued_at = now
        audit.record(
            "postal_letter.queue", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Письмо {dispatch.reg_number} «{dispatch.title}» поставлено в очередь на отправку через API Почты",
        )
        queued += 1
    database.db_session.commit()
    if queued:
        flash(_("Поставлено в очередь: {n}. Агент ЭЗП на компьютере председателя подпишет и отправит их при следующем проходе.", n=queued), "success")
    return redirect(url_for("postal_letters.list_view"))


@bp.route("/unqueue", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def unqueue():
    """Из очереди — обратно в черновики, пока агент их не забрал."""
    dispatches = _selected_dispatches((PostalDispatchStatus.QUEUED,))
    if not dispatches:
        flash(_("Отметьте письма в очереди на отправку."), "danger")
        return redirect(url_for("postal_letters.list_view"))
    for dispatch in dispatches:
        dispatch.status = PostalDispatchStatus.DRAFT
        dispatch.queued_at = None
        audit.record(
            "postal_letter.unqueue", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Письмо {dispatch.reg_number} «{dispatch.title}» убрано из очереди на отправку",
        )
    database.db_session.commit()
    flash(_("Убрано из очереди: {n}.", n=len(dispatches)), "success")
    return redirect(url_for("postal_letters.list_view"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@bp.route("/api-settings", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def api_settings():
    """ID отправителя ИС ЭПС и токен агента ЭЗП. Новый токен показывается
    один раз — в БД только его sha256; выпуск нового отключает старый."""
    settings = get_api_settings()
    new_token = None
    if request.method == "POST":
        if request.form.get("issue_token"):
            new_token = secrets.token_urlsafe(32)
            settings.agent_token_hash = _hash_token(new_token)
            audit.record("postal_api.issue_token", entity_type="postal_api_settings", entity_id=settings.id,
                         summary="Выпущен новый токен агента ЭЗП")
            flash(_("Новый токен агента выпущен — скопируйте его в конфиг агента сейчас, больше он показан не будет."), "success")
        else:
            settings.sender_id = (request.form.get("sender_id") or "").strip() or None
            audit.record("postal_api.settings", entity_type="postal_api_settings", entity_id=settings.id,
                         summary=f"ID отправителя ИС ЭПС: {settings.sender_id or '—'}")
            flash(_("Настройки сохранены."), "success")
        database.db_session.commit()
        if new_token is None:
            return redirect(url_for("postal_letters.api_settings"))
    else:
        database.db_session.commit()  # get_api_settings мог создать запись
    return render_template("postal_letters/api_settings.html", settings=settings, new_token=new_token)


@bp.route("/api-test-kit")
@roles_required(RoleEnum.CHAIRMAN)
def api_test_kit():
    """Комплект для интеграционного тестирования ИС ЭПС (п. 6.12.1.8): три
    письма тем же шаблоном, что и настоящие, тестовым получателям Почты,
    с commonInfo к каждому. Отправляет агент: ezp_agent.py send-file. В
    журнал писем ничего не пишется — это не письма кооператива."""
    settings = get_api_settings()
    if not settings.sender_id:
        flash(_("Сначала укажите ID отправителя ИС ЭПС."), "danger")
        return redirect(url_for("postal_letters.api_settings"))
    coop, chairman = _coop_and_chairman()
    today = dt.date.today()
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    years = [{"year": today.year - 1, "fee_type": _("Членский взнос"), "charged": Decimal("5000.00"), "paid": Decimal("0")}]
    summary = {"penalty_rows": [], "penalty_total": Decimal("0"), "grand_total": Decimal("-5000.00")}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, recipient in enumerate(INTEGRATION_TEST_RECIPIENTS, start=1):
            person = SimpleNamespace(
                full_name=recipient["name"], residence_address=recipient["address"], registration_address=None,
                passport_series=None, phones=[], email=None, telegram=None, vk=None, max_messenger=None,
            )
            pdf_bytes = _render_letter_pdf(
                [{"person": person, "summary": summary, "years": years, "intro_text": None, "closing_text": None}],
                coop, chairman, today,
            )
            if pdf_bytes is None:
                return redirect(url_for("postal_letters.api_settings"))
            reg_number = f"TEST-{stamp}-{n}"
            info = _common_info(
                document_id=f"gsk-test-{stamp}-{n}", reg_number=reg_number, reg_date=today,
                title=_("Уведомление о задолженности — {name}", name=recipient["name"]),
                recipient_name=recipient["name"], recipient_address=recipient["address"],
                recipient_inn=recipient.get("inn"), coop=coop, sender_id=settings.sender_id,
            )
            zf.writestr(f"test{n}/letter.pdf", pdf_bytes)
            zf.writestr(f"test{n}/commonInfo.json", json.dumps(info, ensure_ascii=False, indent=2))
    return Response(
        buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=ezp_test_{stamp}.zip"},
    )


# --- API агента ЭЗП ---------------------------------------------------------

def _agent_auth() -> PostalApiSettings | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    settings = database.db_session.query(PostalApiSettings).first()
    if settings is None or not settings.agent_token_hash:
        return None
    if not hmac.compare_digest(_hash_token(header[len("Bearer "):].strip()), settings.agent_token_hash):
        return None
    settings.agent_last_seen_at = dt.datetime.utcnow()
    return settings


def _agent_unauthorized():
    return jsonify({"error": "unauthorized"}), 401


def _needs_poll(dispatch: PostalDispatch, now: dt.datetime) -> bool:
    if dispatch.api_status in API_FINAL_STATUSES:
        return False
    if dispatch.api_status == "IN_PROGRESS" and dispatch.api_checked_at:
        return now - dispatch.api_checked_at >= API_POLL_INTERVAL_IN_PROGRESS
    return True


@agent_bp.route("/jobs")
def agent_jobs():
    """Что агенту сделать за проход: send — письма из очереди с готовыми
    метаданными (commonInfo) и путём к PDF; poll — отправленные через API
    письма без финального статуса, которым пора спросить статус у Почты."""
    settings = _agent_auth()
    if settings is None:
        return _agent_unauthorized()
    coop = database.db_session.query(Cooperative).first()
    queued = (
        database.db_session.query(PostalDispatch)
        .filter(PostalDispatch.status == PostalDispatchStatus.QUEUED).order_by(PostalDispatch.id).all()
    )
    send = [
        {
            "id": d.id, "reg_number": d.reg_number, "common_info": build_common_info(d, coop, settings),
            "pdf_path": url_for("postal_agent.agent_job_pdf", dispatch_id=d.id),
        }
        for d in queued if d.document and d.document.file_path
    ]
    now = dt.datetime.utcnow()
    sent = (
        database.db_session.query(PostalDispatch)
        .filter(PostalDispatch.api_request_code.isnot(None),
                PostalDispatch.status.in_((PostalDispatchStatus.SENT, PostalDispatchStatus.DELIVERED)))
        .order_by(PostalDispatch.id).all()
    )
    poll = [{"id": d.id, "request_code": d.api_request_code} for d in sent if _needs_poll(d, now)]
    database.db_session.commit()  # agent_last_seen_at
    return jsonify({"send": send, "poll": poll})


@agent_bp.route("/jobs/<int:dispatch_id>/pdf")
def agent_job_pdf(dispatch_id):
    settings = _agent_auth()
    if settings is None:
        return _agent_unauthorized()
    database.db_session.commit()
    dispatch = database.db_session.get(PostalDispatch, dispatch_id)
    if dispatch is None or dispatch.status != PostalDispatchStatus.QUEUED or dispatch.document is None:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], dispatch.document.file_path)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/pdf", download_name="letter.pdf")


@agent_bp.route("/jobs/<int:dispatch_id>/result", methods=["POST"])
def agent_job_result(dispatch_id):
    """Итог отправки письма из очереди: {"request_code": "req_…"} — Почта
    приняла запрос; {"error": "…"} — отказала сразу (4xx, ошибка подписи)."""
    settings = _agent_auth()
    if settings is None:
        return _agent_unauthorized()
    dispatch = database.db_session.get(PostalDispatch, dispatch_id)
    if dispatch is None:
        database.db_session.commit()
        abort(404)
    if dispatch.status != PostalDispatchStatus.QUEUED:
        database.db_session.commit()
        return jsonify({"error": "not queued"}), 409
    data = request.get_json(silent=True) or {}
    now = dt.datetime.utcnow()
    request_code = (data.get("request_code") or "").strip()
    if request_code:
        dispatch.status = PostalDispatchStatus.SENT
        dispatch.sent_at = now
        dispatch.api_request_code = request_code[:64]
        dispatch.api_status = None
        dispatch.api_checked_at = now
        summary = f"Письмо {dispatch.reg_number} «{dispatch.title}» отправлено через API Почты ({dispatch.api_request_code})"
    else:
        dispatch.status = PostalDispatchStatus.FAILED
        dispatch.comment = (data.get("error") or "").strip() or "Агент ЭЗП не смог отправить письмо"
        summary = f"Письмо {dispatch.reg_number} «{dispatch.title}» не отправлено через API Почты: {dispatch.comment}"
    audit.record("postal_letter.api_send", entity_type="postal_dispatch", entity_id=dispatch.id, summary=summary)
    database.db_session.commit()
    return jsonify({"ok": True})


@agent_bp.route("/jobs/<int:dispatch_id>/status", methods=["POST"])
def agent_job_status(dispatch_id):
    """Ответ Почты на GET /letters/{requestCode}/status — data как есть
    ({type, mailId, description, errorCode}). ONLINE_DELIVERED — доставлено
    в электронном виде; DELIVERED_TO_PRINT — Почта напечатала и отправила
    бумажным письмом, для нас это конец опроса, но не «доставлено»."""
    settings = _agent_auth()
    if settings is None:
        return _agent_unauthorized()
    dispatch = database.db_session.get(PostalDispatch, dispatch_id)
    if dispatch is None or not dispatch.api_request_code:
        database.db_session.commit()
        abort(404)
    data = request.get_json(silent=True) or {}
    api_status = (data.get("type") or "").strip()[:32] or None
    now = dt.datetime.utcnow()
    dispatch.api_checked_at = now
    changed = api_status != dispatch.api_status
    dispatch.api_status = api_status
    if data.get("mailId"):
        dispatch.tracking_number = str(data["mailId"])[:20]
    if api_status == "ONLINE_DELIVERED":
        dispatch.status = PostalDispatchStatus.DELIVERED
        dispatch.delivered_at = dispatch.delivered_at or now
    elif api_status == "FAILED":
        dispatch.status = PostalDispatchStatus.FAILED
        description = (data.get("description") or "").strip()
        if data.get("errorCode") is not None:
            description = f"{description} (код {data['errorCode']})".strip()
        dispatch.comment = description or "Почта отклонила письмо"
    if changed:
        audit.record(
            "postal_letter.api_status", entity_type="postal_dispatch", entity_id=dispatch.id,
            summary=f"Письмо {dispatch.reg_number}: статус Почты {api_status or '—'}"
                    f"{f', ШПИ {dispatch.tracking_number}' if dispatch.tracking_number else ''}",
        )
    database.db_session.commit()
    return jsonify({"ok": True})
