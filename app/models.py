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
    ForeignKey, Enum, UniqueConstraint, CheckConstraint, Index, MetaData, text
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

    registration_date: Mapped[dt.date | None] = mapped_column(Date)

    # площади (м²) — для распределения взносов пропорционально площади
    total_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))    # площадь кооператива
    garage_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # площадь, занятая гаражами
    common_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # площадь общего пользования

    # для автоматического расчёта земельного налога (см. accounting.compute_land_tax)
    standard_garage_land_area: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), default=Decimal("30"))
    land_tax_rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 3), default=Decimal("1.5"))  # % от кадастровой стоимости

    bank_fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 3))  # % банка за обслуживание счёта, напр. 1.6

    # Единый по уставу срок оплаты взносов — день и месяц в году (напр. 1 июня).
    # После этой даты по неоплаченным начислениям начинает считаться пеня
    # (см. accounting.penalty). Пока не заполнено — расчёт пени недоступен.
    dues_due_day: Mapped[int | None] = mapped_column(Integer)    # 1-31
    dues_due_month: Mapped[int | None] = mapped_column(Integer)  # 1-12

    comment: Mapped[str | None] = mapped_column(Text)


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

    # Фактический баланс именно на этом счёте — вносится вручную (нет
    # интеграции с банком). Сводный «Баланс кооператива» (на дашборде и на
    # карточке кооператива) — это именно сумма balance по всем счетам,
    # см. accounting.cooperative_balance(); отдельного поля для него в
    # Cooperative больше нет (было раньше, удалено).
    balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    balance_updated_at: Mapped[dt.date | None] = mapped_column(Date)


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

    # Баланс на момент начала учёта в системе (если отношения с
    # контрагентом уже велись раньше и на старте были не с нуля) —
    # тот же знак, что и accounting.counterparty_balance(): отрицательное
    # значит кооператив уже был должен контрагенту на эту сумму.
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    opening_balance_date: Mapped[dt.date | None] = mapped_column(Date)

    expenses: Mapped[list["Expense"]] = relationship(back_populates="counterparty")
    payments: Mapped[list["CounterpartyPayment"]] = relationship(back_populates="counterparty")
    reconciliation_acts: Mapped[list["ReconciliationAct"]] = relationship(back_populates="counterparty")


class Expense(Base):
    """Расход кооператива в адрес контрагента (снег, дорога, обслуживание и т.д.).
    document_id — подтверждающий документ (счёт/акт от контрагента)."""
    __tablename__ = "expense"

    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparty.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    category: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)

    counterparty: Mapped["Counterparty"] = relationship(back_populates="expenses")
    allocations: Mapped[list["ExpenseAllocation"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan"
    )


class CounterpartyPayment(Base):
    """
    Платёж кооператива контрагенту — зеркало Payment (там платят кооперативу,
    здесь платит кооператив). Реальное списание денег: см.
    accounting.pay_counterparty(), который на создании такого платежа уменьшает
    CounterpartyPayment.bank_account.balance на сумму платежа.
    document_id — платёжный документ (платёжка/квитанция), в отличие от
    Expense.document_id (тот — счёт/акт, подтверждающий сам факт задолженности).
    """
    __tablename__ = "counterparty_payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparty.id"), index=True)
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey("bank_account.id", ondelete="SET NULL"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"), index=True)
    comment: Mapped[str | None] = mapped_column(Text)

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

    # управление: любой член кооператива может входить в правление и/или быть председателем.
    # is_accountant отдельно от созывов правления: бухгалтера общее собрание не избирает —
    # его назначает председатель (см. governance.py: set_accountant/unset_accountant), причём
    # бухгалтер не обязан быть членом правления вообще (может быть на аутсорсе).
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

    garage: Mapped["Garage | None"] = relationship(back_populates="charges")
    account: Mapped["MemberAccount | None"] = relationship(back_populates="charges")
    fee_type: Mapped["FeeType | None"] = relationship()
    reading: Mapped["ElectricityReading | None"] = relationship(back_populates="charge")
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

    garage: Mapped["Garage | None"] = relationship(back_populates="payments")
    account: Mapped["MemberAccount | None"] = relationship(back_populates="payments")
    payer: Mapped["Person | None"] = relationship()
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
