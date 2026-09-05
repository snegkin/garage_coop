"""
Модели данных для сервиса ведения гаражного кооператива.

Стек: SQLAlchemy 2.x (Declarative), SQLite.
"""
from __future__ import annotations

import enum
import datetime as dt
import json
from decimal import Decimal

from sqlalchemy import (
    String, Integer, Numeric, Date, DateTime, Boolean, Text,
    ForeignKey, Enum, UniqueConstraint, CheckConstraint, Index, MetaData, text, event
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship
)


class Base(DeclarativeBase):
    # Именованные constraint'ы нужны для SQLite batch-режима Alembic
    # (пересоздание таблицы при ALTER TABLE) — без этого добавление нового
    # внешнего ключа к уже существующей таблице через autogenerate падает с
    # ValueError: Constraint must have a name (столкнулись на добавлении
    # master_meter_reading.expense_id). На уже существующие констрейнты не
    # влияет и лишних диффов в autogenerate не создаёт — проверено.
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


# ---------------------------------------------------------------------------
# Кооператив и контрагенты
# ---------------------------------------------------------------------------

class Cooperative(Base):
    """Юридическое лицо — сам кооператив (обычно одна запись в таблице)."""
    __tablename__ = "cooperative"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    short_name: Mapped[str | None] = mapped_column(String(100))
    inn: Mapped[str] = mapped_column(String(12), unique=True)
    kpp: Mapped[str] = mapped_column(String(9))
    ogrn: Mapped[str] = mapped_column(String(15))
    legal_address: Mapped[str | None] = mapped_column(Text)
    postal_address: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(255))

    registration_date: Mapped[dt.date | None] = mapped_column(Date)

    # площади (м²) — для распределения взносов пропорционально площади
    total_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))    # полная площадь кооператива (до приватизаций)
    common_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # площадь общего пользования (дороги и т.д.)

    # для автоматического расчёта земельного налога (см. accounting.compute_land_tax)
    # Текущая площадь кооператива на кадастровой карте — уменьшается при приватизации.
    cadastral_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Текущая кадастровая стоимость кооператива на кадастровой карте —
    # уменьшается при приватизации (вместо LandTaxYear).
    cadastral_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    standard_garage_land_area: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), default=Decimal("30"))
    land_tax_rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 3), default=Decimal("1.5"))  # % от кадастровой стоимости

    bank_fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 3))  # % банка за обслуживание счёта, напр. 1.6

    # Единый по уставу срок оплаты взносов — день и месяц в году (напр. 1 июня).
    # После этой даты по неоплаченным начислениям начинает считаться пеня
    # (см. accounting.penalty). Пока не заполнено — расчёт пени недоступен.
    dues_due_day: Mapped[int | None] = mapped_column(Integer)    # 1-31
    dues_due_month: Mapped[int | None] = mapped_column(Integer)  # 1-12

    comment: Mapped[str | None] = mapped_column(Text)

    @property
    def garage_area(self) -> "Decimal | None":
        """Площадь под гаражами = Площадь кооператива − Площадь общего пользования."""
        if self.total_area is None or self.common_area is None:
            return None
        return self.total_area - self.common_area

    @property
    def rental_price_per_sqm(self) -> "Decimal | None":
        """
        Справочная стоимость аренды 1 м² = земельный налог на 1 м² = (кадастровая
        стоимость / кадастровая площадь) × ставка налога, а не голая стоимость
        квадратного метра земли без учёта ставки — та же формула, что и у
        суммарного земельного налога (accounting.compute_land_tax:
        total_tax = cadastral_value × land_tax_rate_percent / 100), просто в
        расчёте на 1 м², без деления по гаражам/коэффициентам.
        """
        if self.cadastral_value is None or self.cadastral_area is None or self.cadastral_area == 0:
            return None
        return (self.cadastral_value / self.cadastral_area) * (self.land_tax_rate_percent / Decimal("100"))


class BankApiProvider(str, enum.Enum):
    """
    Банк, через API которого можно работать со счётом в автоматическом
    режиме. У Сбербанка, ВТБ и Т-Банка есть публичное API для организаций —
    но реализована (см. app/bank_api/) пока только интеграция со Сбербанком
    (СберБизнес). ВТБ и Т-Банк в списке уже присутствуют — председатель
    может выбрать банк заранее, UI покажет их как «скоро» — но выбор
    сохраняется просто как факт (реального клиента для них get_client()
    не создаёт, см. app/bank_api/__init__.py).
    """
    NONE = "none"
    SBERBANK = "sberbank"
    VTB = "vtb"
    TBANK = "tbank"


class BankAccount(Base):
    """
    Расчётный счёт кооператива. Может быть несколько — как в одном банке,
    так и в разных.
    """
    __tablename__ = "bank_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_name: Mapped[str] = mapped_column(String(255))
    bik: Mapped[str | None] = mapped_column(String(9))
    checking_account: Mapped[str] = mapped_column(String(20))              # р/с
    correspondent_account: Mapped[str | None] = mapped_column(String(20))  # к/с
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)       # основной счёт, для ПД-4 и т.п.
    comment: Mapped[str | None] = mapped_column(Text)

    # Банк, через API которого можно синхронизировать баланс/выписку/реестры
    # для ЭТОГО конкретного счёта. NONE (по умолчанию) — как раньше, ручной
    # ввод баланса ниже. Реквизиты подключения — в отдельной таблице
    # BankApiCredential (см. ниже), не здесь, т.к. они не нужны, пока API
    # не включён, и содержат секрет.
    api_provider: Mapped[BankApiProvider] = mapped_column(
        Enum(BankApiProvider), default=BankApiProvider.NONE, nullable=False
    )

    # Фактический баланс именно на этом счёте — по умолчанию вносится
    # вручную; если для счёта включена интеграция с API банка (api_provider
    # != NONE), обновляется автоматически синхронизацией (см. app/bank_sync.py),
    # но поле остаётся тем же самым — редактировать вручную по-прежнему можно,
    # следующая синхронизация просто перезапишет значение. Сводный «Баланс
    # кооператива» (на дашборде и на карточке кооператива) — это сумма balance
    # по всем счетам, см. accounting.cooperative_balance().
    balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    balance_updated_at: Mapped[dt.date | None] = mapped_column(Date)

    api_credential: Mapped["BankApiCredential | None"] = relationship(
        back_populates="bank_account", uselist=False, cascade="all, delete-orphan"
    )
    registry_format: Mapped["BankRegistryFormat | None"] = relationship(
        back_populates="bank_account", uselist=False, cascade="all, delete-orphan"
    )


class BankApiCredential(Base):
    """
    Реквизиты подключения к API банка для конкретного расчётного счёта —
    отдельная таблица, а не поля в BankAccount: они не нужны, пока
    api_provider на счёте — NONE, и содержат секрет, который не должен
    попадать в обычную выборку/форму счёта. Не более одной записи на счёт
    (bank_account_id уникален).

    client_secret/refresh_token хранятся не в открытом виде, а зашифрованы
    симметрично (Fernet, см. app/bank_api/crypto.py) — в отличие от пароля
    пользователя (необратимый хэш), секреты API банка нужно расшифровывать
    обратно, чтобы подставить в запрос к банку при каждой синхронизации.

    **Важно (уточнено по официальной документации Sber API, не по
    предположению): авторизация — authorization_code + refresh_token, а
    НЕ client_credentials.** Sber API работает от имени конкретного
    пользователя СберБизнес — доступ выдаётся не парой client_id/secret
    самой по себе, а через access_token/refresh_token, которые
    председатель получает один раз через Личный кабинет Sber API
    (developers.sber.ru → сервис → «Ключи доступа») и вводит здесь.
    client_id/client_secret по-прежнему нужны (аутентификация клиента при
    обновлении access_token через refresh_token — стандартное требование
    OAuth2), но одних их недостаточно. access_token живёт 60 минут —
    хранить его в БД не нужно, приложение обновляет его через
    refresh_token перед каждым обращением к банку (см.
    app/bank_api/sberbank.py: SberbankClient._get_access_token). Банк
    может при обновлении вернуть НОВЫЙ refresh_token взамен старого
    (ротация) — тогда bank_sync.py обязан сохранить его вместо старого,
    иначе следующее обновление токена не пройдёт.

    API Сбербизнес дополнительно требует клиентский mTLS-сертификат для
    самого TLS-соединения (не только токен в заголовке) и доверенные
    корневые сертификаты УЦ Сбера И УЦ Минцифры на стороне приложения —
    см. tls_cert_filename/tls_key_filename ниже и
    Config.SBERBANK_API_CA_BUNDLE. Без обоих реальные обращения к банку не
    установят TLS-соединение вообще, независимо от корректности токенов.
    """
    __tablename__ = "bank_api_credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_account.id", ondelete="CASCADE"), unique=True)

    sandbox: Mapped[bool] = mapped_column(Boolean, default=True)  # тестовый контур банка, а не промышленный
    client_id: Mapped[str | None] = mapped_column(String(255))
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    # Получается вместе с client_id/secret в Личном кабинете Sber API
    # («Ключи доступа» → сгенерировать access_token/refresh_token) —
    # действует 180 дней с момента получения, приложение обновляет
    # access_token через него перед каждым обращением к банку. См.
    # докстринг класса выше — это НЕ то же самое, что client_secret.
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    # Клиентский mTLS-сертификат — банк требует его для ЛЮБОГО обращения к
    # Sber API (не только для подписи платёжных поручений — это отдельная
    #, более редкая ЭП, её тут нет, см. app/bank_api/sberbank.py), выдаётся
    # в личном кабинете Sber API в формате PKCS#12 (.pfx/.p12) с паролем.
    # Здесь хранятся не сами файлы (это делает небезопасным чтение БД целиком
    # при бэкапе/утечке), а только имена файлов в BANK_CERTS_FOLDER — каталоге
    # ВНЕ обычного UPLOAD_FOLDER, который не отдаётся ни одним HTTP-роутом
    # (см. app/bank_sync.py: _save_client_cert). Пароль от .pfx нужен только
    # в момент загрузки/конвертации в PEM и не сохраняется вообще —
    # ни в открытом, ни в зашифрованном виде.
    tls_cert_filename: Mapped[str | None] = mapped_column(String(255))  # PEM, сертификат (+ цепочка, если была в .p12)
    tls_key_filename: Mapped[str | None] = mapped_column(String(255))  # PEM, приватный ключ (без пароля)
    # ИНН/ID организации в СберБизнес — обычно совпадает с ИНН кооператива,
    # но поле отдельное на случай расхождения (например, тестовый контур
    # банка использует отдельную тестовую организацию).
    organization_id: Mapped[str | None] = mapped_column(String(50))
    # Номер счёта для обращений к API банка, если он отличается от
    # BankAccount.checking_account (для API некоторых банков нужен именно
    # лицевой/технический номер, не р/с) — если пусто, используется checking_account.
    account_number: Mapped[str | None] = mapped_column(String(20))

    last_balance_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_statement_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)  # текст последней ошибки синхронизации, для показа в UI

    bank_account: Mapped["BankAccount"] = relationship(back_populates="api_credential")


