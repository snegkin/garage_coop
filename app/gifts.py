"""
Поощрение членов кооператива подарками — деньгами или освобождением от
уплаты взносов (дарение — ст. 572 ГК РФ, в т.ч. «освобождение от
имущественной обязанности перед дарителем»).

Подарок от кооператива одному человеку — не более GIFT_LIMIT за
календарный год по всем его лицевым счетам и способам (деньгами и в счёт
взносов): до 4000 ₽ подарки не облагаются НДФЛ (п. 28 ст. 217 НК РФ).
Подарок — расход статьи GIFT_CATEGORY на контрагенте на имя человека
(как начисление к выплате, см. year_report.add_expense) с этим человеком
в Expense.person_id; по ним и считается лимит.

Подарок в счёт взносов — две связанные записи, без движения денег:
- Payment на лицевом счёте (gift_expense_id → расход) гасит начисление;
- расход-подарок у контрагента-человека с собственной оплатой
  «зачётом» (CounterpartyPayment без банка, Expense.booked_payment_id) —
  он идёт в «Смету и факт» и не висит в «К выплате людям».
Удаляются/отменяются только вместе (cancel_dues_gift).
"""
import datetime as dt
from decimal import Decimal

from . import database
from .accounting import balance, pay_counterparty, reallocate_counterparty_expenses, reallocate_member_charges
from .models import BudgetItem, CounterpartyPayment, Expense, ExpenseCategory, ExpenseKind, MemberAccount, Payment, Person

GIFT_LIMIT = Decimal("4000")
GIFT_CATEGORY = "Поощрения"
GIFT_COLOR = "#c9a227"
# Комментарии — на языке кооператива, как и у остальных проводок.
GIFT_PAYMENT_COMMENT = "Подарок: освобождение от уплаты взносов"
GIFT_SETTLEMENT_COMMENT = "Зачёт в счёт взносов (подарок)"
# Пеня и транзитные платежи (земельный налог, электричество, возмещение
# госпошлины) — не взносы кооператива: подарком не закрываются.
_NOT_GIFTABLE_CODES = {"land_tax", "electricity", "telecom_disputes"}


class GiftError(ValueError):
    """Текст — для пользователя."""


def gifts_total(person: Person, year: int, exclude_expense_id: int | None = None) -> Decimal:
    """Сколько кооператив подарил человеку за год — деньгами и в счёт
    взносов, по всем лицевым счетам (расходы статьи «Поощрения» с ним)."""
    query = database.db_session.query(Expense).filter(
        Expense.person_id == person.id, Expense.category == GIFT_CATEGORY,
        Expense.kind == ExpenseKind.CHARGE,
        Expense.date >= dt.date(year, 1, 1), Expense.date <= dt.date(year, 12, 31),
    )
    if exclude_expense_id is not None:
        query = query.filter(Expense.id != exclude_expense_id)
    return sum((e.amount for e in query), Decimal("0"))


def gift_limit_left(person: Person, year: int, exclude_expense_id: int | None = None) -> Decimal:
    return max(GIFT_LIMIT - gifts_total(person, year, exclude_expense_id), Decimal("0"))


def check_gift_limit(person: Person, date: dt.date, amount: Decimal, exclude_expense_id: int | None = None) -> None:
    left = gift_limit_left(person, date.year, exclude_expense_id)
    if amount > left:
        from .i18n import fmt2, translate as _
        raise GiftError(_(
            "Подарки одному человеку — не более {limit} ₽ за год (без НДФЛ). {name} в {year} году уже "
            "подарено {given} ₽, осталось {left} ₽."
        ).format(limit=fmt2(GIFT_LIMIT), name=person.short_name, year=date.year,
                 given=fmt2(GIFT_LIMIT - left), left=fmt2(left)))


