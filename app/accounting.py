"""
Формирование номеров лицевых счетов по настраиваемой схеме кооператива
(см. модель AccountNumberSettings — правление может менять ширину номера
гаража, ширину порядкового номера собственника и префиксы прямо в UI).

Номер гаража в формуле — ID гаража в БД (целое число), а не текстовый
номер гаража (который может быть нечисловым, напр. «21н»). Это гарантирует
уникальность и корректное дополнение нулями.

По умолчанию:
- Электричество (счёт на гараж, один на гараж):
    0{id:03d}0            например, id 95 -> 00950

- Взнос/налог (счёт на члена кооператива, по конкретному гаражу и виду
  взноса — type_code вида взноса, затем номер гаража, затем порядковый
  номер собственника этого гаража, начиная с 0, чтобы у совладельцев были
  разные счета):
    {type_code}{id:03d}{№ собственника}   например, 10950, 20950

- Пеня по такому взносу/налогу — тот же номер с префиксом (по умолчанию "П"):
    П10950, П20950
"""
import calendar
import datetime as dt
from decimal import Decimal

from sqlalchemy import func

from . import database
from .models import (
    AccountNumberSettings, ElectricityTariff, ElectricitySettings, Cooperative, Garage, LandTaxYear,
    Charge, Payment, MemberAccount, FeeType, ChargeAllocation, KeyRate,
    Counterparty, Expense, CounterpartyPayment, ExpenseAllocation, BankAccount, GarageOwnership,
)


def get_settings() -> AccountNumberSettings:
    """Возвращает единственную запись настроек, создавая её со значениями по умолчанию при первом обращении."""
    settings = database.db_session.query(AccountNumberSettings).first()
    if settings is None:
        settings = AccountNumberSettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


def _garage_digits(garage_id: int, width: int) -> str:
    """ID гаража в БД — всегда целое число, дополняем нулями до нужной ширины."""
    digits = str(garage_id)
    if len(digits) > width:
        return digits[-width:]
    return digits.zfill(width)


def electricity_account_number(garage_id: int, settings: AccountNumberSettings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.electricity_prefix}{_garage_digits(garage_id, settings.garage_digits)}{'0' * settings.owner_digits}"


def member_account_number(
    fee_type_code: str, garage_id: int, owner_index: int, is_penalty: bool,
    settings: AccountNumberSettings | None = None,
) -> str:
    settings = settings or get_settings()
    owner_part = str(owner_index % (10 ** settings.owner_digits)).zfill(settings.owner_digits)
    base = f"{fee_type_code}{_garage_digits(garage_id, settings.garage_digits)}{owner_part}"
    return f"{settings.penalty_prefix}{base}" if is_penalty else base


def owner_index_for(garage_id: int, person_id: int) -> int:
    """Порядковый номер человека среди собственников гаража (по порядку
    добавления записи о владении — id GarageOwnership, не по алфавиту и
    не по доле) — используется в формуле номера счёта члена
    (member_account_number), чтобы у совладельцев одного гаража были
    разные номера счетов. 0, если человек не входит в собственники этого
    гаража (защитный случай — не должен происходить в норме, но лучше
    вернуть детерминированное значение, чем упасть)."""
    ownerships = (
        database.db_session.query(GarageOwnership)
        .filter_by(garage_id=garage_id)
        .order_by(GarageOwnership.id)
        .all()
    )
    return next((i for i, o in enumerate(ownerships) if o.person_id == person_id), 0)


def balance(account) -> Decimal:
    """Работает для любой сущности с .charges/.payments (Garage — начисления на гараж, и MemberAccount)."""
    charged = sum((c.amount for c in account.charges), Decimal("0"))
    paid = sum((p.amount for p in account.payments), Decimal("0"))
    return paid - charged  # отрицательное = долг, положительное = переплата


def charge_sort_date(charge: Charge) -> dt.date:
    """Дата начисления для сортировки/закрытия долгов: дата снятия показаний,
    если начисление связано со счётчиком, иначе 1 января года начисления (для
    начислений, добавленных вручную без привязки к конкретному показанию)."""
    if charge.reading is not None:
        return charge.reading.reading_date
    return dt.date(charge.year, 1, 1)