class BankStatementLine(Base):
    """
    Одна операция из банковской выписки (зачисление/списание), полученная
    автоматически через API банка. Хранится как факт из банка отдельно от
    внутренних Payment/Charge кооператива. external_uid — уникальный номер
    операции в банке, защищает от дублей при повторной синхронизации
    одного и того же дня.

    account_number — номер лицевого счёта, распознанный в назначении
    платежа (см. bank_sync.extract_account_number — банки нередко
    вписывают его в свободный текст вида «ЛС 10640; ЧЛЕНСКИЕ ВЗНОСЫ
    (ФАМИЛИЯ И.О.);...»). Если распознан и совпадает с существующим
    лицевым счётом, зачисление (direction == "credit") автоматически
    гасит задолженность на полную зачисленную сумму — см.
    bank_sync.sync_statement/_auto_allocate_statement_line. Комиссия банка
    (если банк удержал её до зачисления) кооперативом на сумму погашения
    не переносится — платёж гасится полной суммой, поступившей по
    выписке, комиссия — расход кооператива, не недоплата члена (тот же
    принцип, что и в реестре платежей, см. PaymentRegistryEntry).
    """
    __tablename__ = "bank_statement_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_account.id", ondelete="CASCADE"), index=True)
    external_uid: Mapped[str | None] = mapped_column(String(64))
    operation_date: Mapped[dt.date] = mapped_column(Date, index=True)
    direction: Mapped[str] = mapped_column(String(6))  # "credit" (зачисление) / "debit" (списание)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    counterparty_name: Mapped[str | None] = mapped_column(String(255))
    counterparty_inn: Mapped[str | None] = mapped_column(String(12))
    payment_purpose: Mapped[str | None] = mapped_column(Text)
    document_number: Mapped[str | None] = mapped_column(String(50))
    account_number: Mapped[str | None] = mapped_column(String(20))  # лицевой счёт, распознанный в назначении платежа
    matched_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payment.id", ondelete="SET NULL"))
    matched_registry_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_registry_entry.id", ondelete="SET NULL"),
    )
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    bank_account: Mapped["BankAccount"] = relationship()
    matched_payment: Mapped["Payment | None"] = relationship()
    matched_registry: Mapped["PaymentRegistryEntry | None"] = relationship(
        foreign_keys="[BankStatementLine.matched_registry_id]",
    )

    __table_args__ = (
        UniqueConstraint("bank_account_id", "external_uid", name="uq_bank_statement_line_external_uid"),
    )


class ChargeRegistryStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class ChargeRegistryBatch(Base):
    """
    Пакет начислений, отправленный в банк реестром начислений — банк
    показывает его плательщикам (по номеру лицевого счёта — см.
    MemberAccount.account_number / PersonalAccount.account_number), они
    могут оплатить прямо в приложении банка. Статус — то, что вернул банк
    по external_id (присваивается банком при отправке).
    """
    __tablename__ = "charge_registry_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_account.id", ondelete="CASCADE"), index=True)
    period: Mapped[str] = mapped_column(String(20))  # произвольная метка периода, напр. "2026" или "август 2026"
    external_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ChargeRegistryStatus] = mapped_column(
        Enum(ChargeRegistryStatus), default=ChargeRegistryStatus.DRAFT
    )
    charges_count: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    bank_comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    bank_account: Mapped["BankAccount"] = relationship()


class PaymentRegistryEntry(Base):
    """
    Одна запись из реестра платежей, полученного из банка — платёж,
    сделанный по начислению из ChargeRegistryBatch, с номером лицевого
    счёта плательщика. matched_payment_id заполняется, когда запись
    разнесена в учёте кооператива вручную (см. app/bank_sync.py:
    allocate_payment_registry_entry — создаётся Payment и вызывается
    reallocate_garage_charges/reallocate_member_charges) — сама по себе
    запись реестра это только факт из банка, не платёж в учёте.
    """
    __tablename__ = "payment_registry_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_account.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    payer_name: Mapped[str | None] = mapped_column(String(255))
    account_number: Mapped[str | None] = mapped_column(String(20))  # лицевой счёт, распознанный в назначении платежа
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # сумма начисления — то, чем гасится долг (не за вычетом комиссии)
    operation_date: Mapped[dt.date] = mapped_column(Date)
    payment_purpose: Mapped[str | None] = mapped_column(Text)
    # Реально поступило кооперативу и удержано банком — из реального файла
    # реестра платежей это два отдельных поля (сумма начисления гасит долг
    # члена полностью, а не за вычетом комиссии — комиссия banka это
    # отдельный расход кооператива, не недоплата члена). Хранятся отдельно
    # от amount для сверки с фактическим зачислением на счёт, в разнесение
    # платежа (см. allocate_payment_registry_entry) не участвуют.
    credited_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    matched_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payment.id", ondelete="SET NULL"))
    matched_statement_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_statement_line.id", ondelete="SET NULL"),
    )
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    bank_account: Mapped["BankAccount"] = relationship()
    matched_payment: Mapped["Payment | None"] = relationship()
    matched_statement: Mapped["BankStatementLine | None"] = relationship(
        foreign_keys="[PaymentRegistryEntry.matched_statement_id]",
    )

    __table_args__ = (
        UniqueConstraint("bank_account_id", "external_id", name="uq_payment_registry_entry_external_id"),
    )


class BankRegistryFormat(Base):
    """
    Настраиваемый формат текстовых файлов реестра начислений/платежей для
    конкретного расчётного счёта — по одной записи на счёт (bank_account_id
    уникален). Точный порядок и состав полей файла зависит от конкретного
    договора с банком (см. app/bank_api/registry_file.py) — тот же принцип
    настраиваемости, что и у CsvImportProfile для CSV-импорта в мастере
    первого запуска: каталог полей + порядок, а не жёсткий формат.

    charge_columns/payment_columns — JSON-список ключей полей (из
    registry_file.CHARGE_FIELD_CATALOG/PAYMENT_FIELD_CATALOG) в порядке
    файла. Если запись отсутствует для счёта — используется
    registry_file.DEFAULT_FORMAT (реальный формат из образцов файлов,
    полученных от председателя, см. context.md).
    """
    __tablename__ = "bank_registry_format"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_account.id", ondelete="CASCADE"), unique=True)

    charge_columns: Mapped[str] = mapped_column(Text)   # JSON-список ключей
    payment_columns: Mapped[str] = mapped_column(Text)  # JSON-список ключей
    charge_decimal_separator: Mapped[str] = mapped_column(String(1), default=".")
    payment_decimal_separator: Mapped[str] = mapped_column(String(1), default=",")
    delimiter: Mapped[str] = mapped_column(String(1), default=";")
    encoding: Mapped[str] = mapped_column(String(20), default="cp1251")
    trailer_prefix: Mapped[str | None] = mapped_column(String(10), default="=")  # пусто/None — сводки в конце файла нет
    service_code: Mapped[str] = mapped_column(String(20), default="0625")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    bank_account: Mapped["BankAccount"] = relationship(back_populates="registry_format")


class Counterparty(Base):
    """Контрагент: организация или ИП, с которым кооператив расплачивается."""
    __tablename__ = "counterparty"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    inn: Mapped[str | None] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9))
    category: Mapped[str | None] = mapped_column(String(100))  # напр. "уборка снега", "электрика"
    # 120, а не 30 (как у Person.Phone.number, единственный номер в строке):
    # это одно текстовое поле без отдельных строк на каждый номер — несколько
    # телефонов контрагента вводятся через запятую в одну строку (см.
    # contact_format.phone_link — там же разбор на отдельные ссылки).
    phone: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)

    # Баланс на момент начала учёта в системе (если отношения с
    # контрагентом уже велись раньше и на старте были не с нуля) —
    # тот же знак, что и accounting.counterparty_balance(): отрицательное
    # значит кооператив уже был должен контрагенту на эту сумму.
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    opening_balance_date: Mapped[dt.date | None] = mapped_column(Date)

    expenses: Mapped[list["Expense"]] = relationship(back_populates="counterparty")
    payments: Mapped[list["CounterpartyPayment"]] = relationship(back_populates="counterparty")
    reconciliation_acts: Mapped[list["ReconciliationAct"]] = relationship(back_populates="counterparty")
    documents: Mapped[list["Document"]] = relationship(back_populates="counterparty")