def ensure_gift_category() -> None:
    """Статья «Поощрения» в справочнике (если справочник уже заведён в БД —
    иначе действует встроенный, там она есть)."""
    rows = database.db_session.query(ExpenseCategory).all()
    if not rows or any(c.name == GIFT_CATEGORY for c in rows):
        return
    database.db_session.add(ExpenseCategory(
        name=GIFT_CATEGORY, color=GIFT_COLOR, keywords="поощрен, подар",
        position=max(c.position for c in rows if not c.is_default) + 1 if any(not c.is_default for c in rows) else 0,
    ))
    database.db_session.flush()


def can_gift_dues(account: MemberAccount) -> bool:
    fee_type = account.fee_type
    return fee_type is not None and not fee_type.is_penalty and fee_type.code not in _NOT_GIFTABLE_CODES


def is_gift_expense(expense: Expense) -> bool:
    return database.db_session.query(Payment).filter(Payment.gift_expense_id == expense.id).first() is not None


def is_gift_settlement(payment: CounterpartyPayment) -> bool:
    """Оплата «зачётом» расхода-подарка в счёт взносов."""
    expense = database.db_session.query(Expense).filter(Expense.booked_payment_id == payment.id).first()
    return expense is not None and is_gift_expense(expense)


def create_dues_gift(account: MemberAccount, date: dt.date, amount: Decimal, decision: str,
                     budget_item: BudgetItem | None = None) -> Payment:
    """Подарок освобождением от уплаты взносов на этом счёте (см. докстринг
    модуля). decision — «решение правления № 3 от 20.11.2026». Не больше
    долга по счёту (переплата подарком — уже не освобождение от
    обязанности) и не больше остатка лимита на человека за год."""
    from .i18n import fmt2, translate as _
    from .year_report import _person_counterparty

    if not can_gift_dues(account):
        raise GiftError(_("Подарком закрываются только взносы кооператива — не пеня и не транзитные платежи."))
    if amount is None or amount <= 0:
        raise GiftError(_("Укажите сумму подарка."))
    decision = (decision or "").strip()
    if not decision:
        raise GiftError(_("Укажите решение правления о поощрении (номер и дата протокола)."))
    debt = -balance(account)
    if amount > debt:
        raise GiftError(_("Подарок в счёт взносов — не больше долга по счёту ({debt} ₽).").format(
            debt=fmt2(max(debt, Decimal("0")))))
    person = account.person
    check_gift_limit(person, date, amount)

    ensure_gift_category()
    counterparty = _person_counterparty(person)
    expense = Expense(
        counterparty_id=counterparty.id, date=date, amount=amount, category=GIFT_CATEGORY,
        description=f"Подарок — освобождение от уплаты взносов, л/с {account.account_number} ({decision})"[:2000],
        kind=ExpenseKind.CHARGE, person_id=person.id, budget_item_id=budget_item.id if budget_item else None,
    )
    database.db_session.add(expense)
    database.db_session.flush()
    settlement = pay_counterparty(counterparty, date, amount, comment=GIFT_SETTLEMENT_COMMENT, adjust_balance=False)
    expense.booked_payment_id = settlement.id
    payment = Payment(account_id=account.id, date=date, amount=amount,
                      comment=f"{GIFT_PAYMENT_COMMENT} ({decision})", gift_expense_id=expense.id)
    database.db_session.add(payment)
    database.db_session.flush()
    reallocate_member_charges(account)
    reallocate_counterparty_expenses(counterparty)
    return payment


def cancel_dues_gift(payment: Payment) -> None:
    """Отменить подарок в счёт взносов целиком: платёж на счёте, расход-
    подарок и его оплату «зачётом»."""
    account = payment.account
    expense = database.db_session.get(Expense, payment.gift_expense_id) if payment.gift_expense_id else None
    counterparty = expense.counterparty if expense is not None else None
    settlement = expense.booked_payment if expense is not None else None
    database.db_session.delete(payment)
    if expense is not None:
        expense.booked_payment_id = None
        database.db_session.flush()
        database.db_session.delete(expense)
    if settlement is not None:
        database.db_session.delete(settlement)
    database.db_session.flush()
    reallocate_member_charges(account)
    if counterparty is not None:
        reallocate_counterparty_expenses(counterparty)
