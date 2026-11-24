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
            contacts=[("petrov", "съёмщик", True)],  # (ключ, отношение, is_tenant[, ключ собственника])
            # 4-й элемент — чей это контакт (жена, дочь… собственника): если тот выбыл,
            # контакт снимается с гаража и уходит в архив вместе с ним
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
    ("tg", date, amount, note)               целевой сбор (трактор, кабель, фонари…); после загрузки
                                             гаража переносится на целевой счёт собственника на дату
                                             (move_target_charges) — если у вида «Целевой взнос» есть код
    ("pay", date, g, m, note)                платёж: g → ЭЭ и целевые (общий «банк», как в тетради),
                                             m → членский счёт
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
import re
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database, audit  # noqa: E402
from app.models import (  # noqa: E402
    Person, Phone, Garage, GarageOwnership, GarageOwnershipEvent, GarageOwnershipEventType, GarageContact,
    PersonalAccount, MemberAccount, FeeType, Charge, ChargeAllocation, Payment, ElectricityMeter, ElectricityReading,
    ElectricityTariff, ElectricityTariffKind, User,
)
from app.accounting import (  # noqa: E402
    balance, electricity_account_number, reallocate_garage_charges, reallocate_member_charges,
    ensure_personal_member_accounts,
)
from app.garages import _ensure_member_accounts  # noqa: E402
from app.models import short_name_of  # noqa: E402

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


def move_target_charges(s, garage, ft_tg, tg_owner, member_accounts):
    """
    Целевые сборы, начисленные по ходу загрузки на счёт гаража (как в тетради:
    ЭЭ и целевые — одной суммой, общий «банк»), переносятся на целевой счёт
    того, кто владел гаражом на дату начисления, вместе с оплаченными частями
    платежей (по FIFO-разнесению счёта гаража). Так «банк» тратится как в
    тетради, а итог гараж + целевые не меняется. Если у вида «Целевой взнос»
    в БД нет кода счёта — начисления остаются на счёте гаража.
    """
    tbal = defaultdict(Decimal)
    if not ft_tg.type_code:
        return tbal
    tcharges = sorted((c for c in garage.charges if c.fee_type_id == ft_tg.id), key=lambda c: c.id)
    cut = defaultdict(Decimal)
    for charge, (_date, owner) in zip(tcharges, tg_owner):
        acc = s.query(MemberAccount).filter_by(person_id=owner.id, garage_id=garage.id, fee_type_id=ft_tg.id).one()
        member_accounts[acc.id] = acc
        for al in s.query(ChargeAllocation).filter_by(charge_id=charge.id).all():
            payment = s.get(Payment, al.payment_id)
            cut[payment.id] += al.amount
            s.add(Payment(account_id=acc.id, date=payment.date, amount=al.amount,
                          comment=f"Л/С {acc.account_number}: {payment.comment or NOTEBOOK_PREFIX}"))
            tbal[acc.id] += al.amount
        charge.garage_id, charge.account_id = None, acc.id
        tbal[acc.id] -= charge.amount
    s.flush()
    for payment_id, amount in cut.items():
        payment = s.get(Payment, payment_id)
        s.query(ChargeAllocation).filter_by(payment_id=payment_id).delete()
        if payment.amount <= amount:
            s.delete(payment)
        else:
            payment.amount -= amount
    s.flush()
    s.expire(garage, ["charges", "payments"])
    reallocate_garage_charges(garage)
    return tbal