class Expense(Base):
    """Расход кооператива в адрес контрагента (снег, дорога, обслуживание и т.д.).
    documents — подтверждающие документы (счета/акты от контрагента),
    их может быть НЕСКОЛЬКО на один расход: в жизни услуга иногда
    оформляется контрагентом сразу двумя УПД (например регистрация домена
    отдельным документом и аренда ПО веб-панели для его DNS — другим), а
    кооператив всё равно ведёт это одной строкой расхода. См.
    Document.expense_id — обратная сторона связи."""
    __tablename__ = "expense"

    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparty.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    category: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)

    counterparty: Mapped["Counterparty"] = relationship(back_populates="expenses")
    documents: Mapped[list["Document"]] = relationship(back_populates="expense", order_by="Document.id")
    allocations: Mapped[list["ExpenseAllocation"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan"
    )


class CounterpartyPayment(Base):
    """
    Платёж кооператива контрагенту — зеркало Payment (там платят кооперативу,
    здесь платит кооператив). Реальное списание денег: см.
    accounting.pay_counterparty(), который на создании такого платежа уменьшает
    CounterpartyPayment.bank_account.balance на сумму платежа — если только
    adjusts_bank_balance не выключен явно (см. ниже).
    document_id — платёжный документ (платёжка/квитанция), в отличие от
    Expense.documents (те — счета/акты, подтверждающие сам факт задолженности).
    """
    __tablename__ = "counterparty_payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparty.id"), index=True)
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey("bank_account.id", ondelete="SET NULL"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)
    # Альтернатива document_id — вместо прикрепления скана платёжного
    # поручения можно сослаться на уже загруженную строку выписки банка
    # (BankStatementLine, direction == "debit") как на подтверждение факта
    # платежа. Одна строка выписки может быть привязана только к одному
    # платежу — проверяется на уровне роута (app/counterparties.py), не
    # здесь (как и у matched_payment_id на самой BankStatementLine — тот же
    # принцип для зачислений от членов).
    bank_statement_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_statement_line.id", ondelete="SET NULL"), index=True,
    )
    comment: Mapped[str | None] = mapped_column(Text)
    # По умолчанию True — обычный случай, платёж вносится сразу и реально
    # списывается со счёта (см. pay_counterparty). False — платёж вносится
    # задним числом только для сохранности истории (деньги были списаны с
    # реального счёта раньше, до появления этой записи в системе) —
    # bank_account.balance в этом случае трогать не нужно, он их уже не
    # содержит. Флаг запоминается на самом платеже (а не передаётся заново
    # при каждой операции), чтобы edit_counterparty_payment/
    # reverse_counterparty_payment знали, был ли эффект на баланс, который
    # нужно отменять/переносить.
    adjusts_bank_balance: Mapped[bool] = mapped_column(Boolean, default=True)

    # Отменяющая проводка (сторно): если платёж реально ушёл в банк, но
    # обнаружилась ошибка в реквизитах/организации и деньги вернулись —
    # исходный платёж НЕ трогается (виден в истории как есть), а рядом
    # создаётся новая запись с отрицательной суммой, ссылающаяся сюда через
    # reverses_payment_id. См. accounting.reverse_counterparty_payment().
    reverses_payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("counterparty_payment.id", ondelete="SET NULL"), index=True
    )
    reverses: Mapped["CounterpartyPayment | None"] = relationship(
        remote_side="CounterpartyPayment.id", foreign_keys=[reverses_payment_id], back_populates="reversed_by"
    )
    reversed_by: Mapped[list["CounterpartyPayment"]] = relationship(
        foreign_keys=[reverses_payment_id], back_populates="reverses"
    )

    counterparty: Mapped["Counterparty"] = relationship(back_populates="payments")
    bank_account: Mapped["BankAccount | None"] = relationship()
    bank_statement_line: Mapped["BankStatementLine | None"] = relationship()
    allocations: Mapped[list["ExpenseAllocation"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan"
    )


class ExpenseAllocation(Base):
    """
    Разнесение платежа контрагенту по конкретному расходу — зеркало
    ChargeAllocation. Пересчитывается целиком заново (FIFO) функцией
    accounting.reallocate_counterparty_expenses() при каждом новом расходе
    или платеже по контрагенту — не редактируется вручную.
    """
    __tablename__ = "expense_allocation"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expense.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("counterparty_payment.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    expense: Mapped["Expense"] = relationship(back_populates="allocations")
    payment: Mapped["CounterpartyPayment"] = relationship(back_populates="allocations")


class ReconciliationAct(Base):
    """
    Акт сверки с контрагентом — периодическая сверка «сколько должны по
    нашим данным» vs «сколько по данным контрагента». Чисто информационная
    запись: на сумму расходов/платежей не влияет, только фиксирует факт
    сверки и позволяет увидеть расхождение, если оно есть.
    """
    __tablename__ = "reconciliation_act"

    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparty.id"), index=True)
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    our_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))            # наш расчёт на дату акта
    counterparty_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))   # по данным контрагента
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)
    comment: Mapped[str | None] = mapped_column(Text)

    counterparty: Mapped["Counterparty"] = relationship(back_populates="reconciliation_acts")


# ---------------------------------------------------------------------------
# Документы и управление
# ---------------------------------------------------------------------------

class DocumentType(str, enum.Enum):
    CHARTER = "charter"
    ORDER = "order"
    ACT = "act"
    LETTER = "letter"
    PROTOCOL = "protocol"
    INVOICE = "invoice"
    UTD = "utd"  # УПД (универсальный передаточный документ) / счёт-фактура — налоговый документ от контрагента, отдельно от обычного "счёта" (invoice)
    STATEMENT = "statement"
    CERTIFICATE = "certificate"
    ESTIMATE = "estimate"
    REPORT = "report"
    AGREEMENT = "agreement"
    OTHER = "other"


class Document(Base):
    """Документ кооператива: устав, приказ, акт, письмо, протокол, счёт,
    выписка, справка, смета, отчёт и т.п.

    is_internal разделяет документы на общедоступные (видны всем вошедшим
    членам кооператива — прежнее поведение) и внутренние (видны только
    правлению, is_board()) — см. app/cooperative.documents.
    """
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_type: Mapped[DocumentType] = mapped_column(Enum(DocumentType), index=True)
    number: Mapped[str | None] = mapped_column(String(50))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(500))  # оригинальное имя файла при загрузке
    comment: Mapped[str | None] = mapped_column(Text)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    counterparty_id: Mapped[int | None] = mapped_column(ForeignKey("counterparty.id", ondelete="SET NULL"), index=True)
    # Расход, к которому приложен этот документ (счёт/акт/УПД) — см.
    # Expense.documents: один расход может ссылаться на несколько
    # документов (несколько УПД под одну строку расхода). SET NULL — при
    # удалении расхода сам документ (файл) не пропадает, просто отвязывается.
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expense.id", ondelete="SET NULL"), index=True)

    counterparty: Mapped["Counterparty | None"] = relationship(back_populates="documents")
    expense: Mapped["Expense | None"] = relationship(back_populates="documents")


class BoardTerm(Base):
    """Созыв правления (избирается раз в 3 года)."""
    __tablename__ = "board_term"

    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    elected_by_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("general_meeting.id", ondelete="SET NULL"), index=True)

    elected_by_meeting: Mapped["GeneralMeeting | None"] = relationship()
    members: Mapped[list["BoardMember"]] = relationship(back_populates="term")


class BoardMember(Base):
    """Член правления в рамках конкретного созыва; председатель отмечается флагом."""
    __tablename__ = "board_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("board_term.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    is_chairman: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str | None] = mapped_column(String(100))  # напр. "секретарь", "казначей"

    term: Mapped["BoardTerm"] = relationship(back_populates="members")
    person: Mapped["Person"] = relationship()

    __table_args__ = (
        UniqueConstraint("term_id", "person_id", name="uq_board_member_term_person"),
    )


class RevisionCommission(Base):
    """
    Ревизионная комиссия — избирается общим собранием отдельно от правления
    (обычно тем же протоколом, что и созыв правления, но формально это
    самостоятельный избранный орган со своим сроком полномочий — не входит
    в BoardTerm). По уставу её члены, как правило, не должны одновременно
    быть в правлении — это не проверяется в БД как жёсткое ограничение
    (составы уставов у кооперативов отличаются), только мягким
    предупреждением в UI при добавлении.
    """
    __tablename__ = "revision_commission"

    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    elected_by_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("general_meeting.id", ondelete="SET NULL"), index=True)

    elected_by_meeting: Mapped["GeneralMeeting | None"] = relationship()
    members: Mapped[list["RevisionCommissionMember"]] = relationship(back_populates="commission")


class RevisionCommissionMember(Base):
    """Член ревизионной комиссии в рамках созыва комиссии; is_chair — председатель комиссии."""
    __tablename__ = "revision_commission_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("revision_commission.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    is_chair: Mapped[bool] = mapped_column(Boolean, default=False)  # председатель ревизионной комиссии

    commission: Mapped["RevisionCommission"] = relationship(back_populates="members")
    person: Mapped["Person"] = relationship()

    __table_args__ = (
        UniqueConstraint("commission_id", "person_id", name="uq_revision_commission_member_commission_person"),
    )


class GeneralMeeting(Base):
    """Общее собрание членов кооператива (не реже раза в месяц)."""
    __tablename__ = "general_meeting"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    agenda: Mapped[str | None] = mapped_column(Text)
    is_annual_report_meeting: Mapped[bool] = mapped_column(Boolean, default=False)

    secretary_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"), index=True)
    chairman_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"), index=True)
    protocol_document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)

    secretary: Mapped["Person | None"] = relationship(foreign_keys=[secretary_person_id])
    chairman: Mapped["Person | None"] = relationship(foreign_keys=[chairman_person_id])


class AnnualReport(Base):
    """Годовой отчёт председателя: отчёт по расходам, смета и взносы на следующий год."""
    __tablename__ = "annual_report"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("general_meeting.id"), index=True)
    spending_report_document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)
    accounting_report_document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)
    budget_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    budget_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    comment: Mapped[str | None] = mapped_column(Text)

    fee_rates: Mapped[list["FeeRate"]] = relationship(back_populates="annual_report")


class FeeRate(Base):
    """Утверждённая ставка взноса/налога на конкретный год (руб. за м² или фиксированная)."""
    __tablename__ = "fee_rate"

    id: Mapped[int] = mapped_column(primary_key=True)
    annual_report_id: Mapped[int] = mapped_column(ForeignKey("annual_report.id", ondelete="CASCADE"), index=True)
    fee_type_id: Mapped[int] = mapped_column(ForeignKey("fee_type.id"), index=True)
    rate_per_sqm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    fixed_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    annual_report: Mapped["AnnualReport"] = relationship(back_populates="fee_rates")
    fee_type: Mapped["FeeType"] = relationship()


# ---------------------------------------------------------------------------
# Электронное голосование (очно-заочное / заочное)
# ---------------------------------------------------------------------------

class VoteType(str, enum.Enum):
    IN_PERSON_AND_ABSENTEE = "in_person_and_absentee"  # очно-заочное — дополняет очную часть собрания
    ABSENTEE = "absentee"                               # заочное — без очной части вообще
    IN_PERSON = "in_person"                             # полностью очное — решение принято на собрании,
    # электронных вопросов/бюллетеней нет вовсе, единственный источник
    # результатов — прикреплённый при создании протокол (см. voting.py
    # create(): для этого типа Vote создаётся сразу в статусе CLOSED).


class VoteStatus(str, enum.Enum):
    DRAFT = "draft"    # формируется повестка, приём бюллетеней ещё не идёт
    OPEN = "open"       # приём бюллетеней идёт
    CLOSED = "closed"   # завершено, результаты зафиксированы, бюллетени больше не принимаются


class VoteChoice(str, enum.Enum):
    FOR = "for"
    AGAINST = "against"
    ABSTAIN = "abstain"


# Кворум — жёсткое правило, не настраивается за голосование: голосование
# правомочно, только если приняло участие строго больше половины от общего
# веса голосов кооператива (см. voting.quorum_met). Если кворума нет, ни
# один вопрос повестки не считается принятым, независимо от результатов.
QUORUM_THRESHOLD = Decimal("0.5")


