"""
Синхронизация расчётного счёта с API банка — баланс, выписка (зачисления и
списания), реестр начислений, реестр платежей. Пока реально работает
только для api_provider == SBERBANK (см. app/bank_api/), для остальных
get_client() вернёт None и роуты покажут понятное сообщение вместо ошибки.

Настройки подключения (client_id/client_secret и т.п.) хранятся отдельно
от самой формы счёта (app/cooperative.py) — см. BankApiCredential в
app/models.py и комментарий там же.
"""
import datetime as dt
import json
import os
import re
import uuid
from decimal import Decimal

from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PrivateFormat, NoEncryption
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, Response

from . import database, audit
from .i18n import translate as _
from .auth import roles_required
from .models import (
    RoleEnum, BankAccount, BankApiCredential, BankApiProvider, BankStatementLine,
    ChargeRegistryBatch, ChargeRegistryStatus, PaymentRegistryEntry, BankRegistryFormat,
    MemberAccount, PersonalAccount, Payment, Person, GarageOwnership, GarageContact,
)
from .accounting import balance as _balance, reallocate_garage_charges, reallocate_member_charges
from .bank_api import get_client, crypto, build_registry_format
from .bank_api.base import BankApiError, ChargeRegistryItem
from .bank_api import registry_file

bp = Blueprint("bank_sync", __name__, url_prefix="/cooperative/bank-accounts/<int:account_id>")


def _get_account(account_id: int) -> BankAccount:
    account = database.db_session.get(BankAccount, account_id)
    if account is None:
        abort(404)
    return account


def _get_or_create_credential(account: BankAccount) -> BankApiCredential:
    if account.api_credential is None:
        account.api_credential = BankApiCredential(bank_account_id=account.id)
        database.db_session.add(account.api_credential)
        database.db_session.flush()
    return account.api_credential


def _persist_rotated_refresh_token(cred: BankApiCredential, client) -> None:
    """Sber API может при обновлении access_token выдать НОВЫЙ refresh_token
    взамен старого (см. SberbankClient.rotated_refresh_token) — если это
    произошло, старый refresh_token банк аннулирует, и следующая попытка
    обновить токен с ним провалится, если не сохранить новый. Вызывать
    после КАЖДОГО использования клиента, полученного через get_client(),
    до commit."""
    rotated = getattr(client, "rotated_refresh_token", None)
    if rotated:
        cred.refresh_token_encrypted = crypto.encrypt(rotated)


def _save_client_cert(cred: BankApiCredential, file_storage, passphrase: str) -> None:
    """
    Принимает клиентский mTLS-сертификат в формате PKCS#12 (.pfx/.p12) —
    именно в этом формате его выдаёт личный кабинет Sber API — и пароль к
    нему; банк требует такой сертификат для самого TLS-соединения при ЛЮБОМ
    обращении к API (см. комментарий в app/bank_api/sberbank.py), это не
    альтернатива токенам, а дополнительное требование.

    Библиотека requests не умеет работать с PKCS#12 напрямую — только с
    парой PEM-файлов (сертификат + незашифрованный приватный ключ), поэтому
    конвертация здесь неизбежна. Сам .pfx-файл и пароль к нему нигде не
    сохраняются — используются только для этой конвертации и сразу
    отбрасываются; на диск (в BANK_CERTS_FOLDER, вне UPLOAD_FOLDER и вне
    досягаемости любого HTTP-роута) попадают уже готовые PEM cert/key.

    Контейнер .p12, сгенерированный в личном кабинете Sber API, обычно
    содержит не только клиентский сертификат, но и всю цепочку
    промежуточных сертификатов — сохраняем её ВСЮ в файл сертификата
    (лист + цепочка, конкатенацией PEM-блоков), а не только лист: mTLS-
    серверы нередко требуют полную цепочку от клиента для валидации, а не
    один листовой сертификат.
    """
    if not file_storage or not file_storage.filename:
        return
    data = file_storage.read()
    try:
        private_key, certificate, extra_certs = pkcs12.load_key_and_certificates(
            data, passphrase.encode("utf-8") if passphrase else None,
        )
    except Exception as e:
        raise ValueError(
            _("Не удалось прочитать файл сертификата (.pfx/.p12) — проверьте пароль и формат файла.")
        ) from e
    if private_key is None or certificate is None:
        raise ValueError(_("Файл сертификата не содержит приватного ключа или самого сертификата."))

    certs_dir = current_app.config["BANK_CERTS_FOLDER"]
    old_cert, old_key = cred.tls_cert_filename, cred.tls_key_filename

    cert_name = f"{uuid.uuid4().hex}.cert.pem"
    key_name = f"{uuid.uuid4().hex}.key.pem"
    with open(os.path.join(certs_dir, cert_name), "wb") as fh:
        fh.write(certificate.public_bytes(Encoding.PEM))
        for extra in (extra_certs or []):
            fh.write(extra.public_bytes(Encoding.PEM))
    key_path = os.path.join(certs_dir, key_name)
    with open(key_path, "wb") as fh:
        fh.write(private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))
    os.chmod(key_path, 0o600)  # приватный ключ — читаемый только процессом приложения

    cred.tls_cert_filename = cert_name
    cred.tls_key_filename = key_name

    # Заменяем — старые файлы сертификата этого счёта (если был) удаляем,
    # чтобы в BANK_CERTS_FOLDER не копились ключи от отозванных сертификатов.
    for old_name in (old_cert, old_key):
        if old_name:
            old_path = os.path.join(certs_dir, old_name)
            if os.path.exists(old_path):
                os.remove(old_path)


# ---------------------------------------------------------------------------
# Формат файлов реестра начислений/платежей — настраиваемый по счёту (см.
# BankRegistryFormat в models.py), тот же принцип UI, что и настраиваемый
# формат CSV-импорта в мастере первого запуска (app/setup_wizard.py):
# каталог полей, чекбокс «есть в файле» + номер позиции, без drag-and-drop.
# ---------------------------------------------------------------------------

def _parse_registry_format_form(catalog) -> list[str]:
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


def _get_or_create_registry_format_row(account: BankAccount) -> BankRegistryFormat:
    if account.registry_format is None:
        account.registry_format = BankRegistryFormat(
            bank_account_id=account.id,
            charge_columns=json.dumps(registry_file.DEFAULT_CHARGE_COLUMNS, ensure_ascii=False),
            payment_columns=json.dumps(registry_file.DEFAULT_PAYMENT_COLUMNS, ensure_ascii=False),
        )
        database.db_session.add(account.registry_format)
        database.db_session.flush()
    return account.registry_format


@bp.route("/registry/format")
@roles_required(RoleEnum.BOARD)
def registry_format_settings(account_id):
    account = _get_account(account_id)
    row = account.registry_format
    active_charge = json.loads(row.charge_columns) if row else registry_file.DEFAULT_CHARGE_COLUMNS
    active_payment = json.loads(row.payment_columns) if row else registry_file.DEFAULT_PAYMENT_COLUMNS
    return render_template(
        "cooperative/registry_format.html", account=account, row=row,
        charge_catalog=registry_file.CHARGE_FIELD_CATALOG, payment_catalog=registry_file.PAYMENT_FIELD_CATALOG,
        active_charge_keys=set(active_charge), charge_positions={k: i + 1 for i, k in enumerate(active_charge)},
        active_payment_keys=set(active_payment), payment_positions={k: i + 1 for i, k in enumerate(active_payment)},
    )