def _reallocate_fifo(charges: list[Charge], payments: list[Payment]) -> None:
    """
    Общая механика FIFO-разнесения: старые начисления закрываются старыми
    платежами. `charges`/`payments` должны быть уже отсортированы по дате.
    Используется и для гаражей (электричество), и для лицевых счетов членов
    (взносы/налоги/пеня) — см. reallocate_garage_charges/reallocate_member_charges.
    """
    for charge in charges:
        charge.allocations.clear()
    for payment in payments:
        payment.allocations.clear()
    database.db_session.flush()

    payments_iter = iter(payments)
    current_payment = next(payments_iter, None)
    payment_left = current_payment.amount if current_payment else Decimal("0")

    for charge in charges:
        charge_left = charge.amount
        while charge_left > 0 and current_payment is not None:
            if payment_left <= 0:
                current_payment = next(payments_iter, None)
                payment_left = current_payment.amount if current_payment else Decimal("0")
                continue
            alloc_amount = min(charge_left, payment_left)
            database.db_session.add(ChargeAllocation(
                charge_id=charge.id,
                payment_id=current_payment.id,
                amount=alloc_amount,
            ))
            charge_left -= alloc_amount
            payment_left -= alloc_amount


def reallocate_garage_charges(garage: Garage) -> None:
    """
    Пересчитывает разнесение всех платежей гаража по всем его начислениям
    заново, от нуля: старые начисления закрываются старыми платежами (FIFO
    по обеим сторонам). Вызывается после добавления любого нового начисления
    или платежа на гараж — идемпотентна, ничего не портит при повторном вызове,
    и на первом же вызове сама «доразносит» всю уже существующую историю.
    """
    charges = sorted(garage.charges, key=charge_sort_date)
    payments = sorted(garage.payments, key=lambda p: p.date)
    _reallocate_fifo(charges, payments)


def reallocate_member_charges(account: MemberAccount) -> None:
    """
    То же самое, что reallocate_garage_charges(), но для лицевого счёта члена
    кооператива (взносы/налоги/пеня). До появления автоматического начисления
    пени платежи членов не разносились по конкретным начислениям — баланс
    считался просто суммой (см. balance()); теперь разнесение нужно, чтобы
    accounting.penalty мог точно посчитать непогашенный остаток каждого
    конкретного начисления на каждый день просрочки. Вызывается после
    добавления любого нового начисления или платежа на счёт члена.
    """
    charges = sorted(account.charges, key=charge_sort_date)
    payments = sorted(account.payments, key=lambda p: p.date)
    _reallocate_fifo(charges, payments)


def charge_paid_amount(charge: Charge) -> Decimal:
    return sum((a.amount for a in charge.allocations), Decimal("0"))


def receivables_balance() -> Decimal:
    """
    Сумма балансов всех лицевых счетов (электричество по гаражам + взносы/
    налоги по членам) — сколько кооперативу должны/переплатили члены.
    Это НЕ баланс на дашборде правления (см. cooperative_balance ниже) —
    это внутренний учётный остаток по расчётам с членами, отдельная вещь.
    """
    total_charged = database.db_session.query(func.coalesce(func.sum(Charge.amount), 0)).scalar()
    total_paid = database.db_session.query(func.coalesce(func.sum(Payment.amount), 0)).scalar()
    return Decimal(total_paid) - Decimal(total_charged)


def cooperative_balance() -> Decimal:
    """
    Баланс кооператива для дашборда правления — фактическая сумма на
    расчётных счетах (BankAccount.balance, вносится вручную, банк
    напрямую не подключён). Это НЕ то же самое, что receivables_balance()
    (ожидаемые поступления от членов по лицевым счетам) — тот отражает,
    сколько кооперативу должны, а этот — сколько денег реально есть.
    """
    total = database.db_session.query(func.coalesce(func.sum(BankAccount.balance), 0)).scalar()
    return Decimal(total)


# ---------------------------------------------------------------------------
# Расчёты с контрагентами (Expense/CounterpartyPayment/ExpenseAllocation) —
# зеркало Charge/Payment/ChargeAllocation выше: там кооперативу платят,
# здесь платит кооператив.
# ---------------------------------------------------------------------------