class Vote(Base):
    """
    Голосование по одному или нескольким вопросам повестки — заочное,
    очно-заочное (альтернатива/дополнение очному собранию, когда не
    удаётся очно собрать всех собственников) или полностью очное
    (VoteType.IN_PERSON — решение уже принято на собрании, электронных
    вопросов/бюллетеней нет, результаты только в приложенном протоколе).
    Голосуют члены кооператива (владельцы гаражей); вес голоса человека =
    сумма его долей владения по всем гаражам (см. voting.person_voting_weight)
    — так что «1 гараж — 1 голос», а при нескольких собственниках гаража их
    голос делится ровно по их долям, без специального кода под этот случай.
    """
    __tablename__ = "vote"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    voting_type: Mapped[VoteType] = mapped_column(Enum(VoteType), default=VoteType.ABSENTEE)
    status: Mapped[VoteStatus] = mapped_column(Enum(VoteStatus), default=VoteStatus.DRAFT, index=True)

    meeting_id: Mapped[int | None] = mapped_column(ForeignKey("general_meeting.id", ondelete="SET NULL"), index=True)

    opens_at: Mapped[dt.datetime] = mapped_column(DateTime)
    closes_at: Mapped[dt.datetime] = mapped_column(DateTime)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)  # фактическое закрытие (может быть раньше closes_at)

    created_by_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"), index=True)
    protocol_document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)

    meeting: Mapped["GeneralMeeting | None"] = relationship()
    created_by: Mapped["Person | None"] = relationship()
    protocol_document: Mapped["Document | None"] = relationship()
    questions: Mapped[list["VoteQuestion"]] = relationship(back_populates="vote", cascade="all, delete-orphan", order_by="VoteQuestion.order")


class VoteQuestion(Base):
    """Один вопрос повестки в рамках голосования — со своим порогом принятия."""
    __tablename__ = "vote_question"

    id: Mapped[int] = mapped_column(primary_key=True)
    vote_id: Mapped[int] = mapped_column(ForeignKey("vote.id", ondelete="CASCADE"), index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    # Доля голосов "за" от ОБЩЕГО веса голосов кооператива (не от числа
    # проголосовавших), необходимая для принятия решения по этому вопросу —
    # 0.5 = простое большинство, 0.6667 ≈ квалифицированное большинство 2/3
    # и т.п. Устав может требовать разный порог для разных вопросов
    # повестки — задаётся здесь, а не жёстко в коде. Даже при формально
    # достаточной доле "за" вопрос не считается принятым, если по
    # голосованию в целом нет кворума (см. voting.question_results).
    majority_threshold: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.5"))

    vote: Mapped["Vote"] = relationship(back_populates="questions")
    ballots: Mapped[list["VoteBallot"]] = relationship(back_populates="question", cascade="all, delete-orphan")


class VoteBallot(Base):
    """
    Голос одного человека по одному вопросу. weight — вес на момент подачи
    (сумма долей владения этого человека по всем его гаражам) — фиксируется
    заново при каждой подаче/переголосовании, пока голосование открыто.
    Переголосование, пока Vote.status == OPEN, разрешено — обновляет ту же
    запись (upsert по (question_id, person_id)), не создаёт дубль.

    comment — необязательное текстовое обоснование голоса, ПУБЛИЧНОЕ
    (видно всем, не только правлению, и независимо от статуса голосования
    — в отличие от агрегированных итогов, которые до закрытия видны только
    правлению, см. voting.question_results): человек вправе аргументировать
    свою позицию перед остальными.
    """
    __tablename__ = "vote_ballot"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("vote_question.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    choice: Mapped[VoteChoice] = mapped_column(Enum(VoteChoice))
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 5))
    comment: Mapped[str | None] = mapped_column(Text)
    cast_at: Mapped[dt.datetime] = mapped_column(DateTime)

    question: Mapped["VoteQuestion"] = relationship(back_populates="ballots")
    person: Mapped["Person"] = relationship()

    __table_args__ = (
        UniqueConstraint("question_id", "person_id", name="uq_vote_ballot_question_person"),
    )


class ProposalStatus(str, enum.Enum):
    PENDING = "pending"    # ждёт решения правления
    APPROVED = "approved"  # правление одобрило — создан Vote-черновик (см. resulting_vote)
    REJECTED = "rejected"  # правление отклонило


# Срок, в течение которого правление должно рассмотреть предложенное членом
# кооператива голосование — см. proposals.resolve_if_due: решение подводится,
# как только проголосовали все члены текущего созыва правления, либо, если
# кто-то не проголосовал, по истечении этого срока с момента подачи.
PROPOSAL_REVIEW_PERIOD = dt.timedelta(days=7)


class VoteProposal(Base):
    """
    Предложение голосования от члена кооператива — формальный канал вынести
    вопрос на общее голосование, не будучи лично в правлении. Прежде чем
    стать официальным Vote с повесткой, предложение должно быть одобрено
    правлением (см. VoteProposalBoardBallot и proposals.resolve_if_due) —
    большинством ГОЛОСОВ ЧЛЕНОВ ПРАВЛЕНИЯ (по головам, а не по долям
    владения, в отличие от самого Vote). При одобрении автоматически
    создаётся Vote в статусе DRAFT с тем же названием/описанием —
    председателю остаётся сформировать повестку (вопросы) и открыть его
    обычным порядком (см. voting.py).

    Пока статус PENDING, председатель может поправить title/description
    (см. proposals.edit) — например, уточнить формулировку до решения
    правления.
    """
    __tablename__ = "vote_proposal"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProposalStatus] = mapped_column(Enum(ProposalStatus), default=ProposalStatus.PENDING, index=True)

    proposed_by_person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    resulting_vote_id: Mapped[int | None] = mapped_column(ForeignKey("vote.id", ondelete="SET NULL"), index=True)

    proposed_by: Mapped["Person"] = relationship(foreign_keys=[proposed_by_person_id])
    resulting_vote: Mapped["Vote | None"] = relationship()
    board_ballots: Mapped[list["VoteProposalBoardBallot"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan",
    )


class VoteProposalBoardBallot(Base):
    """
    Голос одного члена правления «за/против/воздержался» вынесения
    предложения на общее голосование кооператива — все три варианта
    VoteChoice, как и в обычном голосовании (voting.py); «воздержался» не
    засчитывается ни в «за», ни в «против» при подведении итога (см.
    proposals.proposal_tally/resolve_if_due). Переголосование, пока
    предложение PENDING, разрешено — upsert по (proposal_id, person_id).

    comment — необязательное текстовое обоснование, ПУБЛИЧНОЕ (видно всем,
    не только правлению, независимо от статуса предложения) — как и у
    VoteBallot.comment.
    """
    __tablename__ = "vote_proposal_board_ballot"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("vote_proposal.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    choice: Mapped[VoteChoice] = mapped_column(Enum(VoteChoice))
    comment: Mapped[str | None] = mapped_column(Text)
    voted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)

    proposal: Mapped["VoteProposal"] = relationship(back_populates="board_ballots")
    person: Mapped["Person"] = relationship()

    __table_args__ = (
        UniqueConstraint("proposal_id", "person_id", name="uq_vote_proposal_ballot_proposal_person"),
    )


# ---------------------------------------------------------------------------
# Люди
# ---------------------------------------------------------------------------

class Person(Base):
    """
    Физлицо: может быть собственником гаража, лицом для связи, членом
    правления и т.д. — одна и та же сущность используется во всех ролях.
    """
    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), index=True)
    registration_address: Mapped[str | None] = mapped_column(Text)  # адрес прописки
    residence_address: Mapped[str | None] = mapped_column(Text)     # адрес проживания
    email: Mapped[str | None] = mapped_column(String(120))
    telegram: Mapped[str | None] = mapped_column(String(120))
    vk: Mapped[str | None] = mapped_column(String(120))
    max_messenger: Mapped[str | None] = mapped_column(String(120))  # мессенджер "MAX" (VK) — имя атрибута не "max", чтобы не затенять builtin

    @property
    def short_name(self) -> str:
        """Фамилия и инициалы: «Иванов И.И.» — для официальных документов
        (подписи на печатных формах, подписи собственников под номером
        гаража и т.п.), где полное ФИО избыточно."""
        parts = self.full_name.strip().split()
        if not parts:
            return self.full_name
        surname = parts[0]
        initials = ".".join(p[0] for p in parts[1:3]) + "." if len(parts) > 1 else ""
        return f"{surname} {initials}" if initials else surname

    # паспортные данные РФ
    passport_series: Mapped[str | None] = mapped_column(String(4))
    passport_number: Mapped[str | None] = mapped_column(String(6))
    passport_issue_date: Mapped[dt.date | None] = mapped_column(Date)

    comment: Mapped[str | None] = mapped_column(Text)

    # Архив — человек скрыт из общих списков (умер, продал гараж и выбыл
    # и т.п.), но данные остаются доступны по прямой ссылке (карточка,
    # печатная выписка) — см. persons.py: archive_person/unarchive_person.
    # Синхронизировано с выбытием из собственников гаражей — при архивации
    # для каждого гаража, где человек СОвладелец, он автоматически убирается
    # из собственников (см. garages._remove_owner_and_redistribute) с этой
    # же причиной, доли оставшихся пересчитываются, остаток его лицевых
    # счетов по этому гаражу распределяется между ними пропорционально
    # новым долям. Если он был ЕДИНСТВЕННЫМ собственником — ничего не
    # трогается (ни GarageOwnership, ни лицевые счета) — гараж и счета
    # остаются как есть до появления нового собственника, автоматика
    # намеренно не решает, что делать в этом случае за председателя.
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    archived_reason: Mapped[str | None] = mapped_column(Text)  # «умер», «продал гараж» и т.п.
    archived_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))

    # членство в кооперативе (не у каждого Person обязательно есть — может быть просто контактным лицом)
    membership_start_date: Mapped[dt.date | None] = mapped_column(Date)
    membership_end_date: Mapped[dt.date | None] = mapped_column(Date)

    # управление: любой член кооператива может входить в правление и/или быть председателем.
    # is_accountant отдельно от созывов правления: бухгалтера общее собрание не избирает —
    # его назначает председатель (см. governance.py: set_accountant/unset_accountant), причём
    # бухгалтер не обязан быть членом правления вообще (может быть на аутсорсе).
    is_board_member: Mapped[bool] = mapped_column(Boolean, default=False)
    is_chairman: Mapped[bool] = mapped_column(Boolean, default=False)  # должен быть true максимум у одного Person
    is_accountant: Mapped[bool] = mapped_column(Boolean, default=False)

    phones: Mapped[list["Phone"]] = relationship(back_populates="person", cascade="all, delete-orphan")
    revisions: Mapped[list["PersonDataRevision"]] = relationship(back_populates="person", cascade="all, delete-orphan")
    archived_by: Mapped["User | None"] = relationship(foreign_keys=[archived_by_user_id])

    __table_args__ = (
        Index("uq_single_chairman", "is_chairman", unique=True, sqlite_where=text("is_chairman = 1")),
    )


class Phone(Base):
    __tablename__ = "phone"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(30))
    label: Mapped[str | None] = mapped_column(String(50))  # "мобильный", "рабочий"...

    person: Mapped["Person"] = relationship(back_populates="phones")


# ---------------------------------------------------------------------------
# Пользователи и роли (доступ к сайту)
# ---------------------------------------------------------------------------

