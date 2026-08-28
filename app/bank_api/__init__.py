"""
Фабрика клиента API банка по расчётному счёту — единственное место, которое
знает, какие банки реально поддержаны. app/bank_sync.py вызывает только
get_client() и не импортирует конкретные клиенты банков напрямую.

Добавление нового банка (когда дойдут руки до ВТБ/Т-Банка — у обоих есть
публичное API для организаций, см. models.BankApiProvider): реализовать
BankApiClient в отдельном модуле рядом с sberbank.py и добавить сюда одну
строку в SUPPORTED_PROVIDERS. Само поле BankAccount.api_provider уже
позволяет их выбрать — get_client() для них сейчас всегда вернёт None,
и app/bank_sync.py показывает пользователю понятное сообщение об этом
(«интеграция ещё не реализована»), а не падает.
"""
from __future__ import annotations

import json
import os

from flask import current_app

from ..models import BankAccount, BankApiProvider, BankRegistryFormat
from . import crypto
from .base import BankApiClient
from .sberbank import SberbankClient
from . import registry_file

SUPPORTED_PROVIDERS = {BankApiProvider.SBERBANK}


def build_registry_format(bank_account: BankAccount) -> registry_file.RegistryFormat:
    """Собирает настройку формата файлов реестров для счёта из
    BankRegistryFormat (см. app/bank_sync.py: registry_format-роуты) —
    либо реальный формат из образцов файлов (DEFAULT_FORMAT), если
    председатель формат для этого счёта ещё не настраивал. Используется и
    здесь (для автоматической отправки/получения через API), и напрямую в
    app/bank_sync.py для ручного скачивания/загрузки файла — одна и та же
    настройка для обоих путей, чтобы они не расходились."""
    row = bank_account.registry_format
    if row is None:
        return registry_file.DEFAULT_FORMAT
    try:
        charge_columns = json.loads(row.charge_columns)
        payment_columns = json.loads(row.payment_columns)
    except (ValueError, TypeError):
        return registry_file.DEFAULT_FORMAT
    return registry_file.RegistryFormat(
        charge_columns=charge_columns or registry_file.DEFAULT_CHARGE_COLUMNS,
        payment_columns=payment_columns or registry_file.DEFAULT_PAYMENT_COLUMNS,
        charge_decimal_separator=row.charge_decimal_separator or ".",
        payment_decimal_separator=row.payment_decimal_separator or ",",
        delimiter=row.delimiter or ";",
        encoding=row.encoding or "cp1251",
        trailer_prefix=row.trailer_prefix or None,
        service_code=row.service_code or "",
    )


def get_client(bank_account: BankAccount) -> BankApiClient | None:
    """None означает «для этого счёта нет рабочей интеграции» — либо API не
    выбран (NONE), либо банк выбран, но клиент ещё не реализован (VTB/TBANK),
    либо не заполнены обязательные реквизиты подключения (включая
    клиентский mTLS-сертификат и refresh_token — без них СберБизнес не даст
    даже авторизоваться, см. комментарии в sberbank.py)."""
    if bank_account.api_provider not in SUPPORTED_PROVIDERS:
        return None
    cred = bank_account.api_credential
    if cred is None or not cred.client_id or not cred.client_secret_encrypted or not cred.refresh_token_encrypted:
        return None
    secret = crypto.decrypt(cred.client_secret_encrypted)
    refresh_token = crypto.decrypt(cred.refresh_token_encrypted)
    if not secret or not refresh_token:
        return None

    if bank_account.api_provider == BankApiProvider.SBERBANK:
        client_cert = None
        if cred.tls_cert_filename and cred.tls_key_filename:
            certs_dir = current_app.config["BANK_CERTS_FOLDER"]
            client_cert = (
                os.path.join(certs_dir, cred.tls_cert_filename),
                os.path.join(certs_dir, cred.tls_key_filename),
            )
        return SberbankClient(
            client_id=cred.client_id,
            client_secret=secret,
            refresh_token=refresh_token,
            account_number=cred.account_number or bank_account.checking_account,
            sandbox=cred.sandbox,
            base_url=current_app.config.get("SBERBANK_API_BASE_URL"),
            # Точный token_url подтверждается в личном кабинете разработчика
            # при подключении (см. комментарий в sberbank.py) — переопределяется
            # переменной окружения, чтобы не редактировать код при уточнении.
            token_url=current_app.config.get("SBERBANK_API_TOKEN_URL"),
            client_cert=client_cert,
            ca_bundle=current_app.config.get("SBERBANK_API_CA_BUNDLE"),
            registry_format=build_registry_format(bank_account),
        )
    return None