def reallocate_counterparty_expenses(counterparty: Counterparty) -> None:
    """
    Пересчитывает разнесение всех платежей контрагенту по всем расходам
    перед ним заново, от нуля (FIFO по обеим сторонам, сортировка по дате).
    Вызывается после добавления/изменения любого расхода или платежа —
    идемпотентна, ничего не портит при повторном вызове. Точная копия
    reallocate_garage_charges(), только в обратную сторону.

    Отменяющие проводки (сторно, см. reverse_counterparty_payment) сами по
    себе в разнесении не участвуют — их сумма (отрицательная) присоединяется
    к исходному платежу, который они отменяют, и относительно этой
    эффективной суммы и идёт FIFO. Так исходный платёж и сторно к нему
    остаются двумя отдельными видимыми строками в истории, но на баланс и
    на статус «оплачено/не оплачено» у расходов влияют как единое целое.
    """
    expenses = sorted(counterparty.expenses, key=lambda e: e.date)

    reversal_totals: dict[int, Decimal] = {}
    for p in counterparty.payments:
        if p.reverses_payment_id is not None:
            reversal_totals[p.reverses_payment_id] = (
                reversal_totals.get(p.reverses_payment_id, Decimal("0")) + p.amount
            )
    originals = sorted(
        (p for p in counterparty.payments if p.reverses_payment_id is None),
        key=lambda p: p.date,
    )

    for expense in expenses:
        expense.allocations.clear()
    for payment in counterparty.payments:
        payment.allocations.clear()
    database.db_session.flush()

    def _effective(payment):
        return payment.amount + reversal_totals.get(payment.id, Decimal("0"))

    payments_iter = iter(originals)
    current_payment = next(payments_iter, None)
    payment_left = _effective(current_payment) if current_payment else Decimal("0")

    for expense in expenses:
        expense_left = expense.amount
        while expense_left > 0 and current_payment is not None:
            if payment_left <= 0:
                current_payment = next(payments_iter, None)
                payment_left = _effective(current_payment) if current_payment else Decimal("0")
                continue
            alloc_amount = min(expense_left, payment_left)
            database.db_session.add(ExpenseAllocation(
                expense_id=expense.id,
                payment_id=current_payment.id,
                amount=alloc_amount,
            ))
            expense_left -= alloc_amount
            payment_left -= alloc_amount


def expense_paid_amount(expense: Expense) -> Decimal:
    return sum((a.amount for a in expense.allocations), Decimal("0"))


def counterparty_balance(counterparty: Counterparty) -> Decimal:
    """Баланс расчётов с контрагентом = начальный баланс + payments - expenses
    (сторно уже входят в payments отрицательной суммой). Тот же знак, что и
    balance() для гаражей/членов: отрицательное — кооператив ещё должен
    контрагенту, положительное — переплата (аванс, редкий случай)."""
    charged = sum((e.amount for e in counterparty.expenses), Decimal("0"))
    paid = sum((p.amount for p in counterparty.payments), Decimal("0"))
    opening = counterparty.opening_balance or Decimal("0")
    return opening + paid - charged


def pay_counterparty(
    counterparty: Counterparty,
    date,
    amount: Decimal,
    bank_account: BankAccount | None = None,
    document_id: int | None = None,
    comment: str | None = None,
) -> CounterpartyPayment:
    """
    Оплата контрагенту: создаёт CounterpartyPayment, при указанном
    bank_account сразу списывает эту сумму с его фактического баланса
    (BankAccount.balance — вносится вручную, банк не подключён напрямую),
    и пересчитывает разнесение платежей по расходам этого контрагента.
    Коммит — на вызывающей стороне (как и у reallocate_garage_charges).
    """
    payment = CounterpartyPayment(
        counterparty_id=counterparty.id,
        bank_account_id=bank_account.id if bank_account else None,
        date=date,
        amount=amount,
        document_id=document_id,
        comment=comment,
    )
    database.db_session.add(payment)
    if bank_account is not None:
        bank_account.balance = (bank_account.balance or Decimal("0")) - amount
        bank_account.balance_updated_at = date
    database.db_session.flush()
    reallocate_counterparty_expenses(counterparty)
    return payment


def edit_counterparty_payment(
    payment: CounterpartyPayment,
    date,
    amount: Decimal,
    bank_account: BankAccount | None,
    document_id: int | None = None,
    comment: str | None = None,
) -> None:
    """
    Правка уже внесённого платежа (например, ошиблись в сумме при вводе).
    Если платёж был привязан к счёту, сначала возвращает старую сумму
    на старый счёт, затем списывает новую сумму с нового (может быть тем
    же самым) счёта — чтобы баланс счёта не «поплыл» при повторных правках.
    Используется только для последнего платежа контрагента — ограничение
    накладывается на уровне роута (app/counterparties.py), не здесь.
    """
    if payment.bank_account is not None:
        payment.bank_account.balance = (payment.bank_account.balance or Decimal("0")) + payment.amount

    payment.date = date
    payment.amount = amount
    payment.bank_account_id = bank_account.id if bank_account else None
    if document_id is not None:
        payment.document_id = document_id
    payment.comment = comment

    if bank_account is not None:
        bank_account.balance = (bank_account.balance or Decimal("0")) - amount
        bank_account.balance_updated_at = date

    database.db_session.flush()
    reallocate_counterparty_expenses(payment.counterparty)