class RoleEnum(str, enum.Enum):
    CHAIRMAN = "chairman"     # председатель — полный доступ
    BOARD = "board"           # член правления — расширенный доступ
    ACCOUNTANT = "accountant"  # бухгалтер — доступ наравне с правлением (нужен для сверки/ведения счетов)
    MEMBER = "member"         # рядовой член кооператива — свой ЛС/профиль


class User(Base):
    """Учётная запись для входа на сайт. Привязана к Person (не каждый Person — пользователь)."""
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[RoleEnum] = mapped_column(Enum(RoleEnum), default=RoleEnum.MEMBER)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    person: Mapped["Person | None"] = relationship(foreign_keys=[person_id])


class News(Base):
    """Новостная лента на главной странице (перед входом в систему).
    Ведётся правлением через /news — председатель и члены правления могут
    добавлять, редактировать и удалять записи; рядовым членам и анонимным
    посетителям доступно только чтение (см. news.py)."""
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, onupdate=dt.datetime.utcnow)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)

    author: Mapped["User | None"] = relationship()
    attachments: Mapped[list["NewsAttachment"]] = relationship(
        back_populates="news", cascade="all, delete-orphan", order_by="NewsAttachment.id"
    )


class NewsAttachment(Base):
    """Фото или файл, прикреплённый к новости. Хранится под случайным именем
    в UPLOAD_FOLDER (см. app/uploads.py:save_upload), исходное имя — для
    отображения/скачивания.

    is_inline=False (по умолчанию) — классическое вложение через блок
    «Добавить фото или файлы» в форме, показывается отдельной галереей под
    текстом (см. news/view.html).
    is_inline=True — картинка, вставленная в САМ текст через кнопку
    «Вставить картинку» (AJAX-загрузка на лету, POST /news/attachments/upload,
    см. news.py), в галерею отдельно не выводится — она уже видна в теле
    статьи через ![](url) в markdown.

    news_id nullable — при создании через AJAX-загрузку картинка попадает в
    БД РАНЬШЕ, чем сохранена сама статья (её ещё может не существовать —
    пользователь только начал писать текст): news_id=None, «осиротевшее»
    вложение. При сохранении статьи (create/edit) news.py:
    _sync_inline_attachments() разбирает markdown в body, «забирает» в
    статью все is_inline-вложения, на которые там есть ссылка (и только
    свои — author_id == текущий пользователь), и удаляет ранее забранные
    inline-вложения, ссылку на которые из текста убрали. Вложения, так и
    не попавшие ни в одну статью (черновик закрыли не сохранив), чистит
    scripts/cleanup_orphan_attachments.py по cron — см. .sh-обёртку."""
    __tablename__ = "news_attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[int | None] = mapped_column(ForeignKey("news.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)

    news: Mapped["News | None"] = relationship(back_populates="attachments")
    author: Mapped["User | None"] = relationship()

    @property
    def is_image(self) -> bool:
        ext = self.original_filename.rsplit(".", 1)[-1].lower() if "." in self.original_filename else ""
        return ext in {"jpg", "jpeg", "png", "gif", "webp"}


class WikiPage(Base):
    """Вики кооператива: справочные заметки для правления и/или всех членов
    (параметры видеонаблюдения, структура сети, телефоны контрагентов и
    аварийных служб и т.п.) — в отличие от News (лента событий/объявлений),
    это не хронологический поток, а набор страниц-справок, которые
    правятся по мере необходимости.

    is_internal — тот же принцип, что у Document (см. выше): видимость
    настраивается ПО СТРАНИЦЕ, не глобально для всей вики — часть заметок
    (например, телефоны аварийных служб) уместно показывать всем членам,
    часть (пароли от видеонаблюдения) — только правлению. Редактируют в
    любом случае только правление (см. app/wiki.py), is_internal влияет
    только на то, кто может ЧИТАТЬ.

    parent_id — дерево разделов/подразделов (self-referencing FK, глубина
    не ограничена: подраздел может сам содержать подразделы). Раздел — это
    обычная WikiPage, просто с детьми: у него тоже может быть свой текст
    (не пустая папка-заглушка), и он так же создаётся/правится/удаляется,
    как любая страница (см. app/wiki.py: единая форма для всех уровней).
    ondelete="RESTRICT" — сознательно НЕ каскад: удаление раздела с
    непустыми подразделами запрещено на уровне БД (и явной проверкой в
    app/wiki.py:delete() с понятным сообщением до похода в БД) — иначе
    случайное удаление раздела верхнего уровня беззвучно снесло бы всё
    дерево под ним. Порядок сортировки — по алфавиту (WikiPage.title) на
    каждом уровне, без отдельного поля сортировки (не требовалось —
    можно добавить позже, не меняя структуру дерева).

    Раньше вместо дерева была плоская произвольная категория (свободный
    текст) — заменена деревом при переходе на иерархическую навигацию (см.
    миграцию add_wiki_page_tree: старые значения category стали корневыми
    разделами, существующие страницы этой категории — их детьми)."""
    __tablename__ = "wiki_page"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("wiki_page.id", ondelete="RESTRICT"), index=True)
    body: Mapped[str] = mapped_column(Text)  # markdown-исходник, тот же формат, что у News.body (см. news_format.py)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, onupdate=dt.datetime.utcnow)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)

    author: Mapped["User | None"] = relationship(foreign_keys=[author_id])
    updated_by: Mapped["User | None"] = relationship(foreign_keys=[updated_by_id])
    parent: Mapped["WikiPage | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["WikiPage"]] = relationship(back_populates="parent", order_by="WikiPage.title")
    attachments: Mapped[list["WikiAttachment"]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="WikiAttachment.id"
    )


class WikiAttachment(Base):
    """Файл, привязанный к странице вики — картинка, вставленная в текст
    (![](url) в markdown тела WikiPage.body), ИЛИ обычное скачиваемое
    вложение (например конфигурация устройства), не встроенное в текст.
    is_inline различает эти два случая — тот же приём, что у NewsAttachment.

    is_inline=True — картинка, вставленная в САМ текст через кнопку
    «Вставить картинку» (AJAX-загрузка на лету, POST /wiki/attachments/upload,
    см. wiki.py), в отдельном списке файлов внизу страницы не показывается —
    она уже видна в теле статьи через ![](url) в markdown.
    is_inline=False (по умолчанию) — классическое вложение через блок
    «Добавить файлы» в форме, показывается отдельным списком под текстом
    (см. wiki/view.html) — картинки среди них тоже открываются лайтбоксом,
    но в тексте страницы не встроены.

    page_id nullable — тот же приём, что у NewsAttachment: inline-картинка
    загружается на лету (AJAX) РАНЬШЕ, чем сохранена сама страница. При
    сохранении (create/edit) wiki.py: _sync_inline_attachments() разбирает
    markdown, «забирает» is_inline-вложения, на которые есть ссылка в новом
    body (и только свои — author_id), и удаляет ранее забранные inline,
    ссылку на которые убрали из текста (файлы is_inline=False эта функция
    не трогает — ими управляют чекбоксы remove_attachment, отдельная
    логика, см. news.py: _sync_inline_attachments — тот же приём).
    «Осиротевшие» (черновик так и не сохранён) чистит
    scripts/cleanup_orphan_attachments.py по cron, как и для News.

    Видимость файла при отдаче (см. wiki.py: attachment()) наследуется от
    страницы: WikiPage.is_internal — та же логика, что и у самой страницы,
    иначе файл во внутренней странице был бы доступен по прямой ссылке в
    обход ограничения."""
    __tablename__ = "wiki_attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int | None] = mapped_column(ForeignKey("wiki_page.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    is_inline: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)

    page: Mapped["WikiPage | None"] = relationship(back_populates="attachments")
    author: Mapped["User | None"] = relationship()

    @property
    def is_image(self) -> bool:
        ext = self.original_filename.rsplit(".", 1)[-1].lower() if "." in self.original_filename else ""
        return ext in {"jpg", "jpeg", "png", "gif", "webp"}


def _delete_attachment_file(mapper, connection, target):
    """after_delete для NewsAttachment/WikiAttachment — убирает файл с диска
    вместе с удалением строки в БД. Один общий обработчик на оба вложения:
    покрывает удаление через чекбокс в форме, каскадное удаление при
    удалении новости/страницы вики и удаление «осиротевших» вложений по
    cron (scripts/cleanup_orphan_attachments.py) — раньше файлы на диске
    оставались висеть при любом из этих путей, теперь один хук для всех."""
    from flask import current_app
    import os
    try:
        folder = current_app.config["UPLOAD_FOLDER"]
    except RuntimeError:
        return  # нет активного контекста приложения — в норме такого не бывает
    try:
        os.remove(os.path.join(folder, target.stored_filename))
    except OSError:
        pass  # файла уже нет на диске — не страшно


event.listen(NewsAttachment, "after_delete", _delete_attachment_file)
event.listen(WikiAttachment, "after_delete", _delete_attachment_file)


# ---------------------------------------------------------------------------
# Гаражи
# ---------------------------------------------------------------------------

