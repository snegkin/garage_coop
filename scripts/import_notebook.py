#!/usr/bin/env python3
"""
Импорт рукописной тетради учёта гаражей (расшифровка фото страниц) в БД
кооператива: гаражи, люди, собственники и их смена, лица для связи/съёмщики,
счётчики и показания, начисления (ЭЭ, членские взносы, целевые сборы) и
платежи — с разнесением по лицевым счетам так же, как это делают формы
приложения (garages.add_reading/add_payment, finance.add_member_*).

Запуск:
    INSTANCE_DIR=/path/instance venv/bin/python scripts/import_notebook.py PAGES.py [--dry-run]

PAGES.py — файл расшифровки (в нём персональные данные — держать рядом с
БД кооператива, в INSTANCE_DIR, НЕ в репозитории). Дозагрузка: гаражи,
которые уже есть в БД, пропускаются (о них — строка в отчёте), люди ищутся
по ФИО без учёта регистра и переиспользуются. --dry-run — всё посчитать,
показать отчёт и откатить.

Формат PAGES.py (обычный Python):

    import datetime as dt
    D = lambda s: dt.datetime.strptime(s, "%d.%m.%Y").date()

    PERSONS = {                       # ключ → (ФИО, [телефоны], адрес|None, комментарий|None)
        "ivanov": ("Иванов Иван Иванович", ["+79001234567"], None, None),
    }
    G = {                             # номер гаража → описание страницы
        "173": dict(
            owner="ivanov",                       # ключ PERSONS или None (страница без ФИО)
            # или owners=[("ivanov", "0.5"), ("petrov", "0.5")] — совладельцы с долями
            # (пока только для страниц без взносов и смены собственника)
            contacts=[("petrov", "съёмщик", True)],  # (ключ, отношение, is_tenant)
            comment="произвольная заметка к гаражу",
            events=[ ... ],                       # строго в хронологическом порядке
        ),
    }

События:
    ("start", date|None, value, note)        счётчик: начальное показание (установка)
    ("meter", date|None, value, note)        замена счётчика: новый прибор, нач. показание
    ("r", date, value, tariff, amount, note) показание; amount=None → (value − пред.) × tariff,
                                             число → сумма как в тетради, 0 → показание без начисления
    ("el", date, year, amount, note)         начисление за ЭЭ без показания
    ("mf", date, year, amount, note)         членский взнос за год (счёт текущего собственника)
    ("tg", date, amount, note)               целевой сбор (трактор, кабель, фонари…) на счёт гаража
    ("pay", date, g, m, note)                платёж: g → счёт гаража (ЭЭ+целевые), m → членский счёт
    ("close", date, note)                    «оплачено» (черта/подпись) — погасить долг на обоих счетах
    ("owner", date, person_key, note)        смена собственника (прежние счета архивируются,
                                             номера переходят новому; долги остаются за прежним)
    ("flag", text)                           допущение/неясность — попадает в отчёт

Правила расшифровки (договорённость с правлением): заносится только то, что
записано в тетради; строка с чертой/подписью/«уплочено» — оплачено; «долг» —
нет; «N р — банк» — переплата, остаётся на счёте.
"""
import datetime as dt
import importlib.util
import os
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database, audit  # noqa: E402
from app.models import (  # noqa: E402
    Person, Phone, Garage, GarageOwnership, GarageOwnershipEvent, GarageOwnershipEventType, GarageContact,
    PersonalAccount, MemberAccount, FeeType, Charge, Payment, ElectricityMeter, ElectricityReading,
    ElectricityTariff, ElectricityTariffKind, User,
)
from app.accounting import electricity_account_number, reallocate_garage_charges, reallocate_member_charges  # noqa: E402
from app.garages import _ensure_member_accounts  # noqa: E402

DEFAULT_AREA = Decimal("24")  # площади в тетради нет — заглушка, поправить в карточке гаража
Q = Decimal("0.01")
# Тарифы для членов по тетради — заводятся, только если тарифов ещё нет вовсе.
NOTEBOOK_TARIFFS = [("4.2", "2017-01-01"), ("5", "2021-09-01"), ("5.5", "2025-01-01"), ("6", "2025-07-01")]


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Q, ROUND_HALF_UP)


