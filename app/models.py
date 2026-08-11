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
    ForeignKey, Enum, UniqueConstraint, CheckConstraint, Index, text
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship
)


class Base(DeclarativeBase):
    pass


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

    registration_date: Mapped[dt.date | None] = mapped_column(Date)

    # площади (м²) — для распределения взносов пропорционально площади
    total_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))    # площадь кооператива
    garage_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # площадь, занятая гаражами
    common_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # площадь общего пользования

    # для автоматического расчёта земельного налога (см. accounting.compute_land_tax)
    standard_garage_land_area: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), default=Decimal("30"))
    land_tax_rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 3), default=Decimal("1.5"))  # % от кадастровой стоимости

    bank_fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 3))  # % банка за обслуживание счёта, напр. 1.6

    comment: Mapped[str | None] = mapped_column(Text)


class BankAccount(Base):
    """
    Расчётный счёт кооператива. Может быть несколько — как в одном банке,
    так и в разных. Собственного баланса не хранит: баланс по внутреннему
    учёту считается динамически суммой по лицевым счетам, см.
    accounting.cooperative_balance() — используется на дашборде.
    """
    __tablename__ = "bank_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_name: Mapped[str] = mapped_column(String(255))
    bik: Mapped[str | None] = mapped_column(String(9))
    checking_account: Mapped[str] = mapped_column(String(20))              # р/с
    correspondent_account: Mapped[str | None] = mapped_column(String(20))  # к/с
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)       # основной счёт, для ПД-4 и т.п.
    comment: Mapped[str | None] = mapped_column(Text)


class Counterparty(Base):
    """Контрагент: организация или ИП, с которым кооператив расплачивается."""
    __tablename__ = "counterparty"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    inn: Mapped[str | None] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9))
    category: Mapped[str | None] = mapped_column(String(100))  # напр. "уборка снега", "электрика"
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)

    expenses: Mapped[list["Expense"]] = relationship(back_populates="counterparty")


class Expense(Base):
    """Расход кооператива в адрес контрагента (снег, дорога, обслуживание и т.д.)."""
    __tablename__ = "expense"

    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparty.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    category: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)

    counterparty: Mapped["Counterparty"] = relationship(back_populates="expenses")


# ---------------------------------------------------------------------------
# Документы и управление
# ---------------------------------------------------------------------------

class DocumentType(str, enum.Enum):
    CHARTER = "charter"
    ORDER = "order"
    ACT = "act"
    LETTER = "letter"
    PROTOCOL = "protocol"
    OTHER = "other"


class Document(Base):
    """Внутренний документ: устав, приказ, акт, письмо, протокол."""
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_type: Mapped[DocumentType] = mapped_column(Enum(DocumentType), index=True)
    number: Mapped[str | None] = mapped_column(String(50))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(String(500))
    comment: Mapped[str | None] = mapped_column(Text)


class BoardTerm(Base):
    """Созыв правления (избирается раз в 3 года)."""
    __tablename__ = "board_term"

    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    elected_by_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("general_meeting.id", ondelete="SET NULL"), index=True)

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

    # паспортные данные РФ
    passport_series: Mapped[str | None] = mapped_column(String(4))
    passport_number: Mapped[str | None] = mapped_column(String(6))
    passport_issued_by: Mapped[str | None] = mapped_column(Text)
    passport_issue_date: Mapped[dt.date | None] = mapped_column(Date)
    passport_department_code: Mapped[str | None] = mapped_column(String(7))

    comment: Mapped[str | None] = mapped_column(Text)

    # членство в кооперативе (не у каждого Person обязательно есть — может быть просто контактным лицом)
    membership_start_date: Mapped[dt.date | None] = mapped_column(Date)
    membership_end_date: Mapped[dt.date | None] = mapped_column(Date)

    # управление: любой член кооператива может входить в правление и/или быть председателем
    is_board_member: Mapped[bool] = mapped_column(Boolean, default=False)
    is_chairman: Mapped[bool] = mapped_column(Boolean, default=False)  # должен быть true максимум у одного Person
    is_accountant: Mapped[bool] = mapped_column(Boolean, default=False)

    phones: Mapped[list["Phone"]] = relationship(back_populates="person", cascade="all, delete-orphan")
    revisions: Mapped[list["PersonDataRevision"]] = relationship(back_populates="person", cascade="all, delete-orphan")

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

    person: Mapped["Person | None"] = relationship()


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

    ownerships: Mapped[list["GarageOwnership"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    contacts: Mapped[list["GarageContact"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    photos: Mapped[list["GaragePhoto"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    meters: Mapped[list["ElectricityMeter"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    account: Mapped["PersonalAccount | None"] = relationship(back_populates="garage", uselist=False)
    charges: Mapped[list["Charge"]] = relationship(back_populates="garage", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="garage", cascade="all, delete-orphan")


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
    comment: Mapped[str | None] = mapped_column(Text)

    meter: Mapped["ElectricityMeter"] = relationship(back_populates="readings")


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
    reading: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    tariff_id: Mapped[int] = mapped_column(ForeignKey("electricity_tariff.id"), index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)

    tariff: Mapped["ElectricityTariff"] = relationship()
    document: Mapped["Document | None"] = relationship()


class ElectricitySettings(Base):
    """Настройки раздела «Электроэнергия» (одна запись) — прежде всего контрагент-поставщик."""
    __tablename__ = "electricity_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("counterparty.id", ondelete="SET NULL"))

    supplier: Mapped["Counterparty | None"] = relationship()


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
    comment: Mapped[str | None] = mapped_column(Text)

    garage: Mapped["Garage | None"] = relationship(back_populates="charges")
    account: Mapped["MemberAccount | None"] = relationship(back_populates="charges")
    fee_type: Mapped["FeeType | None"] = relationship()

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

    garage: Mapped["Garage | None"] = relationship(back_populates="payments")
    account: Mapped["MemberAccount | None"] = relationship(back_populates="payments")
    payer: Mapped["Person | None"] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(garage_id IS NOT NULL) + (account_id IS NOT NULL) = 1",
            name="ck_payment_target",
        ),
    )


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
    """
    __tablename__ = "member_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    garage_id: Mapped[int] = mapped_column(ForeignKey("garage.id", ondelete="CASCADE"), index=True)
    fee_type_id: Mapped[int] = mapped_column(ForeignKey("fee_type.id"), index=True)
    account_number: Mapped[str] = mapped_column(String(20), unique=True)
    opened_date: Mapped[dt.date] = mapped_column(Date, default=dt.date.today)

    person: Mapped["Person"] = relationship()
    garage: Mapped["Garage"] = relationship()
    fee_type: Mapped["FeeType"] = relationship()
    charges: Mapped[list["Charge"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("person_id", "garage_id", "fee_type_id", name="uq_member_account"),
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
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    reviewer_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), index=True)

    person: Mapped["Person"] = relationship(back_populates="revisions")
    submitter: Mapped["User"] = relationship(foreign_keys=[submitted_by_user_id])
    reviewer: Mapped["User | None"] = relationship(foreign_keys=[reviewer_user_id])


class PD4Document(Base):
    """История сформированных платёжек ПД-4 с QR-кодом (для отчётности/аудита)."""
    __tablename__ = "pd4_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("member_account.id", ondelete="CASCADE"), index=True)
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey("bank_account.id", ondelete="SET NULL"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    qr_payload: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    account: Mapped["MemberAccount"] = relationship()
    bank_account: Mapped["BankAccount | None"] = relationship()