class Garage(Base):
    __tablename__ = "garage"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    area_sqm: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    coefficient: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=1)  # повышающий/понижающий коэффициент для начислений (напр. 2 — двойной гараж, 0.5 — маленький)
    land_privatized: Mapped[bool] = mapped_column(Boolean, default=False)
    cadastral_number: Mapped[str | None] = mapped_column(String(50))       # кадастровый номер гаража
    land_cadastral_number: Mapped[str | None] = mapped_column(String(50))  # кадастровый номер участка (если приватизирован)
    privatized_land_area: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))  # площадь приватизированного участка, м²
    comment: Mapped[str | None] = mapped_column(Text)
    # К какому узлу дерева контрольных счётчиков подключена ветвь этого гаража
    # (см. ControlMeter) — NULL означает подключение напрямую к вводу, минуя
    # отслеживаемый контрольный счётчик; это поведение по умолчанию для всех
    # существующих гаражей.
    control_meter_id: Mapped[int | None] = mapped_column(ForeignKey("control_meter.id", ondelete="RESTRICT"), index=True)

    ownerships: Mapped[list["GarageOwnership"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    contacts: Mapped[list["GarageContact"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    photos: Mapped[list["GaragePhoto"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    meters: Mapped[list["ElectricityMeter"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    account: Mapped["PersonalAccount | None"] = relationship(back_populates="garage", uselist=False)
    charges: Mapped[list["Charge"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    control_meter: Mapped["ControlMeter | None"] = relationship(back_populates="garages")


class GaragePhoto(Base):
    __tablename__ = "garage_photo"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    caption: Mapped[str | None] = mapped_column(String(255))

    garage: Mapped["Garage"] = relationship(back_populates="photos")


class GarageOwnership(Base):
    """Доля владения гаражом (сумма долей по гаражу должна = 1)."""
    __tablename__ = "garage_ownership"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    share: Mapped[Decimal] = mapped_column(Numeric(6, 5))  # напр. 0.5 = 50%

    garage: Mapped["Garage"] = relationship(back_populates="ownerships")
    person: Mapped["Person"] = relationship()

    __table_args__ = (
        UniqueConstraint("garage_id", "person_id", name="uq_ownership_garage_person"),
        CheckConstraint("share > 0 AND share <= 1", name="ck_share_range"),
    )


class GarageOwnershipEventType(str, enum.Enum):
    ADDED = "added"
    SHARE_CHANGED = "share_changed"
    REMOVED = "removed"


class GarageOwnershipEvent(Base):
    """
    Журнал изменений состава собственников гаража — добавление, изменение
    доли, выбытие (продал, умер, унаследовал и т.п.). Append-only, ничего
    не редактируется и не удаляется после записи.

    Это НЕ источник истины о текущих собственниках — для этого по-прежнему
    служит GarageOwnership, её семантика не меняется (там только
    актуальные владельцы, как и было раньше; весь остальной код —
    MemberAccount, доли начислений, страница гаража и т.д. — продолжает
    работать через неё без изменений). Здесь — только история: что
    произошло, когда, с какой долей и по какой причине (свободный
    комментарий), для печати и справки правлению. См.
    app/templates/garages/ownership_history.html.
    """
    __tablename__ = "garage_ownership_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[GarageOwnershipEventType] = mapped_column(Enum(GarageOwnershipEventType))
    share: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))  # доля ПОСЛЕ события; пусто для REMOVED
    comment: Mapped[str | None] = mapped_column(Text)  # причина: «продал», «умер», «унаследовал» и т.п.
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))

    garage: Mapped["Garage"] = relationship()
    person: Mapped["Person"] = relationship()
    created_by: Mapped["User | None"] = relationship()


class GarageContact(Base):
    """Лицо для связи по гаражу (может не быть собственником)."""
    __tablename__ = "garage_contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    relation: Mapped[str | None] = mapped_column(String(100))  # "супруга", "доверенное лицо"...

    garage: Mapped["Garage"] = relationship(back_populates="contacts")
    person: Mapped["Person"] = relationship()


# ---------------------------------------------------------------------------
# Электричество: счётчики/пломбы (история смен) и журнал показаний
# ---------------------------------------------------------------------------

class ElectricityMeter(Base):
    """
    Запись об установке/переустановке счётчика или переопломбировке на гараже.
    История ведётся по гаражу; актуальным считается последняя по дате (либо
    по id, если даты совпадают) запись для данного garage_id.
    """
    __tablename__ = "electricity_meter"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    meter_number: Mapped[str] = mapped_column(String(50))
    installed_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    sealed_date: Mapped[dt.date | None] = mapped_column(Date)
    initial_reading: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    meter_seal_number: Mapped[str | None] = mapped_column(String(50))       # пломба на счётчике
    breaker_seal_number: Mapped[str | None] = mapped_column(String(50))    # пломба на вводном автомате
    comment: Mapped[str | None] = mapped_column(Text)

    garage: Mapped["Garage"] = relationship(back_populates="meters")
    readings: Mapped[list["ElectricityReading"]] = relationship(back_populates="meter")


class ElectricityReading(Base):
    """Журнал учёта электроэнергии: показания конкретного счётчика на дату снятия."""
    __tablename__ = "electricity_reading"

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("electricity_meter.id", ondelete="CASCADE"), index=True)
    reading: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reading_date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # автоматически: (показание - предыдущее) * тариф на дату
    tariff: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))  # тариф ₽/кВт·ч, применённый при расчёте amount
    comment: Mapped[str | None] = mapped_column(Text)

    meter: Mapped["ElectricityMeter"] = relationship(back_populates="readings")
    charge: Mapped["Charge | None"] = relationship(back_populates="reading", uselist=False)


class ElectricityTariff(Base):
    """История тарифов на электроэнергию (руб/кВт·ч). Действующим считается тариф
    с последней effective_date, не позже даты, на которую он ищется."""
    __tablename__ = "electricity_tariff"

    id: Mapped[int] = mapped_column(primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    effective_date: Mapped[dt.date] = mapped_column(Date, index=True)
    comment: Mapped[str | None] = mapped_column(Text)


class MasterMeterReading(Base):
    """
    Показания общего (вводного) счётчика кооператива — то, что реально
    приходит от энергосбытовой компании, для сверки с суммой начислений
    по гаражам. Ведётся помесячно правлением, с возможностью приложить
    документ (счёт/акт) от энергосбыта.

    Тариф — ссылка на actual запись в electricity_tariff (не копия числа),
    выбирается автоматически по месяцу оплаты. Сумма нигде не хранится —
    считается на лету как (текущие показания − предыдущие) × ставка тарифа.
    """
    __tablename__ = "master_meter_reading"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer)  # 1-12
    reading_date: Mapped[dt.date] = mapped_column(Date, index=True)  # вычисляется из year/month (первое число месяца)
    reading: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # автоматически: (показание - предыдущее) * тариф
    tariff_id: Mapped[int] = mapped_column(ForeignKey("electricity_tariff.id"), index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    expense_id: Mapped[int | None] = mapped_column(
        ForeignKey("expense.id", ondelete="SET NULL"), unique=True, index=True
    )  # автоматически созданный расход перед поставщиком на сумму этого показания

    expense: Mapped["Expense | None"] = relationship()
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)

    tariff: Mapped["ElectricityTariff"] = relationship()
    document: Mapped["Document | None"] = relationship()


class ElectricitySettings(Base):
    """Настройки раздела «Электроэнергия» (одна запись) — прежде всего контрагент-поставщик."""
    __tablename__ = "electricity_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("counterparty.id", ondelete="SET NULL"))

    supplier: Mapped["Counterparty | None"] = relationship()


class ControlMeter(Base):
    """
    Внутренний контрольный счётчик кооператива — узел дерева сверки
    (self-referencing FK, по образцу WikiPage.parent_id). НЕ формирует
    начислений — только показания для сверки: дельта показаний узла должна
    примерно совпадать с суммой дельт его непосредственных потребителей
    (дочерних узлов и/или подключённых гаражей), расхождение — это потери
    в проводке этого конкретного сегмента (см. app/control_meters.py).

    parent_id IS NULL — узел верхнего уровня, физически подключён к вводу.
    Это НЕ объединяется с MasterMeterReading (app/power.py) в БД — сверка
    "ввод vs верхние узлы + гаражи без узла" считается отдельно, на чтение
    (см. control_meters.root_level_reconciliation).

    ondelete="RESTRICT" — как у wiki_page.parent_id: защита от случайного
    каскадного сноса поддерева на уровне БД; приложение уже не даёт удалить
    узел с детьми/гаражами явной проверкой в control_meters.delete().
    """
    __tablename__ = "control_meter"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("control_meter.id", ondelete="RESTRICT"), index=True)
    comment: Mapped[str | None] = mapped_column(Text)

    parent: Mapped["ControlMeter | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["ControlMeter"]] = relationship(back_populates="parent", order_by="ControlMeter.name")
    readings: Mapped[list["ControlMeterReading"]] = relationship(
        back_populates="control_meter", cascade="all, delete-orphan", order_by="ControlMeterReading.reading_date"
    )
    garages: Mapped[list["Garage"]] = relationship(back_populates="control_meter")


class ControlMeterReading(Base):
    """
    История показаний контрольного счётчика — только сырое показание кВт·ч,
    БЕЗ денег (в отличие от ElectricityReading/MasterMeterReading: контрольные
    счётчики не формируют Charge/Expense, только сверку). Дельта к предыдущей
    по времени записи считается на лету, тем же приёмом, что
    power._readings_with_amounts, но без умножения на тариф.
    """
    __tablename__ = "control_meter_reading"

    id: Mapped[int] = mapped_column(primary_key=True)
    control_meter_id: Mapped[int] = mapped_column(ForeignKey("control_meter.id", ondelete="CASCADE"), index=True)
    reading: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    reading_date: Mapped[dt.date] = mapped_column(Date, index=True)
    comment: Mapped[str | None] = mapped_column(Text)

    control_meter: Mapped["ControlMeter"] = relationship(back_populates="readings")


class CsvImportProfile(Base):
    """
    Настраиваемый формат CSV-файла для импорта в мастере первого запуска
    (см. app/setup_wizard.py) — по одной записи на тип импорта ("people",
    "garages"). columns — JSON-список ключей колонок в том порядке, в
    котором председатель выгружает их в свой CSV; сохраняется, чтобы не
    настраивать формат заново при повторном/дополнительном импорте.
    """
    __tablename__ = "csv_import_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_type: Mapped[str] = mapped_column(String(50), unique=True)
    columns: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class KeyRate(Base):
    """
    История ключевой ставки ЦБ РФ (% годовых) — используется для автоматического
    расчёта пени по просроченным взносам членов (см. accounting.penalty):
    сумма долга × ставка × (1/300 первые 30 дней просрочки, 1/150 — с 31-го) ×
    дни просрочки. Действующей на дату считается запись с последней
    effective_date, не позже этой даты — по аналогии с ElectricityTariff.
    Заполняется автоматически по расписанию с cbr.ru (см. penalty.fetch_key_rates),
    либо вручную (is_manual=True), если сайт ЦБ недоступен.
    """
    __tablename__ = "key_rate"

    id: Mapped[int] = mapped_column(primary_key=True)
    rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    effective_date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)


class LandTaxYear(Base):
    """
    Кадастровая стоимость земли кооператива (за вычетом приватизированных
    участков) на конкретный год — присылается налоговой ежегодно и меняется.
    Используется для автоматического расчёта земельного налога на гараж,
    см. accounting.compute_land_tax().
    """
    __tablename__ = "land_tax_year"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, unique=True)
    cadastral_value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    comment: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Бухгалтерия: лицевые счета, начисления, платежи
# ---------------------------------------------------------------------------

class FeeType(Base):
    """Справочник видов начислений: земельный налог, членский взнос, целевой взнос, электричество..."""
    __tablename__ = "fee_type"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)   # "land_tax", "membership", "target", "electricity"
    name: Mapped[str] = mapped_column(String(150))
    comment: Mapped[str | None] = mapped_column(Text)

    # Если задан type_code — при добавлении собственника гаражу автоматически
    # заводится персональный лицевой счёт на этот вид взноса (см. accounting.py).
    # Электричество сюда не относится — у него отдельный, гаражный лицевой счёт.
    type_code: Mapped[str | None] = mapped_column(String(5))
    is_penalty: Mapped[bool] = mapped_column(Boolean, default=False)  # пеня по этому виду взноса