def load_pages(path):
    spec = importlib.util.spec_from_file_location("notebook_pages", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_pages(pages):
    s = database.db_session
    ft = {f.code: f for f in s.query(FeeType).all()}
    ft_el, ft_mf, ft_tg = ft["electricity"], ft["membership"], ft["target"]

    if not s.query(ElectricityTariff).filter_by(kind=ElectricityTariffKind.MEMBER).count():
        for rate, eff in NOTEBOOK_TARIFFS:
            s.add(ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal(rate),
                                    effective_date=dt.date.fromisoformat(eff), comment="По тетради учёта (импорт)"))

    persons, created_persons = {}, 0

    def person(key):
        nonlocal created_persons
        if key not in persons:
            name, phones, addr, comment = pages.PERSONS[key]
            p = s.query(Person).filter(Person.full_name.ilike(name)).first()
            if p is None:
                p = Person(full_name=name, residence_address=addr, comment=comment)
                s.add(p)
                s.flush()
                for ph in phones:
                    s.add(Phone(person_id=p.id, number=ph))
                created_persons += 1
            persons[key] = p
        return persons[key]

    report, totals, skipped = [], defaultdict(Decimal), []
    existing = {g.number for g in s.query(Garage).all()}

    for number in sorted(pages.G, key=lambda n: (len(n), n)):
        if number in existing:
            skipped.append(number)
            continue
        spec = pages.G[number]
        garage = Garage(number=number, area_sqm=DEFAULT_AREA, coefficient=Decimal("1"), comment=spec.get("comment"))
        s.add(garage)
        s.flush()
        s.add(PersonalAccount(garage_id=garage.id, account_number=electricity_account_number(garage.id)))

        owner = None
        gbal = Decimal("0")          # paid − charged по счёту гаража
        mbal = defaultdict(Decimal)  # id членского счёта → paid − charged
        member_accounts = {}
        meter, prev_value = None, None

        def member_account():
            acc = s.query(MemberAccount).filter_by(
                person_id=owner.id, garage_id=garage.id, fee_type_id=ft_mf.id, is_archived=False).one()
            member_accounts[acc.id] = acc
            return acc

        def set_owner(key, date, comment):
            nonlocal owner
            new = person(key)
            when = dt.datetime.combine(date, dt.time(12)) if date else dt.datetime.utcnow()
            if owner is not None:
                s.delete(s.query(GarageOwnership).filter_by(garage_id=garage.id, person_id=owner.id).one())
                s.add(GarageOwnershipEvent(garage_id=garage.id, person_id=owner.id, created_at=when,
                                           event_type=GarageOwnershipEventType.REMOVED, comment=comment))
                s.flush()
            s.add(GarageOwnership(garage_id=garage.id, person_id=new.id, share=Decimal("1")))
            s.flush()
            if owner is None:
                _ensure_member_accounts(garage, new.id, 0)
            else:
                # как garages._archive_owner_accounts_and_reuse, но без переноса остатка:
                # исторический долг/переплата остаются за прежним собственником
                for old_acc in s.query(MemberAccount).filter_by(garage_id=garage.id, is_archived=False).all():
                    old_acc.is_archived = True
                    s.flush()
                    s.add(MemberAccount(person_id=new.id, garage_id=garage.id, fee_type_id=old_acc.fee_type_id,
                                        account_number=old_acc.account_number))
            s.add(GarageOwnershipEvent(garage_id=garage.id, person_id=new.id, share=Decimal("1"), created_at=when,
                                       event_type=GarageOwnershipEventType.ADDED,
                                       comment=comment or "Импорт из тетради"))
            s.flush()
            owner = new

        if spec.get("owners"):
            # совладельцы с долями — только для страниц без событий по членским счетам
            assert not any(ev[0] in ("mf", "owner") or (ev[0] == "pay" and ev[3]) for ev in spec["events"]), \
                f"Гараж {number}: совладельцы поддерживаются только без взносов/смены собственника"
            for index, (key, share) in enumerate(spec["owners"]):
                p = person(key)
                s.add(GarageOwnership(garage_id=garage.id, person_id=p.id, share=Decimal(str(share))))
                s.flush()
                _ensure_member_accounts(garage, p.id, index)
                s.add(GarageOwnershipEvent(garage_id=garage.id, person_id=p.id, share=Decimal(str(share)),
                                           event_type=GarageOwnershipEventType.ADDED, comment="Импорт из тетради"))
                owner = owner or p
            s.flush()
        elif spec.get("owner"):
            set_owner(spec["owner"], None, "Импорт из тетради")
        for key, relation, tenant in spec.get("contacts", []) + spec.get("extra_contacts", []):
            s.add(GarageContact(garage_id=garage.id, person_id=person(key).id, relation=relation, is_tenant=tenant))

        flags = []
        for ev in spec["events"]:
            kind = ev[0]
            if kind == "flag":
                flags.append(ev[1])
            elif kind in ("start", "meter"):
                _, date, value, note = ev
                meter = ElectricityMeter(garage_id=garage.id, meter_number="б/н", installed_date=date,
                                         initial_reading=Decimal(str(value)), comment=note)
                s.add(meter)
                s.flush()
                prev_value = Decimal(str(value))
            elif kind == "r":
                _, date, value, tariff, amount, note = ev
                value, tariff = Decimal(str(value)), Decimal(str(tariff))
                if amount is None:
                    amount = money((value - prev_value) * tariff)
                amount = money(amount) if amount else None
                reading = ElectricityReading(meter_id=meter.id, reading=value, reading_date=date,
                                             amount=amount, tariff=tariff, comment=note)
                s.add(reading)
                s.flush()
                if amount:
                    s.add(Charge(garage_id=garage.id, fee_type_id=ft_el.id, year=date.year, amount=amount,
                                 reading_id=reading.id, comment=f"Начислено по показаниям от {date.isoformat()}"))
                    gbal -= amount
                    totals["el"] += amount
                prev_value = value
            elif kind == "el":
                _, date, year, amount, note = ev
                amount = money(amount)
                s.add(Charge(garage_id=garage.id, fee_type_id=ft_el.id, year=year, amount=amount, comment=note))
                gbal -= amount
                totals["el"] += amount
            elif kind == "tg":
                _, date, amount, note = ev
                amount = money(amount)
                s.add(Charge(garage_id=garage.id, fee_type_id=ft_tg.id, year=date.year, amount=amount,
                             comment=f"{note} (по тетради, {date:%d.%m.%Y})"))
                gbal -= amount
                totals["tg"] += amount
            elif kind == "mf":
                _, date, year, amount, note = ev
                amount = money(amount)
                acc = member_account()
                s.add(Charge(account_id=acc.id, fee_type_id=ft_mf.id, year=year, amount=amount,
                             comment=f"Членский взнос {year} (по тетради, {date:%d.%m.%Y})" + (f": {note}" if note else "")))
                mbal[acc.id] -= amount
                totals["mf"] += amount
            elif kind in ("pay", "close"):
                if kind == "pay":
                    _, date, g_amt, m_amt, note = ev
                    g_amt, m_amt = money(g_amt), money(m_amt)
                else:
                    _, date, note = ev
                    acc = member_account() if owner else None
                    g_amt = -gbal if gbal < 0 else Decimal("0")
                    m_amt = -mbal[acc.id] if acc is not None and mbal[acc.id] < 0 else Decimal("0")
                text = "По тетради" + (f": {note}" if note else "")
                if g_amt:
                    s.add(Payment(garage_id=garage.id, date=date, amount=g_amt,
                                  payer_person_id=owner.id if owner else None, comment=text))
                    gbal += g_amt
                    totals["pay"] += g_amt
                if m_amt:
                    acc = member_account()
                    s.add(Payment(account_id=acc.id, date=date, amount=m_amt,
                                  comment=f"Л/С {acc.account_number}: {text}"))
                    mbal[acc.id] += m_amt
                    totals["pay"] += m_amt
            elif kind == "owner":
                _, date, key, note = ev
                set_owner(key, date, note)
            else:
                raise ValueError(f"Гараж {number}: неизвестное событие {ev!r}")
            s.flush()

        reallocate_garage_charges(garage)
        for acc in member_accounts.values():
            reallocate_member_charges(acc)
        report.append((number, owner.full_name if owner else "—", gbal,
                       {member_accounts[a].person.full_name: b for a, b in mbal.items() if b}, flags))

    return report, totals, created_persons, skipped


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__.split("Формат")[0])
    dry = "--dry-run" in sys.argv
    pages = load_pages(args[0])

    app = create_app()
    with app.test_request_context():
        from flask import g
        report, totals, created_persons, skipped = import_pages(pages)
        if report:
            g.user = database.db_session.query(User).order_by(User.id).first()
            audit.record(
                "import.notebook",
                f"Импорт тетради учёта ({os.path.basename(args[0])}): гаражей {len(report)}, новых людей "
                f"{created_persons}; начислено ЭЭ {totals['el']} ₽, взносов {totals['mf']} ₽, целевых "
                f"{totals['tg']} ₽; платежей {totals['pay']} ₽",
            )
        if dry:
            database.db_session.rollback()
        else:
            database.db_session.commit()

    for number, owner, gbal, mb, flags in report:
        mtxt = "; ".join(f"{k}: {v:+.2f}" for k, v in mb.items()) or "0"
        print(f"{number:>5} | {owner:<36} | гараж {gbal:+9.2f} | взносы {mtxt}")
        for f in flags:
            print(f"        ! {f}")
    if skipped:
        print(f"\nПропущены (уже есть в БД): {', '.join(skipped)}")
    print(f"\nГаражей: {len(report)}, новых людей: {created_persons}. Начислено: ЭЭ {totals['el']}, взносы "
          f"{totals['mf']}, целевые {totals['tg']}; платежей {totals['pay']}. "
          + ("DRY-RUN — изменения отменены." if dry else "Записано."))


if __name__ == "__main__":
    main()