@bp.route("/registry/format/charges", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_charge_registry_format(account_id):
    account = _get_account(account_id)
    columns = _parse_registry_format_form(registry_file.CHARGE_FIELD_CATALOG)
    required = {k for k, _l, r in registry_file.CHARGE_FIELD_CATALOG if r}
    if not required.issubset(set(columns)):
        flash(_("В формате реестра начислений обязательны поля «Лицевой счёт», «ФИО плательщика», "
                 "«Назначение платежа» и «Сумма»."), "danger")
        return redirect(url_for("bank_sync.registry_format_settings", account_id=account.id))

    row = _get_or_create_registry_format_row(account)
    row.charge_columns = json.dumps(columns, ensure_ascii=False)
    row.charge_decimal_separator = (request.form.get("charge_decimal_separator") or ".")[:1]
    row.service_code = request.form.get("service_code") or ""
    row.delimiter = (request.form.get("delimiter") or ";")[:1]
    row.encoding = request.form.get("encoding") or "cp1251"
    database.db_session.commit()
    flash(_("Формат файла реестра начислений сохранён."), "success")
    return redirect(url_for("bank_sync.registry_format_settings", account_id=account.id))


@bp.route("/registry/format/payments", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_payment_registry_format(account_id):
    account = _get_account(account_id)
    columns = _parse_registry_format_form(registry_file.PAYMENT_FIELD_CATALOG)
    required = {k for k, _l, r in registry_file.PAYMENT_FIELD_CATALOG if r}
    if not required.issubset(set(columns)):
        flash(_("В формате реестра платежей обязательны поля «Дата», «Лицевой счёт» и «Сумма начисления»."), "danger")
        return redirect(url_for("bank_sync.registry_format_settings", account_id=account.id))

    row = _get_or_create_registry_format_row(account)
    row.payment_columns = json.dumps(columns, ensure_ascii=False)
    row.payment_decimal_separator = (request.form.get("payment_decimal_separator") or ",")[:1]
    row.trailer_prefix = request.form.get("trailer_prefix") or None
    # delimiter/encoding — общие для обоих файлов (см. модель), управляются
    # только формой реестра начислений выше, здесь не трогаем, чтобы
    # сохранение этой формы не затирало то, что задано на другой.
    database.db_session.commit()
    flash(_("Формат файла реестра платежей сохранён."), "success")
    return redirect(url_for("bank_sync.registry_format_settings", account_id=account.id))


# ---------------------------------------------------------------------------
# Настройки подключения к API
# ---------------------------------------------------------------------------

@bp.route("/api-settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_api_settings(account_id):
    account = _get_account(account_id)
    f = request.form

    provider_raw = f.get("api_provider", BankApiProvider.NONE.value)
    try:
        account.api_provider = BankApiProvider(provider_raw)
    except ValueError:
        abort(400)

    if account.api_provider == BankApiProvider.NONE:
        database.db_session.commit()
        flash(_("API банка отключён для этого счёта."), "success")
        return redirect(url_for("cooperative.view"))

    cred = _get_or_create_credential(account)
    cred.sandbox = bool(f.get("sandbox"))
    cred.client_id = f.get("client_id") or None
    cred.organization_id = f.get("organization_id") or None
    cred.account_number = f.get("account_number") or None
    # Пустое поле секрета в форме = «оставить прежний» (мы никогда не
    # показываем сохранённый секрет обратно в форме — только факт, что он
    # задан), непустое = заменить. То же для refresh_token.
    new_secret = f.get("client_secret")
    if new_secret:
        cred.client_secret_encrypted = crypto.encrypt(new_secret)
    new_refresh_token = f.get("refresh_token")
    if new_refresh_token:
        cred.refresh_token_encrypted = crypto.encrypt(new_refresh_token.strip())

    cert_file = request.files.get("tls_cert_p12")
    if cert_file and cert_file.filename:
        try:
            _save_client_cert(cred, cert_file, f.get("tls_cert_passphrase") or "")
        except ValueError as e:
            database.db_session.rollback()
            flash(str(e), "danger")
            return redirect(url_for("cooperative.view"))

    cred.last_error = None

    audit.record(
        "bank_api.settings_save", entity_type="bank_account", entity_id=account.id,
        summary=f"Настройки API банка обновлены для счёта {account.bank_name} {account.checking_account} "
                f"(провайдер: {account.api_provider.value})",
    )
    database.db_session.commit()
    flash(_("Настройки API банка сохранены."), "success")
    return redirect(url_for("cooperative.view"))


# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------

@bp.route("/sync-balance", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def sync_balance(account_id):
    account = _get_account(account_id)
    client = get_client(account)
    if client is None:
        flash(_("Для этого счёта не настроено или не поддерживается автоматическое обновление баланса."), "warning")
        return redirect(url_for("cooperative.view"))

    cred = _get_or_create_credential(account)
    try:
        info = client.get_balance()
    except BankApiError as e:
        _persist_rotated_refresh_token(cred, client)  # обновление токена могло пройти, даже если сам запрос — нет
        cred.last_error = str(e)
        database.db_session.commit()
        flash(_("Не удалось получить баланс из банка: {error}").format(error=str(e)), "danger")
        return redirect(url_for("cooperative.view"))

    account.balance = info.amount
    account.balance_updated_at = info.as_of
    cred.last_balance_sync_at = dt.datetime.utcnow()
    cred.last_error = None
    _persist_rotated_refresh_token(cred, client)
    audit.record(
        "bank_api.balance_sync", entity_type="bank_account", entity_id=account.id,
        summary=f"Баланс счёта {account.bank_name} {account.checking_account} обновлён из банка: {info.amount} ₽",
    )
    database.db_session.commit()
    flash(_("Баланс обновлён из банка."), "success")
    return redirect(url_for("cooperative.view"))


# ---------------------------------------------------------------------------
# Выписка (зачисления/списания)
# ---------------------------------------------------------------------------

@bp.route("/statement")
@roles_required(RoleEnum.BOARD)
def statement(account_id):
    account = _get_account(account_id)
    date_to = dt.date.today()
    date_from = date_to - dt.timedelta(days=30)
    if request.args.get("date_from"):
        date_from = dt.date.fromisoformat(request.args["date_from"])
    if request.args.get("date_to"):
        date_to = dt.date.fromisoformat(request.args["date_to"])

    lines = (
        database.db_session.query(BankStatementLine)
        .filter(
            BankStatementLine.bank_account_id == account.id,
            BankStatementLine.operation_date >= date_from,
            BankStatementLine.operation_date <= date_to,
        )
        .order_by(BankStatementLine.operation_date.desc(), BankStatementLine.id.desc())
        .all()
    )

    # Предполагаемый номер счёта для ещё не разнесённых зачислений — тот же
    # поиск (номер из текста → имя/лица для связи → сужение по виду взноса
    # и по голому числу в конце), что и при реальном разнесении, но БЕЗ
    # записи в БД — только чтобы подсказать в плейсхолдере поля ввода, что
    # система предложит, если нажать «Разнести». Без этого предпросмотра
    # плейсхолдер показывал номер, только если его поймал явный regex
    # («ЛС»/«Л/С»/«ЛСИ») при синхронизации — для строк, которые распознаются
    # только по имени (см. _resolve_account_by_name), поле всегда было
    # пустой подсказкой «Лицевой счёт», даже когда угадать уже можно.
    suggested_numbers = {}
    for line in lines:
        if line.direction != "credit" or line.matched_payment_id or line.account_number:
            continue
        if is_aggregate_registry_payment(line.payment_purpose):
            continue
        account_number = extract_account_number(line.payment_purpose)
        if not account_number:
            _kind, _target, account_number = _resolve_account_by_name(line.counterparty_name, line.payment_purpose)
        if account_number:
            suggested_numbers[line.id] = account_number

    return render_template(
        "cooperative/bank_statement.html", account=account, lines=lines, date_from=date_from, date_to=date_to,
        is_aggregate_registry_payment=is_aggregate_registry_payment, suggested_numbers=suggested_numbers,
        pending_credits=sum(
            1 for line in lines
            if line.direction == "credit" and not line.matched_payment_id
            and not is_aggregate_registry_payment(line.payment_purpose)
        ),
    )


@bp.route("/sync-statement", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def sync_statement(account_id):
    account = _get_account(account_id)
    client = get_client(account)
    if client is None:
        flash(_("Для этого счёта не настроено или не поддерживается автоматическая выписка."), "warning")
        return redirect(url_for("cooperative.view"))

    f = request.form
    date_from = dt.date.fromisoformat(f["date_from"])
    date_to = dt.date.fromisoformat(f["date_to"])
    cred = _get_or_create_credential(account)

    try:
        fetched = client.get_statement(date_from, date_to)
    except BankApiError as e:
        _persist_rotated_refresh_token(cred, client)
        cred.last_error = str(e)
        database.db_session.commit()
        flash(_("Не удалось получить выписку из банка: {error}").format(error=str(e)), "danger")
        return redirect(url_for("bank_sync.statement", account_id=account.id))

    existing_uids = {
        row[0] for row in database.db_session.query(BankStatementLine.external_uid)
        .filter(BankStatementLine.bank_account_id == account.id, BankStatementLine.external_uid.isnot(None))
        .all()
    }
    added = 0
    auto_allocated = 0
    for line in fetched:
        if line.external_uid and line.external_uid in existing_uids:
            continue  # уже загружена раньше — не дублируем
        account_number = extract_account_number(line.payment_purpose)
        row = BankStatementLine(
            bank_account_id=account.id,
            external_uid=line.external_uid,
            operation_date=line.operation_date,
            direction=line.direction,
            amount=line.amount,
            counterparty_name=line.counterparty_name,
            counterparty_inn=line.counterparty_inn,
            payment_purpose=line.payment_purpose,
            document_number=line.document_number,
            account_number=account_number,
        )
        database.db_session.add(row)
        added += 1

        # Автоматическое погашение — только для зачислений (деньги пришли
        # кооперативу). Сначала по номеру лицевого счёта, если распознан в
        # тексте; если не распознан или счёт с таким номером не найден —
        # по имени плательщика/того, за кого платят, включая лиц для связи
        # (см. _allocate_payment_to_account). Списания и то, что не
        # разрешилось ни одним способом, остаются непогашенными — не
        # гадаем, кому их отнести.
        if line.direction == "credit":
            database.db_session.flush()  # row.id нужен для комментария платежа
            comment = _("Автоматически разнесено по выписке банка, операция {uid}").format(
                uid=line.external_uid or row.id,
            )
            payment, resolved_number = _allocate_payment_to_account(
                line.operation_date, line.amount, comment,
                account_number=account_number, payer_name=line.counterparty_name, purpose=line.payment_purpose,
            )
            if payment is not None:
                row.matched_payment_id = payment.id
                row.account_number = resolved_number
                auto_allocated += 1

    cred.last_statement_sync_at = dt.datetime.utcnow()
    cred.last_error = None
    _persist_rotated_refresh_token(cred, client)

    # Сопоставить новые строки выписки с записями реестра
    direct, parametric = _match_registry_and_statement(account.id)

    audit.record(
        "bank_api.statement_sync", entity_type="bank_account", entity_id=account.id,
        summary=f"Загружена выписка счёта {account.bank_name} {account.checking_account} за "
                f"{date_from}—{date_to}: {added} новых операций, {auto_allocated} разнесено автоматически, "
                f"{direct + parametric} сопоставлено с реестром ({direct} прямых, {parametric} параметрических)",
    )
    database.db_session.commit()
    extra = ""
    if direct or parametric:
        extra = f" Сопоставлено с реестром: {direct} прямых + {parametric} параметрических совпадений."
    if auto_allocated:
        flash(
            _("Выписка обновлена: {n} новых операций, из них {m} автоматически разнесено по лицевым счетам.")
            .format(n=added, m=auto_allocated) + extra,
            "success",
        )
    else:
        flash(_("Выписка обновлена: {n} новых операций.").format(n=added) + extra, "success")
    # Показываем только что загруженный период, а не сбрасываем на дефолт.
    return redirect(url_for("bank_sync.statement", account_id=account.id, date_from=date_from.isoformat(), date_to=date_to.isoformat()))


@bp.route("/statement/<int:line_id>/allocate", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def allocate_statement_line(account_id, line_id):
    """
    Ручное погашение для строк выписки, которые не разнеслись сами при
    синхронизации. Если в форме явно указан номер лицевого счёта —
    используется он (это позволяет председателю переопределить
    автоматическое распознавание, если оно ошиблось или ничего не нашло);
    если поле пустое — повторяется тот же поиск, что и при автоматическом
    разнесении (по номеру из текста, затем по имени/лицам для связи) — на
    случай, если лицевой счёт завели уже ПОСЛЕ синхронизации выписки.

    Вызывается через AJAX со страницы выписки (см. bank_statement.html) —
    обычный POST+redirect сбрасывал бы фильтры периода в адресной строке
    (дата с/по — GET-параметры, отдельные от этой формы), председателю
    приходилось выбирать период заново после каждого разнесения. Если
    запрос помечен заголовком `X-Requested-With: XMLHttpRequest` — отвечаем
    JSON вместо редиректа; обычная форма (JS отключён) по-прежнему
    работает через redirect+flash, как раньше.
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def respond(success: bool, message: str, **extra):
        """И для AJAX, и для обычной формы (JS отключён) — общая точка
        выхода. Для AJAX всегда отвечаем 200 с полем success в теле —
        HTTP-код здесь не несёт бизнес-смысла (сама операция обработана
        корректно, даже если результат — «не нашли счёт»), фронтенд
        разбирает success, а не статус ответа."""
        if is_ajax:
            return {"success": success, "message": message, **extra}
        flash(message, "success" if success else "danger")
        return redirect(url_for("bank_sync.statement", account_id=account_id))

    account = _get_account(account_id)
    line = database.db_session.get(BankStatementLine, line_id)
    if line is None or line.bank_account_id != account.id:
        abort(404)
    if line.matched_payment_id is not None:
        return respond(False, _("Эта операция уже разнесена."))
    if line.direction != "credit":
        return respond(False, _("Разносить можно только зачисления, не списания."))

    override = (request.form.get("account_number") or "").strip()
    # Тот же порядок, что и при автоматическом разнесении (sync_statement)
    # и в предпросмотре подсказки (statement()): переопределение из формы →
    # уже сохранённый номер → повторный разбор текста. Раньше здесь не
    # было последнего шага — из-за этого подсказка «Предполагаемый счёт»
    # (которая как раз и вычисляется через extract_account_number) могла
    # показать номер, а сама кнопка «Разнести» с пустым полем — ничего не
    # найти, хотя номер буквально виден в тексте назначения платежа.
    account_number = override or line.account_number or extract_account_number(line.payment_purpose)

    comment = _("Разнесено вручную по выписке банка, операция {uid}").format(uid=line.external_uid or line.id)
    payment, resolved_number = _allocate_payment_to_account(
        line.operation_date, line.amount, comment,
        account_number=account_number, payer_name=line.counterparty_name, purpose=line.payment_purpose,
    )
    if payment is None:
        if account_number:
            msg = _("Лицевой счёт «{number}» не найден, и по имени плательщика однозначно определить его тоже не удалось.").format(number=account_number)
        else:
            msg = _("Не удалось определить лицевой счёт по имени плательщика — укажите номер лицевого счёта вручную.")
        return respond(False, msg)

    line.account_number = resolved_number
    line.matched_payment_id = payment.id
    audit.record(
        "bank_api.statement_line_allocate", entity_type="bank_account", entity_id=account.id,
        summary=f"Операция выписки ({resolved_number}, {line.amount} ₽) разнесена платежом вручную",
    )
    database.db_session.commit()
    return respond(True, _("Платёж разнесён."), account_number=resolved_number)


@bp.route("/statement/allocate-all", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def allocate_all_statement_lines(account_id):
    """
    «Разнести всё возможное одной кнопкой» — повторяет автоматическое
    погашение (номер лицевого счёта → имя плательщика/лица для связи, см.
    _allocate_payment_to_account) для ВСЕХ ещё не разнесённых зачислений
    счёта разом, а не по одному через ручную кнопку на каждой строке.
    Полезно после того, как в справочник добавили новый лицевой счёт или
    поправили формат распознавания номера — старые строки выписки сами
    не пересчитываются, а тут пересчитываются все за один клик, ЗАНОВО
    разбирая номер из текста (а не только используя то, что уже было
    сохранено в line.account_number при первой синхронизации) — иначе
    улучшение регэкспа не помогло бы уже загруженным старым операциям.
    Пропускает агрегированные проводки по реестру платежей (см.
    is_aggregate_registry_payment) — их в принципе нельзя отнести на один
    счёт, это не «не получилось», а «не должно пытаться».
    """
    account = _get_account(account_id)
    lines = (
        database.db_session.query(BankStatementLine)
        .filter(
            BankStatementLine.bank_account_id == account.id,
            BankStatementLine.direction == "credit",
            BankStatementLine.matched_payment_id.is_(None),
        )
        .all()
    )
    allocated = 0
    skipped_aggregate = 0
    for line in lines:
        if is_aggregate_registry_payment(line.payment_purpose):
            skipped_aggregate += 1
            continue
        comment = _("Автоматически разнесено по выписке банка, операция {uid}").format(
            uid=line.external_uid or line.id,
        )
        account_number = line.account_number or extract_account_number(line.payment_purpose)
        payment, resolved_number = _allocate_payment_to_account(
            line.operation_date, line.amount, comment,
            account_number=account_number, payer_name=line.counterparty_name, purpose=line.payment_purpose,
        )
        if payment is not None:
            line.matched_payment_id = payment.id
            line.account_number = resolved_number
            allocated += 1

    if allocated:
        audit.record(
            "bank_api.statement_bulk_allocate", entity_type="bank_account", entity_id=account.id,
            summary=f"Массовое разнесение выписки счёта {account.bank_name} {account.checking_account}: "
                    f"{allocated} операций",
        )
    database.db_session.commit()
    remaining = len(lines) - allocated - skipped_aggregate
    if allocated:
        flash(_("Разнесено: {n} операций.").format(n=allocated), "success")
    else:
        flash(_("Не удалось разнести ни одной операции — для оставшихся нужен ручной ввод номера счёта."), "warning")
    if remaining:
        flash(
            _("Осталось неразнесённых (нужен ручной ввод номера счёта): {n}.").format(n=remaining),
            "warning",
        )
    # Сохраняем текущий отображаемый период (даты «показать» на странице,
    # они отдельны от диапазона синхронизации) — иначе редирект на голый
    # URL без query-параметров сбрасывает фильтр на дефолтные последние
    # 30 дней, и председателю приходится выбирать период заново.
    return redirect(url_for(
        "bank_sync.statement", account_id=account.id,
        date_from=request.form.get("date_from") or None, date_to=request.form.get("date_to") or None,
    ))


# ---------------------------------------------------------------------------
# Реестр начислений — текущая задолженность членов/гаражей, отправляемая
# в банк, чтобы плательщик мог увидеть и оплатить её в приложении банка.
# ---------------------------------------------------------------------------

def _debtor_items() -> list[ChargeRegistryItem]:
    """Все лицевые счета (гаражные и членские) с отрицательным балансом —
    см. accounting.balance(). Не фильтрует по тому, отправлялся ли этот долг
    в реестр раньше (см. context.md — известное ограничение)."""
    items = []
    for personal_account in database.db_session.query(PersonalAccount).all():
        garage = personal_account.garage
        bal = _balance(garage)
        if bal >= 0:
            continue
        owners = ", ".join(o.person.full_name for o in garage.ownerships) or f"гараж №{garage.number}"
        items.append(ChargeRegistryItem(
            account_number=personal_account.account_number,
            payer_name=owners,
            amount=-bal,
            purpose=f"Электричество, гараж №{garage.number}",
        ))
    for member_account in database.db_session.query(MemberAccount).all():
        bal = _balance(member_account)
        if bal >= 0:
            continue
        items.append(ChargeRegistryItem(
            account_number=member_account.account_number,
            payer_name=member_account.person.full_name,
            amount=-bal,
            purpose=f"{member_account.fee_type.name}, гараж №{member_account.garage.number}",
        ))
    return items


@bp.route("/registry/charges")
@roles_required(RoleEnum.BOARD)
def charge_registry(account_id):
    account = _get_account(account_id)
    batches = (
        database.db_session.query(ChargeRegistryBatch)
        .filter_by(bank_account_id=account.id)
        .order_by(ChargeRegistryBatch.created_at.desc())
        .all()
    )
    pending = _debtor_items()
    return render_template(
        "cooperative/charge_registry.html", account=account, batches=batches,
        pending_count=len(pending), pending_total=sum((i.amount for i in pending), Decimal("0")),
    )


@bp.route("/registry/charges/download")
@roles_required(RoleEnum.BOARD)
def download_charge_registry_file(account_id):
    """
    Тот же файл, что уходит в банк кнопкой «Отправить реестр в банк» (см.
    send_charge_registry ниже), но для скачивания — на случай, если
    автоматическая отправка через API недоступна для конкретного
    подключения (см. комментарий в app/bank_api/sberbank.py: реестр
    начислений — файловый канал СберБизнес Онлайн, а не гарантированно
    REST). Председатель может загрузить его вручную через веб-интерфейс
    банка. **Кодировка файла — Windows-1251 (cp1251)**, не UTF-8 — это
    формат, который принимает СберБизнес Онлайн для реестров начислений
    (см. app/bank_api/registry_file.py).
    """
    account = _get_account(account_id)
    items = _debtor_items()
    content = registry_file.build_charge_registry_file(items, build_registry_format(account))
    filename = f"charges_{account.checking_account}_{dt.date.today().isoformat()}.txt"
    return Response(
        content,
        headers={
            "Content-Type": "text/plain; charset=windows-1251",
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )


@bp.route("/registry/charges/send", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def send_charge_registry(account_id):
    account = _get_account(account_id)
    client = get_client(account)
    if client is None:
        flash(_("Для этого счёта не настроен или не поддерживается реестр начислений."), "warning")
        return redirect(url_for("cooperative.view"))

    period = (request.form.get("period") or "").strip()
    if not period:
        flash(_("Укажите период реестра."), "danger")
        return redirect(url_for("bank_sync.charge_registry", account_id=account.id))

    items = _debtor_items()
    if not items:
        flash(_("Нет непогашенной задолженности для отправки."), "warning")
        return redirect(url_for("bank_sync.charge_registry", account_id=account.id))

    batch = ChargeRegistryBatch(
        bank_account_id=account.id, period=period,
        charges_count=len(items), total_amount=sum((i.amount for i in items), Decimal("0")),
    )
    database.db_session.add(batch)
    database.db_session.flush()

    cred = _get_or_create_credential(account)
    try:
        result = client.send_charge_registry(items, period)
    except BankApiError as e:
        _persist_rotated_refresh_token(cred, client)
        batch.status = ChargeRegistryStatus.ERROR
        batch.bank_comment = str(e)
        cred.last_error = str(e)
        database.db_session.commit()
        flash(_("Не удалось отправить реестр начислений: {error}").format(error=str(e)), "danger")
        return redirect(url_for("bank_sync.charge_registry", account_id=account.id))

    batch.external_id = result.external_id
    batch.status = ChargeRegistryStatus.SENT
    batch.bank_comment = result.bank_comment
    batch.sent_at = dt.datetime.utcnow()
    cred.last_error = None
    _persist_rotated_refresh_token(cred, client)
    audit.record(
        "bank_api.charge_registry_send", entity_type="bank_account", entity_id=account.id,
        summary=f"Отправлен реестр начислений за «{period}»: {batch.charges_count} начислений на {batch.total_amount} ₽",
    )
    database.db_session.commit()
    flash(_("Реестр начислений отправлен в банк."), "success")
    return redirect(url_for("bank_sync.charge_registry", account_id=account.id))


@bp.route("/registry/charges/<int:batch_id>/refresh", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def refresh_charge_registry(account_id, batch_id):
    account = _get_account(account_id)
    batch = database.db_session.get(ChargeRegistryBatch, batch_id)
    if batch is None or batch.bank_account_id != account.id:
        abort(404)
    client = get_client(account)
    if client is None or not batch.external_id:
        flash(_("Невозможно обновить статус этого реестра."), "warning")
        return redirect(url_for("bank_sync.charge_registry", account_id=account.id))

    try:
        result = client.get_charge_registry_status(batch.external_id)
    except BankApiError as e:
        cred = _get_or_create_credential(account)
        _persist_rotated_refresh_token(cred, client)
        cred.last_error = str(e)
        database.db_session.commit()
        flash(_("Не удалось обновить статус: {error}").format(error=str(e)), "danger")
        return redirect(url_for("bank_sync.charge_registry", account_id=account.id))

    status_map = {s.value: s for s in ChargeRegistryStatus}
    batch.status = status_map.get(result.status.lower(), batch.status)
    batch.bank_comment = result.bank_comment
    _persist_rotated_refresh_token(_get_or_create_credential(account), client)
    database.db_session.commit()
    flash(_("Статус реестра обновлён."), "success")
    return redirect(url_for("bank_sync.charge_registry", account_id=account.id))


# ---------------------------------------------------------------------------
# Реестр платежей — платежи, поступившие в банк по реестру начислений
# ---------------------------------------------------------------------------

def _find_account_by_number(account_number: str):
    """Возвращает (kind, объект) — ("member", MemberAccount) или ("garage", Garage),
    либо (None, None), если лицевой счёт с таким номером не найден ни там, ни там."""
    member_account = database.db_session.query(MemberAccount).filter_by(account_number=account_number).first()
    if member_account is not None:
        return "member", member_account
    personal_account = database.db_session.query(PersonalAccount).filter_by(account_number=account_number).first()
    if personal_account is not None:
        return "garage", personal_account.garage
    return None, None


# Банк нередко вписывает номер лицевого счёта прямо в свободный текст
# назначения платежа, например: «ЛС 10640; ЧЛЕНСКИЕ ВЗНОСЫ (ФАМИЛИЯ И.О.);
# 0126;ФАМИЛИЯ ИМЯ» — «ЛС» (лицевой счёт), затем опционально «:»/«№»/пробелы,
# затем сам номер. Регистронезависимо — некоторые банки шлют «лс» строчными.
#
# **Проверено на реальной выписке (81 зачисление,
# tests/fixtures/statement_sample.csv — структура и форматы подлинные,
# все ФИО/ИНН/адреса/номера счетов в файле вымышленные, см. context.md)**:
# более узкий вариант без «ЛСИ» ловил заметно меньше — тот же банк/
# подключение вперемешку с обычным «ЛС» присылает ещё «Л/С» (слэш между
# буквами) и «ЛСИ» (третья буква, иногда с пробелом перед двоеточием —
# «ЛСИ :20780»). Регэксп ниже покрывает все три варианта одним выражением: «л» + необязательный «/» +
# «с» + необязательное «и» + необязательные пробелы/«:»/«№» + номер. С
# «ЛСИ» распознаётся 29 из 81 зачисления вместо 10 без него — остальные
# 52 не баг регэкспа: 22 из них — агрегированные проводки по реестру
# платежей (см. is_aggregate_registry_payment ниже, туда номер счёта в
# принципе не подставить), оставшиеся не содержат номера в тексте вовсе и
# рассчитаны на разбор по имени (см. _resolve_account_by_name). Если при
# доработке регэкспа доля распознавания на этом образце упадёт — это
# регрессия, см. tests/test_bank_sync.py:
# test_account_number_extraction_against_statement_sample.
_ACCOUNT_NUMBER_RE = re.compile(r"л\s*/?\s*с\s*и?\s*[:№]?\s*(\d+)", re.IGNORECASE)

# Агрегированное зачисление по реестру платежей — банк одной проводкой
# переводит сумму сразу за НЕСКОЛЬКО платежей физлиц, принятых через
# реестр начислений (см. app/bank_api/registry_file.py), одной строкой
# вида «ПО ПРИНЯТЫМ ПЛАТЕЖАМ С 12/02/2026 ПО 12/02/2026 НА ОБЩУЮ СУММУ
# 5120.00,...,СОГЛАСНО ЭЛ.РЕЕСТРУ EPS..._051.txt» — узнаваема по этой
# фразе независимо от банка/подключения (на реальной выписке — 22 из 81
# зачисления, самая частая причина «не разнесено» из всех). Разносить
# такую операцию на ОДИН лицевой счёт в принципе неверно — сумма
# покрывает нескольких плательщиков сразу; реальное разнесение делается
# через реестр платежей (см. bank_sync.py: payment_registry), не через
# выписку. extract_account_number() и автоматическое погашение такие
# строки не трогают вообще — is_aggregate_registry_payment() ниже
# используется только для того, чтобы показать в UI понятную пометку
# вместо «не разнесён», как будто это можно разнести вручную.
_AGGREGATE_REGISTRY_PAYMENT_RE = re.compile(r"по\s+прин[ая]тым\s+платежам", re.IGNORECASE)


def is_aggregate_registry_payment(text: str | None) -> bool:
    return bool(text) and bool(_AGGREGATE_REGISTRY_PAYMENT_RE.search(text))

# «Фамилия И.О.» — банк нередко указывает в назначении платежа не самого
# плательщика, а того, за кого платят (типичный пример из реальных файлов
# реестра: "ЧЛЕНСКИЕ ВЗНОСЫ (ЛИКСАНОВ Г.Н.)", где плательщик по факту —
# другой человек, например супруг(а) или доверенное лицо). Заглавные и
# строчные буквы фамилии — банки шлют по-разному (то Title Case, то ВСЕ
# ЗАГЛАВНЫЕ), поэтому в обеих группах допускаем оба регистра.
_INITIALS_NAME_RE = re.compile(r"([А-ЯЁ][А-ЯЁа-яё]+)\s+([А-ЯЁ])\s*\.\s*([А-ЯЁ])\s*\.")


def extract_account_number(text: str | None) -> str | None:
    """None, если номер лицевого счёта в тексте не распознан — это НЕ
    ошибка, обычная ситуация для многих операций по счёту (переводы,
    расходные операции, платежи не по начислению) — вызывающий код должен
    воспринимать это как «не удалось сопоставить автоматически», не как
    сбой."""
    if not text:
        return None
    # Сначала ищем «ЛС 12345» в тексте
    m = _ACCOUNT_NUMBER_RE.search(text)
    if m:
        return m.group(1)
    return None


def _normalize_full_name(text: str) -> str:
    return " ".join(text.upper().split())


def _initials_key(full_name: str) -> str | None:
    """«Иванов Иван Иванович» -> «ИВАНОВ И.И.» — сокращённая форма для
    сравнения с тем, что банк обычно пишет в свободном тексте. None, если
    в ФИО меньше двух слов — сокращать нечего."""
    parts = full_name.split()
    if len(parts) < 2:
        return None
    surname = parts[0].upper()
    initials = "".join(f"{p[0].upper()}." for p in parts[1:3])
    return f"{surname} {initials}"


def _extract_initials_candidates(text: str | None) -> set[str]:
    if not text:
        return set()
    return {
        f"{m.group(1).upper()} {m.group(2).upper()}.{m.group(3).upper()}."
        for m in _INITIALS_NAME_RE.finditer(text)
    }


def _find_persons_by_name(payer_name: str | None, purpose: str | None) -> list[Person]:
    """Ищет людей по тому, что банк прислал текстом — как полное ФИО
    (обычно поле «плательщик»), так и по форме «Фамилия И.О.», которую
    банк нередко указывает в САМОМ назначении платежа для того, за кого
    платят (не обязательно совпадает с плательщиком — см. комментарий у
    _INITIALS_NAME_RE). Ищет среди ВСЕХ Person, не только собственников —
    совпадение с лицом для связи (GarageContact) тоже валидно, дальше
    _accounts_for_person() поднимает от него счета владельцев гаража.

    Полный перебор Person в Python (не SQL ILIKE) — приемлемо для размера
    обычного ГСК (десятки-сотни человек, не тысячи); сравнение по
    сокращённой форме ФИО в любом случае требует вычисления на стороне
    Python, единым проходом заодно делаем и точное сравнение."""
    full_candidates = {_normalize_full_name(payer_name)} if payer_name else set()
    initials_candidates = _extract_initials_candidates(purpose) | _extract_initials_candidates(payer_name)
    if not full_candidates and not initials_candidates:
        return []

    matched = []
    for person in database.db_session.query(Person).all():
        normalized = _normalize_full_name(person.full_name)
        if normalized in full_candidates:
            matched.append(person)
            continue
        key = _initials_key(person.full_name)
        if key and key in initials_candidates:
            matched.append(person)
    return matched


def _accounts_for_person(person: Person) -> list[tuple[str, object]]:
    """(kind, target) для всех лицевых счетов, к которым можно отнести
    платёж от этого человека — как непосредственно его собственные
    MemberAccount, так и (если он собственник ИЛИ лицо для связи по
    гаражу) счета всех собственников этого гаража и гаражный
    PersonalAccount на электричество. Лицо для связи само по себе
    лицевого счёта не имеет — предполагается, что оно платит ЗА
    собственника, поэтому от него поднимаются именно счета владельцев."""
    results: list[tuple[str, object]] = []
    seen: set[tuple[str, int]] = set()

    def add(kind: str, obj) -> None:
        key = (kind, obj.id)
        if key not in seen:
            seen.add(key)
            results.append((kind, obj))

    for ma in database.db_session.query(MemberAccount).filter_by(person_id=person.id).all():
        add("member", ma)

    owned = {r[0] for r in database.db_session.query(GarageOwnership.garage_id).filter_by(person_id=person.id).all()}
    contact_for = {r[0] for r in database.db_session.query(GarageContact.garage_id).filter_by(person_id=person.id).all()}
    for garage_id in owned | contact_for:
        personal_account = database.db_session.query(PersonalAccount).filter_by(garage_id=garage_id).first()
        if personal_account is not None:
            add("garage", personal_account.garage)
        for ma in database.db_session.query(MemberAccount).filter_by(garage_id=garage_id).all():
            add("member", ma)

    return results


# Служебные слова, не несущие смысла для сравнения вида взноса — не
# участвуют в сопоставлении (предлоги в «Пеня ПО членскому взносу» и т.п.).
_FEE_TYPE_STOP_WORDS = {"по", "на", "за", "и", "в", "к", "с", "от", "для"}


def _word_stem(word: str) -> str:
    """Грубый «стемминг» для сравнения вида взноса с текстом банка —
    отбрасывает последние 1-2 буквы слова (обычно это и есть окончание —
    падеж/число/род различаются именно ими: членскИЙ/членскИЕ,
    взнос/взносЫ). Короткие слова (≤3 букв) не обрезаем — риск срезать
    значащую часть короткого слова выше пользы."""
    if len(word) <= 3:
        return word
    cut = 2 if len(word) >= 6 else 1
    return word[:-cut]


def _fee_type_matches_purpose(fee_type_name: str, purpose_upper: str) -> bool:
    """Сравнение вида взноса с текстом назначения платежа устойчиво к
    падежным/числовым окончаниям — банк почти всегда пишет во
    множественном числе («ЧЛЕНСКИЕ ВЗНОСЫ»), а в справочнике вид взноса
    обычно назван в единственном («Членский взнос»); точное совпадение
    подстроки эту разницу не переживает. Реальный случай, найденный
    Sne: «Земельный налог» совпадал всегда (это словосочетание в данном
    контексте не меняется по числу), «Членский взнос» — никогда, из-за
    чего членские взносы вообще переставали автоматически разноситься
    для ВСЕХ членов, а не только для конкретного человека — расхождение
    формы слова, не что-то специфичное для одного счёта. Требуем совпадения
    ВСЕХ значащих слов вида взноса (без предлогов) — это и отличает
    «Земельный налог» от «Пеня по земельному налогу» (слово «пеня»
    обязано присутствовать), не позволяя пене случайно сойти за
    основной налог."""
    words = [w for w in re.findall(r"[А-ЯЁа-яё]+", fee_type_name.upper()) if w not in _FEE_TYPE_STOP_WORDS and len(w) > 2]
    if not words:
        return False
    return all(_word_stem(w) in purpose_upper for w in words)


def _fee_type_word_count(fee_type_name: str) -> int:
    return len([w for w in re.findall(r"[А-ЯЁа-яё]+", fee_type_name.upper()) if w not in _FEE_TYPE_STOP_WORDS and len(w) > 2])


def _narrow_by_fee_type(candidates: list[tuple[str, object]], purpose: str | None) -> list[tuple[str, object]]:
    """Если после поиска по имени осталось несколько лицевых счетов —
    сужаем по виду взноса, если он назван в назначении платежа текстом
    («ЧЛЕНСКИЕ ВЗНОСЫ» / «ЗЕМЕЛЬНЫЙ НАЛОГ» и т.п. — обычная практика
    банковских выписок и реестров, см. FeeType.name и
    _fee_type_matches_purpose выше про устойчивость к окончаниям).

    Среди подошедших видов взноса выбираем САМЫЙ КОНКРЕТНЫЙ — с
    наибольшим числом совпавших значащих слов. Это важно для пары
    «Земельный налог» / «Пеня по земельному налогу»: у пени слова
    основного налога — подмножество (она содержит их же плюс «пеня»), то
    есть текст пени формально подходит под оба вида взноса — без выбора
    самого длинного совпадения оба оставались бы кандидатами и платёж
    считался бы неоднозначным даже тогда, когда на самом деле понятно,
    что это именно пеня. Если после этого сужение оставляет 0 — возвращаем
    исходный список (не уверены, что название вида взноса в справочнике
    совпадает с тем, что написал банк), если оставляет ровно 1 — used как
    окончательный ответ вызывающим кодом."""
    if len(candidates) <= 1 or not purpose:
        return candidates
    purpose_upper = purpose.upper()
    matched = [
        (kind, obj) for kind, obj in candidates
        if kind == "member" and _fee_type_matches_purpose(obj.fee_type.name, purpose_upper)
    ]
    if not matched:
        return candidates
    best_score = max(_fee_type_word_count(obj.fee_type.name) for _kind, obj in matched)
    return [(kind, obj) for kind, obj in matched if _fee_type_word_count(obj.fee_type.name) == best_score]


# Иногда банк (или сам плательщик через QR/шаблон) добавляет голый номер
# лицевого счёта СРАЗУ после «(Фамилия И.О.)», без метки «ЛС»/«Л/С»/«ЛСИ»:
# «ЗЕМЕЛЬНЫЙ НАЛОГ (Тишинов И.М.);20850». Отдельно от extract_account_number
# (там — доверенная метка) этот голый номер сам по себе НЕ используется как
# источник лицевого счёта — слишком легко перепутать с датой/номером
# документа/периодом («;0126» — тоже 4 цифры в конце похожего места). Он
# годится только для СУЖЕНИЯ уже найденных по имени кандидатов (см.
# _resolve_account_by_name ниже) — то есть подтверждает выбор среди счетов
# уже опознанного человека, а не может сам по себе привести к чужому
# счёту: если совпадений с реальными счетами кандидатов нет — просто
# ничего не сужаем, риска неверного разнесения это не создаёт.
_TRAILING_ACCOUNT_NUMBER_RE = re.compile(r"\)\s*;?\s*(\d{3,8})\s*$")


def _narrow_by_trailing_number(candidates: list[tuple[str, object]], purpose: str | None) -> list[tuple[str, object]]:
    if len(candidates) <= 1 or not purpose:
        return candidates
    m = _TRAILING_ACCOUNT_NUMBER_RE.search(purpose)
    if not m:
        return candidates
    number = m.group(1)
    narrowed = [
        (kind, obj) for kind, obj in candidates
        if (kind == "member" and obj.account_number == number)
        or (kind == "garage" and obj.account and obj.account.account_number == number)
    ]
    return narrowed or candidates


def _resolve_account_by_name(payer_name: str | None, purpose: str | None):
    """Возвращает (kind, target, номер_счёта) при ОДНОЗНАЧНОМ совпадении
    по имени, иначе (None, None, None) — в том числе если найдено
    НЕСКОЛЬКО кандидатов даже после сужения по виду взноса и голому номеру
    в конце текста (см. _narrow_by_trailing_number — это подтверждение
    среди уже найденных по имени счетов, не самостоятельный источник
    номера). Разносить платёж по имени, когда неоднозначно, чей это счёт
    (у человека несколько гаражей/видов взносов, или совпало несколько
    людей с похожим ФИО) — риск отнести чужой платёж не туда, поэтому в
    неоднозначных случаях платёж остаётся неразнесённым для решения
    председателем вручную, как и при отсутствии совпадений вовсе."""
    persons = _find_persons_by_name(payer_name, purpose)
    if not persons:
        return None, None, None

    candidates: list[tuple[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for person in persons:
        for kind, obj in _accounts_for_person(person):
            key = (kind, obj.id)
            if key not in seen:
                seen.add(key)
                candidates.append((kind, obj))

    candidates = _narrow_by_fee_type(candidates, purpose)
    candidates = _narrow_by_trailing_number(candidates, purpose)
    if len(candidates) != 1:
        return None, None, None

    kind, target = candidates[0]
    if kind == "member":
        account_number = target.account_number
    else:
        account_number = target.account.account_number if target.account else None
    return kind, target, account_number


def _allocate_payment_to_account(
    date: dt.date, amount: Decimal, comment: str,
    account_number: str | None = None, payer_name: str | None = None, purpose: str | None = None,
) -> tuple[Payment | None, str | None]:
    """Общая точка для «нашли лицевой счёт — завести Payment на полную
    сумму и разнести по FIFO» — используется и для реестра платежей
    (allocate_payment_registry_entry), и для автоматического/ручного
    разнесения строк выписки (см. ниже).

    Поиск в два шага: сначала по номеру лицевого счёта (если он есть и
    найден — самый надёжный источник, используется как есть); если номера
    нет или счёт с таким номером не найден — по имени плательщика/тому,
    за кого платят (см. _resolve_account_by_name), включая лиц для связи.

    Возвращает (Payment, распознанный_номер_счёта) — второй элемент
    полезен даже при поиске по имени, чтобы сохранить фактический номер
    счёта на BankStatementLine для наглядности; (None, None), если ни
    один способ не дал ОДНОЗНАЧНОГО совпадения — вызывающий код решает,
    что делать (для выписки — оставить как «не разнесено», для реестра —
    показать ошибку и предложить разнести вручную).

    Разносится ПОЛНАЯ сумма, поступившая по банку (amount) — не за вычетом
    комиссии банка, если банк её удерживает: комиссия — расход кооператива,
    а не недоплата члена (тот же принцип, что у PaymentRegistryEntry.amount
    vs .credited_amount/.fee_amount)."""
    kind, target = (None, None)
    resolved_account_number = account_number
    if account_number:
        kind, target = _find_account_by_number(account_number)
    if kind is None:
        kind, target, resolved_account_number = _resolve_account_by_name(payer_name, purpose)
    if kind is None:
        return None, None

    payment = (
        Payment(account_id=target.id, date=date, amount=amount, comment=comment) if kind == "member"
        else Payment(garage_id=target.id, date=date, amount=amount, comment=comment)
    )
    database.db_session.add(payment)
    database.db_session.flush()
    if kind == "member":
        reallocate_member_charges(target)
    else:
        reallocate_garage_charges(target)
    return payment, resolved_account_number


def _match_registry_and_statement(account_id: int) -> tuple[int, int]:
    """
    Сопоставляет записи реестра платежей и банковской выписки для данного счёта.
    Возвращает (прямые_совпадения, параметрические_совпадения).

    Приоритет 1 — прямой match по внешнему ID банка:
        PaymentRegistryEntry.external_id == BankStatementLine.external_uid
    Приоритет 2 — параметрический match по account_number + amount + date (±1 день).
    Только для записей, которые ещё не сопоставлены ни с чем.
    """
    stmt_lines = (
        database.db_session.query(BankStatementLine)
        .filter_by(bank_account_id=account_id, matched_registry_id=None)
        .all()
    )
    reg_entries = (
        database.db_session.query(PaymentRegistryEntry)
        .filter_by(bank_account_id=account_id, matched_statement_id=None)
        .all()
    )

    direct_matches = 0
    parametric_matches = 0

    # Прямой match по внешнему ID
    for entry in reg_entries:
        for line in stmt_lines:
            if line.external_uid and line.external_uid == entry.external_id:
                entry.matched_statement_id = line.id
                line.matched_registry_id = entry.id
                direct_matches += 1
                break

    # Параметрический match по account_number + amount + date
    matched_line_ids = {line.id for line in stmt_lines if line.matched_registry_id is not None}
    matched_entry_ids = {entry.id for entry in reg_entries if entry.matched_statement_id is not None}

    for entry in reg_entries:
        if entry.id in matched_entry_ids:
            continue
        entry_account = entry.account_number
        if not entry_account:
            continue
        for line in stmt_lines:
            if line.id in matched_line_ids:
                continue
            if line.matched_registry_id is not None:
                continue
            line_account = line.account_number
            if not line_account:
                continue
            if entry_account == line_account and entry.amount == line.amount:
                date_diff = abs((entry.operation_date - line.operation_date).days)
                if date_diff <= 1:
                    entry.matched_statement_id = line.id
                    line.matched_registry_id = entry.id
                    parametric_matches += 1
                    matched_line_ids.add(line.id)
                    matched_entry_ids.add(entry.id)
                    break

    return direct_matches, parametric_matches


def _store_payment_registry_items(account: BankAccount, items: list) -> int:
    """Общая дедупликация по external_id для обоих источников реестра
    платежей — автоматического (fetch_payment_registry, через API) и
    ручного (upload_payment_registry_file, файл, скачанный из СберБизнес
    Онлайн вручную) — чтобы правило «не завести запись дважды» не
    расходилось между путями."""
    existing_ids = {
        row[0] for row in database.db_session.query(PaymentRegistryEntry.external_id)
        .filter_by(bank_account_id=account.id).all()
    }
    added = 0
    for item in items:
        if item.external_id in existing_ids:
            continue
        database.db_session.add(PaymentRegistryEntry(
            bank_account_id=account.id,
            external_id=item.external_id,
            payer_name=item.payer_name,
            account_number=item.account_number,
            amount=item.amount,
            operation_date=item.operation_date,
            payment_purpose=item.payment_purpose,
            credited_amount=getattr(item, "credited_amount", None),
            fee_amount=getattr(item, "fee_amount", None),
        ))
        added += 1
    return added


@bp.route("/registry/payments")
@roles_required(RoleEnum.BOARD)
def payment_registry(account_id):
    account = _get_account(account_id)
    entries = (
        database.db_session.query(PaymentRegistryEntry)
        .filter_by(bank_account_id=account.id)
        .order_by(PaymentRegistryEntry.operation_date.desc(), PaymentRegistryEntry.id.desc())
        .all()
    )
    return render_template("cooperative/payment_registry.html", account=account, entries=entries)


@bp.route("/registry/payments/upload", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def upload_payment_registry_file(account_id):
    """
    Ручной путь для реестра платежей — на случай, если автоматический
    запрос через API недоступен для конкретного подключения (см.
    комментарий в app/bank_api/sberbank.py). Председатель скачивает файл(ы)
    реестра платежей в веб-интерфейсе СберБизнес Онлайн и загружает их
    здесь — можно выбрать сразу НЕСКОЛЬКО файлов (банк обычно отдаёт один
    файл на реестр/день, а разнести хочется весь накопленный период за
    один раз, не по одному файлу). **Файлы должны быть в кодировке
    Windows-1251 (cp1251)** — как их и отдаёт банк для этого канала (см.
    app/bank_api/registry_file.py); UTF-8-файл будет прочитан некорректно
    (кириллица превратится в «кракозябры»).

    Один плохой файл в пачке (пустой/не в том формате) не должен обрывать
    загрузку остальных — обрабатываем каждый независимо и считаем итоги
    по всем файлам сразу; `_match_registry_and_statement` запускается
    один раз в конце по всем загруженным записям, а не отдельно на
    каждый файл.
    """
    account = _get_account(account_id)
    file_storages = [f for f in request.files.getlist("registry_file") if f and f.filename]
    if not file_storages:
        flash(_("Файл реестра не выбран."), "danger")
        return redirect(url_for("bank_sync.payment_registry", account_id=account.id))

    fmt = build_registry_format(account)
    added = 0
    empty_files = []
    for file_storage in file_storages:
        content = file_storage.read()
        items = registry_file.parse_payment_registry_file(content, fmt)
        if not items:
            empty_files.append(file_storage.filename)
            continue
        added += _store_payment_registry_items(account, items)

    if added == 0:
        flash(
            _("Ни в одном из {n} файлов не найдено ни одной корректной строки — "
              "проверьте, что файлы в кодировке Windows-1251.").format(n=len(file_storages)),
            "warning",
        )
        return redirect(url_for("bank_sync.payment_registry", account_id=account.id))

    database.db_session.flush()  # entry IDs нужны для сопоставления
    direct, parametric = _match_registry_and_statement(account.id)
    audit.record(
        "bank_api.payment_registry_upload", entity_type="bank_account", entity_id=account.id,
        summary=f"Загружено файлов реестра платежей вручную для счёта {account.bank_name} "
                f"{account.checking_account}: {len(file_storages)} (успешно разобрано "
                f"{len(file_storages) - len(empty_files)}), {added} новых записей, "
                f"{direct + parametric} сопоставлено с выпиской ({direct} прямых, {parametric} параметрических)",
    )
    database.db_session.commit()
    extra = ""
    if direct or parametric:
        extra = f" Сопоставлено с выпиской: {direct} прямых + {parametric} параметрических совпадений."
    if empty_files:
        extra += " " + _("Пропущено файлов без корректных строк: {n} ({files}).").format(
            n=len(empty_files), files=", ".join(empty_files),
        )
    flash(
        _("Реестр платежей обновлён из {n} файл(ов): {added} новых записей.")
        .format(n=len(file_storages), added=added) + extra,
        "success",
    )
    return redirect(url_for("bank_sync.payment_registry", account_id=account.id))


@bp.route("/registry/payments/<int:entry_id>/allocate", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def allocate_payment_registry_entry(account_id, entry_id):
    """
    Вызывается через AJAX со страницы реестра платежей (см.
    payment_registry.html) — тот же приём, что и на странице выписки
    (allocate_statement_line): обычный POST+redirect возвращал бы на
    верх страницы, а не туда, где была запись, — при длинном списке
    записей председателю приходилось заново прокручивать страницу после
    каждого разнесения. Роут отдаёт JSON при заголовке
    X-Requested-With; обычная форма (JS отключён) по-прежнему работает
    через redirect+flash.
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def respond(success: bool, message: str, **extra):
        if is_ajax:
            return {"success": success, "message": message, **extra}
        flash(message, "success" if success else "danger")
        return redirect(url_for("bank_sync.payment_registry", account_id=account_id))

    account = _get_account(account_id)
    entry = database.db_session.get(PaymentRegistryEntry, entry_id)
    if entry is None or entry.bank_account_id != account.id:
        abort(404)
    if entry.matched_payment_id is not None:
        return respond(False, _("Эта запись уже разнесена."))

    comment = _("Импорт из реестра платежей банка (id {id})").format(id=entry.external_id)
    payment, resolved_number = _allocate_payment_to_account(
        entry.operation_date, entry.amount, comment,
        account_number=entry.account_number, payer_name=entry.payer_name, purpose=entry.payment_purpose,
    )
    if payment is None:
        return respond(
            False,
            _("Не удалось найти лицевой счёт ни по номеру, ни по имени плательщика для этой записи реестра."),
        )

    entry.matched_payment_id = payment.id
    audit.record(
        "bank_api.payment_registry_allocate", entity_type="bank_account", entity_id=account.id,
        summary=f"Запись реестра платежей ({resolved_number}, {entry.amount} ₽) разнесена платежом",
    )
    database.db_session.commit()
    return respond(True, _("Платёж разнесён."), account_number=resolved_number)


@bp.route("/match-registry-statement", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def match_registry_and_statement_manual(account_id):
    """Ручной запуск сопоставления реестра и выписки."""
    account = _get_account(account_id)
    direct, parametric = _match_registry_and_statement(account.id)
    database.db_session.commit()
    if direct or parametric:
        flash(
            _("Сопоставлено: {direct} прямых + {parametric} параметрических совпадений.").format(
                direct=direct, parametric=parametric,
            ),
            "success",
        )
    else:
        flash(_("Новых совпадений не найдено."), "info")
    return redirect(url_for("bank_sync.payment_registry", account_id=account.id))