def regroup_target_payments(s, garage, ft_tg):
    """
    После move_target_charges целевой счёт получает по куску от каждой
    оплаты, которой FIFO счёта гаража закрыло целевое начисление, — с датой
    той оплаты (взнос за фонари 2021 г. — кусками из оплат 2019–2020).
    В тетради же целевой сбор оплачивается в строке, где он записан.
    Перекладывает: на целевой счёт — одна оплата в дату начисления, сколько
    начислено (но не больше суммы ЭЭ+целевые этой даты), остаток той же
    оплаты — на счёт гаража; «банк», которым целевой был оплачен раньше,
    остаётся на прежней дате. Сумма каждой оплаты тетради и итог гараж +
    целевые не меняются. Возвращает список изменений.
    """
    Z = Decimal("0")
    taccs = s.query(MemberAccount).filter_by(garage_id=garage.id, fee_type_id=ft_tg.id).all()
    tpays = [p for a in taccs for p in a.payments if NOTEBOOK_PREFIX in (p.comment or "")]
    if not tpays:
        return []
    el = defaultdict(list)
    for p in garage.payments:
        if (p.comment or "").startswith(NOTEBOOK_PREFIX):
            el[p.date].append(p)
    cur, need = defaultdict(Decimal), defaultdict(Decimal)
    for p in tpays:
        cur[(p.account_id, p.date)] += p.amount
    for a in taccs:
        for c in a.charges:
            m = re.search(r"по тетради, (\d\d\.\d\d\.\d{4})", c.comment or "")
            if m:
                need[(a.id, dt.datetime.strptime(m.group(1), "%d.%m.%Y").date())] += c.amount
    final = defaultdict(Decimal)
    for key, amt in need.items():
        final[key] = min(amt, cur.get(key, Z) + sum((p.amount for p in el.get(key[1], [])), Z))
    left = sum(cur.values()) - sum(final.values())
    for key in sorted(final, key=lambda k: k[1], reverse=True):   # FIFO не дотянулся — урезаем поздние даты
        if left >= 0:
            break
        cut = min(final[key], -left)
        final[key] -= cut
        left += cut
    for key in sorted(cur, key=lambda k: k[1]):                    # ранний «банк» — остаётся где был
        if left <= 0:
            break
        take = min(max(cur[key] - final.get(key, Z), Z), left)
        final[key] += take
        left -= take
    changes = []
    for key in sorted(set(cur) | set(final), key=lambda k: k[1]):
        aid, d = key
        delta = final.get(key, Z) - cur.get(key, Z)   # > 0 — забрать с оплаты ЭЭ той же даты
        ps = [p for p in tpays if (p.account_id, p.date) == key]
        if delta == 0 and len(ps) <= 1:
            continue
        if delta > 0:
            for p in sorted(el[d], key=lambda p: -p.amount):
                t = min(p.amount, delta)
                p.amount -= t
                delta -= t
            assert delta == 0, (garage.number, d)
        elif delta < 0:
            if el.get(d):
                el[d][0].amount -= delta
            else:
                src = ps[0].comment.split(": ", 1)[1] if ps[0].comment.startswith("Л/С") else ps[0].comment
                np_ = Payment(garage_id=garage.id, date=d, amount=-delta, comment=src)
                s.add(np_)
                el[d] = [np_]
        comment = ps[0].comment if ps else f"Л/С {s.get(MemberAccount, aid).account_number}: {el[d][0].comment}"
        for p in ps:
            s.delete(p)
        if final.get(key, Z) > 0:
            s.add(Payment(account_id=aid, date=d, amount=final[key], comment=comment))
        changes.append(f"{d:%d.%m.%Y}: {cur.get(key, Z)} → {final.get(key, Z)} ₽ ({len(ps)} шт.)")
    for ps in el.values():
        for p in ps:
            if p.amount == 0:
                s.delete(p)
    s.flush()
    s.expire_all()
    reallocate_garage_charges(s.get(Garage, garage.id))
    for a in taccs:
        reallocate_member_charges(s.get(MemberAccount, a.id))
    s.flush()
    return changes


def _rub(x: Decimal) -> str:
    text = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return (text[:-3] if text.endswith(",00") else text) + " ₽"


NOTEBOOK_PREFIX = "По тетради"
MOVED_SUFFIX = " (целевой, перенесено со счёта гаража)"