class PersonalAccount(Base):
    """
    Лицевой счёт на электричество, привязан к гаражу (не к человеку — ровно один
    счёт на гараж, без разбивки между собственниками). Для остальных видов
    начислений (земельный налог, членские взносы и т.п.) используются
    MemberAccount — они привязаны к конкретному члену кооператива, т.к. сумма
    зависит от его доли владения.
    """
    __tablename__ = "personal_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id"), unique=True)  # уже индексирован как unique
    account_number: Mapped[str] = mapped_column(String(30), unique=True)
    opened_date: Mapped[dt.date] = mapped_column(Date, default=dt.date.today)

    garage: Mapped["Garage"] = relationship(back_populates="account")


class Charge(Base):
    """
    Начисление за год — либо на гараж (garage_id, напр. электричество),
    либо на лицевой счёт члена кооператива (account_id → MemberAccount,
    напр. земельный налог, членский взнос). Ровно одно из двух должно
    быть заполнено (см. ck_charge_target).
    """
    __tablename__ = "charge"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int | None] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("member_account.id", ondelete="CASCADE"), index=True)
    fee_type_id: Mapped[int | None] = mapped_column(ForeignKey("fee_type.id"), index=True)  # для гаражных начислений; у счёта члена вид взноса уже задан на самом счёте
    year: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    annual_report_id: Mapped[int | None] = mapped_column(ForeignKey("annual_report.id", ondelete="SET NULL"), index=True)
    reading_id: Mapped[int | None] = mapped_column(
        ForeignKey("electricity_reading.id", ondelete="SET NULL"), index=True
    )  # связь с показанием счётчика, из которого начисление рассчитано автоматически
    comment: Mapped[str | None] = mapped_column(Text)

    # Только для обычных (не пенных) начислений члена кооператива, по которым
    # уже считалась пеня — дата, по которую (включительно) пеня уже начислена
    # и проведена отдельными Charge на счёт пени. Следующий пересчёт продолжит
    # с этой даты, а не с нуля. См. accounting.penalty.accrue_penalties().
    penalty_calculated_through: Mapped[dt.date | None] = mapped_column(Date)
    # Обратная связь: если этот Charge — сама пеня, здесь id начисления, за
    # просрочку которого она посчитана (для отображения происхождения).
    penalty_for_charge_id: Mapped[int | None] = mapped_column(
        ForeignKey("charge.id", ondelete="SET NULL"), index=True
    )
    # Человек, упомянутый по ФИО в comment (напр. «Долг, унаследованный от
    # прежнего собственника (Иванов И.И.)» — см. accounting.
    # transfer_member_account_balance/redistribute_member_account_balance).
    # Только для того, чтобы в шаблоне превратить это ФИО в ссылку на
    # карточку человека (правлению) — не влияет на расчёты.
    related_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"), index=True)

    garage: Mapped["Garage | None"] = relationship(back_populates="charges")
    account: Mapped["MemberAccount | None"] = relationship(back_populates="charges")
    fee_type: Mapped["FeeType | None"] = relationship()
    reading: Mapped["ElectricityReading | None"] = relationship(back_populates="charge")
    related_person: Mapped["Person | None"] = relationship(foreign_keys=[related_person_id])
    allocations: Mapped[list["ChargeAllocation"]] = relationship(
        back_populates="charge", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "(garage_id IS NOT NULL) + (account_id IS NOT NULL) = 1",
            name="ck_charge_target",
        ),
    )


class Payment(Base):
    """
    Платёж — либо на гараж (garage_id, напр. оплата электричества),
    либо на лицевой счёт члена кооператива (account_id → MemberAccount).
    Ровно одно из двух должно быть заполнено (см. ck_payment_target).
    """
    __tablename__ = "payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_id: Mapped[int | None] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("member_account.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    payer_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"), index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    # См. Charge.related_person_id — тот же приём, для тех же
    # автосгенерированных комментариев про унаследованный долг/переплату,
    # только со стороны Payment.
    related_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"), index=True)
    # Заполняется только для платежа, созданного зачётом между лицевыми
    # счетами (см. finance.transfer_member_account_funds) — id того самого
    # Charge, что зачёт завёл ПАРНО на счёте-источнике. Без этой связи
    # удаление такого платежа (finance.delete_member_payment) стирало бы
    # только его половину, оставляя начисление на счёте-источнике висеть
    # без соответствующего платежа на другой стороне — деньги "терялись"
    # бы для владельца счёта-источника. См. finance.cancel_transfer,
    # которая по этой ссылке удаляет обе половины зачёта разом.
    offset_charge_id: Mapped[int | None] = mapped_column(ForeignKey("charge.id", ondelete="SET NULL"), index=True)

    garage: Mapped["Garage | None"] = relationship(back_populates="payments")
    account: Mapped["MemberAccount | None"] = relationship(back_populates="payments")
    payer: Mapped["Person | None"] = relationship(foreign_keys=[payer_person_id])
    related_person: Mapped["Person | None"] = relationship(foreign_keys=[related_person_id])
    offset_charge: Mapped["Charge | None"] = relationship(foreign_keys=[offset_charge_id])
    allocations: Mapped[list["ChargeAllocation"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "(garage_id IS NOT NULL) + (account_id IS NOT NULL) = 1",
            name="ck_payment_target",
        ),
    )


class ChargeAllocation(Base):
    """
    Разнесение платежа по конкретному начислению — какая часть какого Payment
    закрывает какой Charge. Пересчитывается целиком заново (FIFO: старые
    начисления закрываются старыми платежами) функцией accounting.reallocate_garage_charges()
    при каждом новом начислении/платеже по гаражу — не редактируется вручную.
    """
    __tablename__ = "charge_allocation"

    id: Mapped[int] = mapped_column(primary_key=True)
    charge_id: Mapped[int] = mapped_column(ForeignKey("charge.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payment.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    charge: Mapped["Charge"] = relationship(back_populates="allocations")
    payment: Mapped["Payment"] = relationship(back_populates="allocations")


class AccountNumberSettings(Base):
    """
    Настройки формата номеров лицевых счетов (одна запись в таблице).
    Позволяет расширить/сузить номер и сменить префиксы без изменения кода —
    см. accounting.py, где эти настройки используются при генерации номеров.
    При изменении настроек правление может пересчитать уже существующие
    номера под новый формат (см. finance.regenerate_account_numbers).
    """
    __tablename__ = "account_number_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    garage_digits: Mapped[int] = mapped_column(Integer, default=3)   # ширина номера гаража, напр. 3 -> "095"
    owner_digits: Mapped[int] = mapped_column(Integer, default=1)    # ширина порядкового номера собственника
    electricity_prefix: Mapped[str] = mapped_column(String(10), default="0")
    penalty_prefix: Mapped[str] = mapped_column(String(10), default="П")


class MemberAccount(Base):
    """
    Лицевой счёт члена кооператива на конкретный вид взноса/налога по
    конкретному гаражу (сумма зависит от доли владения этим гаражом).
    У одного человека может быть несколько таких счетов — по числу гаражей
    и видов взносов. Номер счёта формируется в accounting.py.

    is_archived — счёт больше не актуален (прежний собственник выбыл, а
    гараж затем обзавёлся новым — см. garages._archive_owner_accounts):
    счёт остаётся привязанным к прежнему person_id для истории начислений
    и платежей, но по нему больше ничего не начисляется, а его номер
    (account_number) переходит новому, свежесозданному счёту нового
    собственника — отсюда частичный уникальный индекс ниже вместо
    обычного UNIQUE на account_number: одинаковый номер у активного и
    архивного счёта — норма, у двух АКТИВНЫХ — нет.
    """
    __tablename__ = "member_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    fee_type_id: Mapped[int] = mapped_column(ForeignKey("fee_type.id"), index=True)
    account_number: Mapped[str] = mapped_column(String(20))
    opened_date: Mapped[dt.date] = mapped_column(Date, default=dt.date.today)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)

    person: Mapped["Person"] = relationship()
    garage: Mapped["Garage"] = relationship()
    fee_type: Mapped["FeeType"] = relationship()
    charges: Mapped[list["Charge"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        # Обычный UniqueConstraint здесь не годится: после архивации счёта
        # (см. is_archived выше) у того же человека по тому же гаражу и
        # виду взноса может позже появиться НОВЫЙ активный счёт (например,
        # он вновь стал собственником спустя годы) — старый архивный не
        # должен этому мешать. Поэтому обе уникальности — и по человеку/
        # гаражу/виду взноса, и по номеру счёта — только среди активных.
        Index(
            "uq_member_account_active", "person_id", "garage_id", "fee_type_id", unique=True,
            sqlite_where=text("NOT is_archived"), postgresql_where=text("NOT is_archived"),
        ),
        Index(
            "uq_member_account_number_active", "account_number", unique=True,
            sqlite_where=text("NOT is_archived"), postgresql_where=text("NOT is_archived"),
        ),
    )


class PersonDataRevisionStatus(str, enum.Enum):
    PENDING = "pending"   # предложение ожидает рассмотрения
    APPROVED = "approved" # председатель одобрил
    REJECTED = "rejected" # председатель отклонил


class PersonDataRevision(Base):
    """
    Предложенные изменения персональных данных членом кооператива.
    Член через ЛК предлагает изменения — создаётся ревизия со статусом pending.
    Председатель одобряет (status=approved) или отклоняет (status=rejected).
    Актуальные данные всегда соответствуют последней одобренной ревизии
    (либо начальным данным, если одобренных нет).
    """
    __tablename__ = "person_data_revision"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    submitted_by_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    status: Mapped[PersonDataRevisionStatus] = mapped_column(Enum(PersonDataRevisionStatus), default=PersonDataRevisionStatus.PENDING)
    fields_snapshot: Mapped[str] = mapped_column(Text)  # JSON: полные значения изменяемых полей на момент отправки
    # JSON: те же поля со значениями ДО отправки — снимок текущей карточки Person
    # на момент создания ревизии. Нужен, чтобы «Было/Стало» в истории (см.
    # persons._revision_diff_rows) не «съезжало»: после одобрения карточка
    # Person уже содержит те же данные, что и fields_snapshot, поэтому сравнивать
    # с ЖИВЫМИ данными person для уже рассмотренных ревизий нельзя — nullable,
    # т.к. у ревизий, созданных до этого поля, снимка «было» нет (см. fallback
    # на текущие данные person в _revision_diff_rows).
    previous_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    reviewer_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), index=True)

    person: Mapped["Person"] = relationship(back_populates="revisions")
    submitter: Mapped["User"] = relationship(foreign_keys=[submitted_by_user_id])
    reviewer: Mapped["User | None"] = relationship(foreign_keys=[reviewer_user_id])