def reverse_counterparty_payment(payment: CounterpartyPayment, date, comment: str | None = None) -> CounterpartyPayment:
    """
    Отменяющая проводка (сторно): для платежа, который реально ушёл в банк,
    но потом обнаружилась ошибка в реквизитах/организации, и деньги
    вернулись — банком или самим контрагентом. Исходный платёж НЕ
    трогается (виден в истории как есть — он реально был), рядом
    создаётся новая запись с отрицательной суммой, которая компенсирует
    его эффект на баланс контрагента и возвращает деньги на счёт списания
    (если он был указан). Один платёж можно сторнировать только один раз —
    проверка на уровне роута (app/counterparties.py).
    """
    reversal = CounterpartyPayment(
        counterparty_id=payment.counterparty_id,
        bank_account_id=payment.bank_account_id,
        date=date,
        amount=-payment.amount,
        reverses_payment_id=payment.id,
        comment=comment,
    )
    database.db_session.add(reversal)
    if payment.bank_account is not None:
        payment.bank_account.balance = (payment.bank_account.balance or Decimal("0")) + payment.amount
        payment.bank_account.balance_updated_at = date
    database.db_session.flush()
    reallocate_counterparty_expenses(payment.counterparty)
    return reversal


def get_electricity_settings() -> ElectricitySettings:
    """Единственная запись настроек раздела «Электроэнергия» (прежде всего — поставщик)."""
    settings = database.db_session.query(ElectricitySettings).first()
    if settings is None:
        settings = ElectricitySettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


def current_tariff(as_of: dt.date | None = None) -> ElectricityTariff | None:
    """Действующий тариф на указанную дату (по умолчанию — сегодня): последняя
    запись, у которой effective_date не позже этой даты."""
    as_of = as_of or dt.date.today()
    return (
        database.db_session.query(ElectricityTariff)
        .filter(ElectricityTariff.effective_date <= as_of)
        .order_by(ElectricityTariff.effective_date.desc(), ElectricityTariff.id.desc())
        .first()
    )


def compute_land_tax(year: int) -> dict[int, Decimal] | None:
    """
    Автоматический расчёт земельного налога на гараж.

    Формула:
    - чистая налогооблагаемая площадь = cadastral_area (текущая площадь
      кооператива на кадастровой карте, уже без приватизированных участков)
    - налог за 1 м2 = (cadastral_value / чистая площадь) * ставка налога, %
      (без промежуточного округления)
    - налог на общую территорию (на гараж) = налог за 1 м2 * (common_area / количество гаражей)
      - эту часть платят АБСОЛЮТНО ВСЕ (и приватизированные, и нет)
    - налог под самим гаражом = налог за 1 м2 * standard_garage_land_area
      - эту часть платят ТОЛЬКО владельцы НЕприватизированных гаражей
    - ИТОГО для неприватизированного гаража: (налог на общую территорию + налог под гаражом) * (1 + % банка)
    - ИТОГО для приватизированного гаража: ТОЛЬКО налог на общую территорию
      (участок уже приватизирован, платится напрямую государству)
    - Результат умножается на коэффициент гаража (coefficient)
    - Далее сумма делится между собственниками по их долям.

    Возвращает {garage_id: сумма} или None, если не хватает исходных данных
    (не указаны cadastral_area или cadastral_value).
    """
    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        return None
    if coop.cadastral_area is None or coop.cadastral_value is None:
        return None
    if coop.common_area is None:
        return None

    garages = database.db_session.query(Garage).all()
    garage_count = len(garages)
    if garage_count == 0:
        return {}

    net_taxable_area = coop.cadastral_area
    if net_taxable_area <= 0:
        return None

    total_tax = coop.cadastral_value * (coop.land_tax_rate_percent / Decimal("100"))
    price_per_sqm = total_tax / net_taxable_area  # полная точность, без промежуточного округления

    common_area_tax = price_per_sqm * (coop.common_area / garage_count)
    standard_area = coop.standard_garage_land_area or Decimal("30")
    bank_multiplier = Decimal("1") + (coop.bank_fee_percent or Decimal("0")) / Decimal("100")

    result = {}
    for garage in garages:
        if garage.land_privatized:
            # Только доля в налоге за дороги — участок уже приватизирован.
            garage_tax = common_area_tax
        else:
            under_building = standard_area * price_per_sqm
            garage_tax = (under_building + common_area_tax) * bank_multiplier
        # Коэффициент гаража (напр. 2 — двойной гараж, 0.5 — маленький)
        garage_tax = garage_tax * garage.coefficient
        result[garage.id] = garage_tax.quantize(Decimal("0.01"))
    return result