def rewrite_payment_comments(s, garage, ft_by_id):
    """
    Одна оплата из тетради в системе разнесена на несколько платежей (счёт
    гаража — ЭЭ, членский, целевой), а в комментарии каждого был итог строки
    тетради («По тетради: 1006 р» у части в 1000 ₽). Переписывает комментарии
    импортированных платежей гаража: «По тетради, оплата 25.04.2026 — всего
    1 006 ₽: 1 000 ₽ взносы, 6 ₽ электроэнергия (в тетради: …)». Части одной
    оплаты — одна дата и одна заметка. Зачёты (фонари) не трогает.
    """
    accounts = s.query(MemberAccount).filter_by(garage_id=garage.id).all()
    rows = [(p, "электроэнергия", None) for p in garage.payments]
    for acc in accounts:
        label = "целевые" if ft_by_id[acc.fee_type_id].code == "target" else "взносы"
        rows += [(p, label, acc) for p in acc.payments]
    groups = defaultdict(list)
    for p, label, acc in rows:
        text = p.comment or ""
        if acc is not None and text.startswith(f"Л/С {acc.account_number}: "):
            text = text[len(f"Л/С {acc.account_number}: "):]
        if not text.startswith(NOTEBOOK_PREFIX):
            continue
        if text.startswith(f"{NOTEBOOK_PREFIX}, оплата"):   # уже переписан — пересобрать (суммы частей могли измениться)
            m = re.search(r" \(в тетради: (.*)\)$", text)
            note = m.group(1) if m else ""
        else:
            note = text.removesuffix(MOVED_SUFFIX)[len(NOTEBOOK_PREFIX):].lstrip(":").strip()
        if note.startswith("Зачёт"):
            continue
        groups[(p.date, note)].append((p, label, acc))
    order = {"взносы": 0, "электроэнергия": 1, "целевые": 2}
    changed = 0
    for (date, note), parts in groups.items():
        by_label = defaultdict(Decimal)
        for p, label, _acc in parts:
            by_label[label] += p.amount
        total = sum(by_label.values(), Decimal("0"))
        text = f"{NOTEBOOK_PREFIX}, оплата {date:%d.%m.%Y}"
        if len(by_label) > 1:
            text += f" — всего {_rub(total)}: " + ", ".join(
                f"{_rub(v)} {k}" for k, v in sorted(by_label.items(), key=lambda kv: order[kv[0]]))
        if note:
            text += f" (в тетради: {note})"
        for p, _label, acc in parts:
            p.comment = f"Л/С {acc.account_number}: {text}" if acc is not None else text
            changed += 1
    return changed


def first_record_date(events):
    """Дата первой записи о гараже в тетради (показание, установка счётчика,
    платёж) — с неё собственник со страницы считается владельцем. Год взноса
    не годится: взносы за 2014–2017 могли оплатить в 2017-м."""
    dates = [ev[1] for ev in events
             if ev[0] in ("start", "meter", "r", "pay", "close", "owner") and isinstance(ev[1], dt.date)]
    return min(dates) if dates else None


def persons_touched(persons):
    return [p.id for p in persons.values()]


def archive_former_owners(s, person_ids):
    """
    Выбывшие из собственников, у которых не осталось ни одного гаража, — в
    архив людей, как это делает приложение (persons.archive_person): причина
    «умер» или «продал гараж» с номером гаража, дата — дата выбытия.
    """
    owners_now = {o.person_id for o in s.query(GarageOwnership).all()}
    archived = []
    events = (s.query(GarageOwnershipEvent)
              .filter(GarageOwnershipEvent.event_type == GarageOwnershipEventType.REMOVED,
                      GarageOwnershipEvent.person_id.in_(person_ids))
              .order_by(GarageOwnershipEvent.created_at).all())
    for ev in events:
        person = ev.person
        if person.id in owners_now or person.is_archived:
            continue
        note = f"{ev.comment or ''} {person.comment or ''}".lower()
        reason = "умер" if "умер" in note else "продал гараж"
        person.is_archived = True
        person.archived_at = ev.created_at
        person.archived_reason = f"{reason} (гараж №{ev.garage.number}, по тетради)"
        archived.append(f"{person.full_name} — {person.archived_reason}, {ev.created_at:%d.%m.%Y}")
    s.flush()
    return archived


def archive_owner_contacts(s, owner_contacts):
    """
    Контакты выбывшего собственника (родственники, чьи телефоны записаны в
    тетради) снимаются с гаража — как в приложении при выбытии всех
    собственников (garages._drop_contacts_of_ownerless), — и уходят в архив
    вместе с ним, если они сами не собственники и не контакты действующего
    собственника. Дата — та же, что у собственника.
    """
    owned = {(o.garage.number, o.person_id) for o in s.query(GarageOwnership).all()}
    for number, contact, owner in owner_contacts:
        if (number, owner.id) not in owned:
            garage = s.query(Garage).filter_by(number=number).one()
            s.query(GarageContact).filter_by(garage_id=garage.id, person_id=contact.id).delete()
    owners_now = {pid for _, pid in owned}
    keep = {c.id for _, c, o in owner_contacts if not o.is_archived}
    archived = []
    for number, contact, owner in owner_contacts:
        if not owner.is_archived or contact.is_archived or contact.id in owners_now or contact.id in keep:
            continue
        contact.is_archived = True
        contact.archived_at = owner.archived_at
        contact.archived_reason = f"контакт прежнего собственника {owner.short_name or short_name_of(owner.full_name)} (гараж №{number}, по тетради)"
        archived.append(f"{contact.full_name} — {contact.archived_reason}, {owner.archived_at:%d.%m.%Y}")
    s.flush()
    return archived