class PD4Document(Base):
    """История сформированных платёжек ПД-4 с QR-кодом (для отчётности/аудита).
    Ровно одно из account_id/personal_account_id должно быть заполнено —
    платёжка либо по взносу/налогу члена (MemberAccount), либо по
    электричеству гаража (PersonalAccount)."""
    __tablename__ = "pd4_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("member_account.id", ondelete="CASCADE"), index=True)
    personal_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_account.id", ondelete="CASCADE"), index=True
    )
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey("bank_account.id", ondelete="SET NULL"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    qr_payload: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    account: Mapped["MemberAccount | None"] = relationship()
    personal_account: Mapped["PersonalAccount | None"] = relationship()
    bank_account: Mapped["BankAccount | None"] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(account_id IS NOT NULL) + (personal_account_id IS NOT NULL) = 1",
            name="ck_pd4_document_target",
        ),
    )


# ---------------------------------------------------------------------------
# Журнал аудита
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """
    Журнал действий с деньгами/доступом кооператива: кто, когда и что сделал
    (начисления, платежи, изменения ролей, вход в систему и т.п.). Не
    редактируется руками — только через audit.record() (см. app/audit.py).
    actor_username/actor_role — снимок на момент действия, а не FK-джойн:
    учётка могла с тех пор смениться/быть отвязана, а запись в журнале
    должна остаться читаемой как есть (юридически значимая история).
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    actor_username: Mapped[str | None] = mapped_column(String(80))
    actor_role: Mapped[str | None] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(50), index=True)  # напр. "payment.create", "role.change", "auth.login_failed"
    entity_type: Mapped[str | None] = mapped_column(String(50), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    summary: Mapped[str] = mapped_column(Text)  # человекочитаемое описание, уже готовое к показу в журнале

    actor: Mapped["User | None"] = relationship()


# ---------------------------------------------------------------------------
# Мониторинг электроэнергии по фазам (eWeLink POWCT)
# ---------------------------------------------------------------------------
#
# Отдельная подсистема от MasterMeterReading/ElectricityMeter выше:
# те — ручной помесячный ввод показаний для биллинга, эта — автоматический
# снимок текущей мощности/напряжения/тока по 3 фазам вводного щита с
# устройств Sonoff POWCT через облако eWeLink, для страницы мониторинга на
# сайте. Показания сюда не участвуют в начислениях/расчётах с поставщиком.

class EWeLinkAccount(Base):
    """
    Данные подключения к облаку eWeLink — одна запись (singleton, как
    ElectricitySettings выше). Хранится в БД, редактируется председателем
    через страницу мониторинга (см. app/electricity_monitor.py), а не в
    .env — чтобы можно было сменить без деплоя.

    Авторизация — официальный OAuth2 Open API eWeLink (authorization code
    flow, приложение зарегистрировано и одобрено на dev.ewelink.cc). До
    этого использовался неофициальный вход по email/паролю (как в сторонних
    клиентах Home Assistant SonoffLAN, ewelink-api) — модерация заявки на
    dev.ewelink.cc могла занять до нескольких дней, и на этапе постановки
    задачи это было сознательно отклонено; после одобрения заявки модуль
    переписан на официальный флоу (см. app/ewelink/client.py). app_id/
    app_secret теперь — client id/secret приложения на dev.ewelink.cc, а не
    учётные данные пользователя: пароль от аккаунта eWeLink в это
    приложение вообще не попадает, председатель логинится напрямую на
    странице авторизации eWeLink в браузере.

    app_secret хранится зашифрованным (Fernet, см. app.bank_api.crypto —
    модуль назван по банку исторически, но шифрование в нём общего
    назначения, ключ из SECRET_KEY/BANK_API_ENCRYPTION_KEY; переиспользуем,
    а не заводим копию).

    family_id — выбранный «дом» (family) в терминах eWeLink Open API,
    обязателен для GET /v2/device/thing (без него не получить список
    устройств) — заполняется отдельным шагом после первой авторизации, см.
    electricity_monitor.py:save_family.

    access_token/refresh_token — тоже шифруются; access_token живёт
    ограниченное время (у eWeLink — обычно порядка 30 дней, ТОЧНО НЕ
    ПОДТВЕРЖДЕНО живым тестом), refresh-логика должна persist-ить новый
    refresh_token сразу после успешного client.refresh(), даже если
    последующий запрос статуса устройства упадёт — тот же принцип, что и
    для Sberbank (см. BankApiCredential выше).
    """
    __tablename__ = "ewelink_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[str | None] = mapped_column(String(255))
    app_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    family_id: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(4))  # eu/us/as/cn — уточняется при авторизации

    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_obtained_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    last_poll_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)  # текст последней ошибки опроса, для показа в UI


class PowerPhaseDevice(Base):
    """Одно устройство Sonoff POWCT, привязанное к фазе вводного щита."""
    __tablename__ = "power_phase_device"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(100))  # "Фаза A" / "Фаза B" / "Фаза C"
    ewelink_device_id: Mapped[str] = mapped_column(String(32), unique=True)  # deviceid в eWeLink
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # временно исключить из опроса, не удаляя историю

    readings: Mapped[list["PowerPhaseReading"]] = relationship(back_populates="device")


class PowerPhaseReading(Base):
    """
    Один снимок показаний с устройства. Пишется поллером раз в минуту (см.
    scripts/poll_ewelink.py) — не начисление и не биллинг, только текущее
    состояние для мониторинга.

    Все поля — уже разобранные/масштабированные значения из params ответа
    eWeLink (см. app.ewelink.client.parse_phase_snapshot), не сырой JSON —
    имена и масштаб полей подтверждены живым тестом на оборудовании
    заказчика (см. README.md), поэтому больше не нужно хранить raw_params
    целиком про запас на случай, если разбор окажется неверным.
    """
    __tablename__ = "power_phase_reading"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("power_phase_device.id", ondelete="CASCADE"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    voltage_v: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    current_a: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    day_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    month_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_online: Mapped[bool] = mapped_column(Boolean, default=True)
    sled_online: Mapped[bool | None] = mapped_column(Boolean)  # статусный светодиод устройства (params.sledOnline)
    switch_on: Mapped[bool | None] = mapped_column(Boolean)  # состояние реле (params.switches[0].switch)

    device: Mapped["PowerPhaseDevice"] = relationship(back_populates="readings")

    __table_args__ = (
        Index("ix_power_phase_reading_device_ts", "device_id", "ts"),
    )


class DvrRecorder(Base):
    """
    Видеорегистратор (DVR/NVR) — физическое устройство с несколькими
    камерами, выставленное наружу по RTSP на нестандартном порту (см.
    app/surveillance.py). Раздел «Видеонаблюдение» пока показывает только
    периодически обновляемые превью-кадры (см. DvrCamera), не живое видео.
    """
    __tablename__ = "dvr_recorder"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=554)
    username: Mapped[str | None] = mapped_column(String(100))
    # Шифруется тем же приёмом, что и секреты API банка/токены eWeLink —
    # см. app/bank_api/crypto.py (общий на все три случая, несмотря на имя
    # модуля/пакета).
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str | None] = mapped_column(Text)

    cameras: Mapped[list["DvrCamera"]] = relationship(
        back_populates="recorder", cascade="all, delete-orphan", order_by="DvrCamera.sort_order",
    )


class DvrCamera(Base):
    """
    Одна камера на регистраторе. channel/stream — номер канала и номер
    потока в RTSP-пути регистратора (`channel={channel}_stream={stream}.sdp`,
    см. app/surveillance.py:rtsp_url) — у большинства DVR/NVR 0 — основной
    поток высокого разрешения, 1+ — дополнительные (более низкого
    разрешения, экономичнее для превью, но выбор потока — на усмотрение
    председателя при настройке, не задаётся жёстко).
    """
    __tablename__ = "dvr_camera"

    id: Mapped[int] = mapped_column(primary_key=True)
    recorder_id: Mapped[int] = mapped_column(ForeignKey("dvr_recorder.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    channel: Mapped[int] = mapped_column(Integer)
    stream: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Заполняются cron-скриптом (scripts/dvr_snapshot.py) на каждой попытке
    # снять кадр — last_error НЕ очищает последний удачный снимок с диска
    # (см. surveillance.snapshot_path) — при временной ошибке камера
    # продолжает показывать последний удачный кадр с пометкой, что он мог
    # устареть, а не пропадает с экрана вовсе.
    last_snapshot_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)

    recorder: Mapped["DvrRecorder"] = relationship(back_populates="cameras")


# ---------------------------------------------------------------------------
# Почта правления
# ---------------------------------------------------------------------------
#
# Один общий почтовый ящик правления (напр. pravlenie@...), не личная почта
# отдельных членов — председатель один раз вводит параметры подключения,
# правление читает/пишет письма через этот ящик. Письма нигде не кэшируются
# в БД (см. app/mail_client.py) — только сами параметры подключения.

class MailProtocol(str, enum.Enum):
    IMAP = "imap"
    POP3 = "pop3"


class MailEncryption(str, enum.Enum):
    SSL = "ssl"            # неявный TLS с начала соединения (обычно порты 993/995/465)
    STARTTLS = "starttls"  # соединение открытое, потом апгрейд (обычно порты 143/110/587)
    NONE = "none"           # без шифрования — для локальных/тестовых серверов


class MailboxSettings(Base):
    """Единственная запись — параметры подключения к общему ящику правления.
    Пароль общий для входящих (IMAP/POP3) и исходящих (SMTP) — обычный
    случай одного почтового аккаунта. Шифруется тем же Fernet, что и
    DvrRecorder.password_encrypted/EWeLinkAccount.app_secret_encrypted (см.
    app/bank_api/crypto.py — модуль общего назначения, несмотря на путь)."""
    __tablename__ = "mailbox_settings"

    id: Mapped[int] = mapped_column(primary_key=True)

    incoming_protocol: Mapped[MailProtocol] = mapped_column(Enum(MailProtocol), default=MailProtocol.IMAP)
    incoming_host: Mapped[str | None] = mapped_column(String(255))
    incoming_port: Mapped[int] = mapped_column(Integer, default=993)
    incoming_encryption: Mapped[MailEncryption] = mapped_column(Enum(MailEncryption), default=MailEncryption.SSL)

    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_encryption: Mapped[MailEncryption] = mapped_column(Enum(MailEncryption), default=MailEncryption.STARTTLS)

    username: Mapped[str | None] = mapped_column(String(255))
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    from_name: Mapped[str | None] = mapped_column(String(255))

    # Папка "Отправленные" (IMAP) — SMTP сам не сохраняет копию письма
    # никуда, поэтому после отправки мы сами дописываем её сюда через IMAP
    # APPEND (см. mail_client.send_message: _save_to_sent_folder). Имя
    # папки у разных провайдеров разное ("Sent", "Отправленные", "[Gmail]/
    # Sent Mail") — председатель указывает его сам, автоопределение не
    # делаем (соответствует общему принципу — без привязки к провайдеру).
    # NULL/пусто — копии в "Отправленные" не сохраняются. У POP3 нет
    # папок вовсе — поле игнорируется для этого протокола.
    sent_folder: Mapped[str | None] = mapped_column(String(255), default="Sent")

    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