def dues_due_date(coop: Cooperative, year: int) -> dt.date | None:
    """
    Дата, до которой должен быть оплачен взнос за данный год (единая по
    уставу дата в году — см. Cooperative.dues_due_day/dues_due_month).
    None, если срок не настроен в реквизитах кооператива. День подрезается
    до последнего дня месяца, если в этом году короче (напр. 30/31 февраля).
    """
    if not coop or not coop.dues_due_day or not coop.dues_due_month:
        return None
    last_day = calendar.monthrange(year, coop.dues_due_month)[1]
    return dt.date(year, coop.dues_due_month, min(coop.dues_due_day, last_day))


def key_rate_on(as_of: dt.date) -> KeyRate | None:
    """Действующая на указанную дату запись ключевой ставки ЦБ РФ (последняя не позже as_of)."""
    return (
        database.db_session.query(KeyRate)
        .filter(KeyRate.effective_date <= as_of)
        .order_by(KeyRate.effective_date.desc(), KeyRate.id.desc())
        .first()
    )


def penalty_sibling_account(member_account: MemberAccount) -> MemberAccount | None:
    """
    Для обычного (не пенного) счёта находит соответствующий счёт пени —
    тот же человек, тот же гараж, тот же код вида взноса (type_code), но
    is_penalty=True. Используется, чтобы при печати ПД-4 на земельный налог
    или взнос автоматически прицепить ПД-4 на пеню, если она есть.
    """
    if member_account.fee_type.is_penalty or not member_account.fee_type.type_code:
        return None
    return (
        database.db_session.query(MemberAccount)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(
            MemberAccount.person_id == member_account.person_id,
            MemberAccount.garage_id == member_account.garage_id,
            FeeType.type_code == member_account.fee_type.type_code,
            FeeType.is_penalty.is_(True),
        )
        .first()
    )


def get_primary_bank_account():
    """Основной расчётный счёт кооператива (для реквизитов получателя на ПД-4)."""
    from .models import BankAccount
    return (
        database.db_session.query(BankAccount)
        .order_by(BankAccount.is_primary.desc(), BankAccount.id)
        .first()
    )


def pd4_qr_payload(coop: Cooperative, bank_account, member_account: MemberAccount, amount: Decimal) -> str:
    """
    Строка для QR-кода платёжки по стандарту ГОСТ Р 56042-2014 (тот же формат,
    что используют банковские приложения при сканировании квитанций).
    """
    payer = member_account.person
    fields = {
        "Name": coop.short_name or coop.full_name,
        "PersonalAcc": bank_account.checking_account if bank_account else "",
        "BankName": bank_account.bank_name if bank_account else "",
        "BIC": bank_account.bik if bank_account else "",
        "CorrespAcc": bank_account.correspondent_account if bank_account else "",
        "PayeeINN": coop.inn,
        "KPP": coop.kpp,
        "Purpose": f"{member_account.fee_type.name}, гараж №{member_account.garage.number}, л/с {member_account.account_number}",
        "Sum": str(int((amount * 100).to_integral_value())),
        "LastName": payer.full_name,
        "PersAcc": member_account.account_number,
    }
    return "ST00012|" + "|".join(f"{k}={v}" for k, v in fields.items() if v)


def pd4_qr_payload_electricity(coop: Cooperative, bank_account, garage: Garage, personal_account, amount: Decimal) -> str:
    """
    То же самое, но для лицевого счёта на электричество (PersonalAccount) — он общий
    на гараж, без привязки к конкретному собственнику, поэтому в поле «ФИО плательщика»
    подставляются все текущие собственники гаража через запятую.
    """
    owners = ", ".join(o.person.full_name for o in garage.ownerships)
    payer_name = owners or f"Гараж №{garage.number}"
    electricity_fee_type = database.db_session.query(FeeType).filter_by(code="electricity").first()
    purpose_label = electricity_fee_type.name if electricity_fee_type else "Электроэнергия"
    fields = {
        "Name": coop.short_name or coop.full_name,
        "PersonalAcc": bank_account.checking_account if bank_account else "",
        "BankName": bank_account.bank_name if bank_account else "",
        "BIC": bank_account.bik if bank_account else "",
        "CorrespAcc": bank_account.correspondent_account if bank_account else "",
        "PayeeINN": coop.inn,
        "KPP": coop.kpp,
        "Purpose": f"{purpose_label}, гараж №{garage.number}, л/с {personal_account.account_number}",
        "Sum": str(int((amount * 100).to_integral_value())),
        "LastName": payer_name,
        "PersAcc": personal_account.account_number,
    }
    return "ST00012|" + "|".join(f"{k}={v}" for k, v in fields.items() if v)