def import_pages(pages):
    s = database.db_session
    ft = {f.code: f for f in s.query(FeeType).all()}
    ft_el, ft_mf, ft_tg = ft["electricity"], ft["membership"], ft["target"]
    ft_by_id = {f.id: f for f in ft.values()}

    if not s.query(ElectricityTariff).filter_by(kind=ElectricityTariffKind.MEMBER).count():
        for rate, eff in NOTEBOOK_TARIFFS:
            s.add(ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal(rate),
                                    effective_date=dt.date.fromisoformat(eff), comment="По тетради учёта (импорт)"))

    persons, created_persons = {}, 0
    owner_contacts = []  # (номер гаража, контакт, чей это контакт)

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
        tg_owner = []                # (дата, собственник) по каждому целевому начислению, по порядку
        meter, prev_value = None, None

        def member_account(fee_type=ft_mf):
            acc = s.query(MemberAccount).filter_by(
                person_id=owner.id, garage_id=garage.id, fee_type_id=fee_type.id, is_archived=False).one()
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
                ensure_personal_member_accounts(new.id)
            else:
                # как garages._archive_owner_accounts_and_reuse, но без переноса остатка:
                # исторический долг/переплата остаются за прежним собственником
                for old_acc in s.query(MemberAccount).filter_by(garage_id=garage.id, is_archived=False).all():
                    old_acc.is_archived = True
                    s.flush()
                    s.add(MemberAccount(person_id=new.id, garage_id=garage.id, fee_type_id=old_acc.fee_type_id,
                                        account_number=old_acc.account_number))
                ensure_personal_member_accounts(new.id)
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
                ensure_personal_member_accounts(p.id)
                s.add(GarageOwnershipEvent(garage_id=garage.id, person_id=p.id, share=Decimal(str(share)),
                                           event_type=GarageOwnershipEventType.ADDED, comment="Импорт из тетради"))
                owner = owner or p
            s.flush()
        elif spec.get("owner"):
            set_owner(spec["owner"], first_record_date(spec["events"]), "Импорт из тетради")
        for key, relation, tenant, *of in spec.get("contacts", []) + spec.get("extra_contacts", []):
            s.add(GarageContact(garage_id=garage.id, person_id=person(key).id, relation=relation, is_tenant=tenant))
            if of:
                owner_contacts.append((number, person(key), person(of[0])))

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
                tg_owner.append((date, owner))
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
        s.flush()
        tbal = move_target_charges(s, garage, ft_tg, tg_owner, member_accounts)
        for acc in member_accounts.values():
            reallocate_member_charges(acc)
        s.flush()
        regroup_target_payments(s, garage, ft_tg)
        garage = s.get(Garage, garage.id)
        gbal = balance(garage)
        tbal = {a: balance(s.get(MemberAccount, a)) for a in tbal}
        s.expire(garage, ["payments"])
        rewrite_payment_comments(s, garage, ft_by_id)
        balances = {f"взносы {member_accounts[a].person.full_name}": b for a, b in mbal.items() if b}
        balances.update({f"целевые {member_accounts[a].person.full_name}": b for a, b in tbal.items() if b})
        report.append((number, owner.full_name if owner else "—", gbal, balances, flags))

    archived = archive_former_owners(s, set(persons_touched(persons)))
    archived += archive_owner_contacts(s, owner_contacts)
    return report, totals, created_persons, skipped, archived


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__.split("Формат")[0])
    dry = "--dry-run" in sys.argv
    pages = load_pages(args[0])

    app = create_app()
    with app.test_request_context():
        from flask import g
        report, totals, created_persons, skipped, archived = import_pages(pages)
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
        print(f"{number:>5} | {owner:<36} | гараж (ЭЭ) {gbal:+9.2f} | {mtxt}")
        for f in flags:
            print(f"        ! {f}")
    if archived:
        print("\nВ архив людей (выбывшие собственники и их контакты):")
        for line in archived:
            print(f"  {line}")
    if skipped:
        print(f"\nПропущены (уже есть в БД): {', '.join(skipped)}")
    print(f"\nГаражей: {len(report)}, новых людей: {created_persons}. Начислено: ЭЭ {totals['el']}, взносы "
          f"{totals['mf']}, целевые {totals['tg']}; платежей {totals['pay']}. "
          + ("DRY-RUN — изменения отменены." if dry else "Записано."))


if __name__ == "__main__":
    main()
