"""Команды бота MAX для правления (app/max_commands.py) и выбор кооператива (app/max_router.py)."""
import datetime as dt
from decimal import Decimal

import pytest

from app import max_bot, max_router
from app.bank_api import crypto
from app.models import (
    Charge, ElectricityMeter, ElectricityReading, FieldEntry, MaxSettings, MaxUserRoute, Payment, Phone, RoleEnum,
)
from tests.conftest import make_garage, make_ownership, make_person, make_user
from tests.test_max_news import FakeMax, FakeResponse

BOARD_MAX = 7001
TODAY = dt.date.today()


def _world(db, role=RoleEnum.BOARD):
    db.add(MaxSettings(bot_token_encrypted=crypto.encrypt("max-token"), bot_username="id77_bot"))
    me = make_person(db, "Председатель Правления", max_user_id=BOARD_MAX)
    make_user(db, "boarduser", "pw", role, person=me)
    owner = make_person(db, "Сидоров Сидор")
    db.add(Phone(person_id=owner.id, number="+7 901 000-00-00"))
    garage = make_garage(db, "15")
    make_ownership(db, garage, owner)
    meter = ElectricityMeter(garage_id=garage.id, meter_number="SN-1", installed_date=TODAY - dt.timedelta(days=300))
    db.add(meter)
    db.flush()
    db.add(ElectricityReading(meter_id=meter.id, reading=Decimal("1000"), reading_date=TODAY - dt.timedelta(days=30)))
    db.add(Charge(garage_id=garage.id, year=TODAY.year, amount=Decimal("700")))
    db.commit()
    return garage, meter


class Bot:
    """Апдейты в GET /updates и что бот ответил."""

    def __init__(self, monkeypatch):
        self.fake = FakeMax(monkeypatch)
        self.batch = {"updates": []}
        self.answers = []
        original = self.fake.request

        def request(method, url, params=None, **kw):
            if url.endswith("/updates"):
                batch, self.batch = self.batch, {"updates": []}
                return FakeResponse(200, batch)
            if url.endswith("/answers"):
                self.answers.append((params["callback_id"], kw.get("json")))
                return FakeResponse(200, {"success": True})
            return original(method, url, params=params, **kw)

        monkeypatch.setattr(max_bot.requests, "request", request)

    def say(self, text, user=BOARD_MAX):
        self.batch["updates"].append({"update_type": "message_created", "message": {
            "sender": {"user_id": user}, "recipient": {"chat_id": 5, "chat_type": "dialog"}, "body": {"text": text}}})
        max_bot.poll_updates()

    def press(self, payload, user=BOARD_MAX, cid="cb1"):
        self.batch["updates"].append({"update_type": "message_callback",
                                      "callback": {"callback_id": cid, "payload": payload, "user": {"user_id": user}}})
        max_bot.poll_updates()

    def last_text(self):
        return self.fake.messages[-1]["body"]["text"]

    def last_buttons(self):
        atts = self.fake.messages[-1]["body"].get("attachments") or []
        return [b for row in atts[0]["payload"]["buttons"] for b in row] if atts else []


def test_garage_card_and_help(app, db, monkeypatch):
    _world(db)
    bot = Bot(monkeypatch)
    with app.app_context():
        bot.say("15")
        text = bot.last_text()
        assert "Гараж №15" in text and "Сидоров Сидор" in text and "+7 901 000-00-00" in text
        assert "Электричество: долг 700,00 ₽" in text and "Счётчик SN-1: 1000" in text
        bot.say("помощь")
        assert "15 1450" in bot.last_text()
        bot.say("999")
        assert "Не понял" in bot.last_text()


def test_not_board_and_not_linked(app, db, monkeypatch):
    _world(db)
    member = make_person(db, "Рядовой Член", max_user_id=8001)
    make_user(db, "member1", "pw", RoleEnum.MEMBER, person=member)
    db.commit()
    bot = Bot(monkeypatch)
    with app.app_context():
        bot.say("15", user=8001)
        assert "только для правления" in bot.last_text()
        bot.say("15", user=9999)
        assert "не привязан" in bot.last_text()
        bot.press("rd|1|2000|2026-01-01|0|SN-1", user=8001)
    assert db.query(ElectricityReading).count() == 1


def test_reading_with_confirmation(app, db, monkeypatch):
    garage, meter = _world(db)
    bot = Bot(monkeypatch)
    with app.app_context():
        bot.say("15 900")
        assert "больше прежних" in bot.last_text()
        bot.say("15 1150")
        assert "внести 1150, расход 150 кВт·ч" in bot.last_text()
        confirm = next(b for b in bot.last_buttons() if b["text"] == "Внести")
        bot.press(confirm["payload"])
    reading = db.query(ElectricityReading).order_by(ElectricityReading.id.desc()).first()
    assert reading.reading == Decimal("1150") and "через MAX" in reading.comment
    assert "Внесено" in bot.answers[-1][1]["message"]["text"]
    assert bot.answers[-1][1]["message"]["attachments"] == []  # кнопки убраны


def test_payment_by_board_waits(app, db, monkeypatch):
    garage, _meter = _world(db)
    bot = Bot(monkeypatch)
    with app.app_context():
        bot.say("15 взнос 700")
        buttons = bot.last_buttons()
        assert buttons[0]["text"].startswith("Электричество (долг 700,00 ₽)")
        bot.press(buttons[0]["payload"])
    assert db.query(Payment).count() == 0
    entry = db.query(FieldEntry).one()
    assert entry.status == "pending" and entry.receipt_number.endswith("-0001")
    assert "Квитанция №" in bot.answers[-1][1]["message"]["text"]


def test_payment_by_chairman_applied(app, db, monkeypatch):
    _world(db, role=RoleEnum.CHAIRMAN)
    bot = Bot(monkeypatch)
    with app.app_context():
        bot.say("15 оплата 700")
        bot.press(bot.last_buttons()[0]["payload"])
    payment = db.query(Payment).one()
    assert payment.amount == Decimal("700") and "через MAX" in payment.comment


def test_debtors(app, db, monkeypatch):
    from app.models import FeeType, MemberAccount, Person
    _world(db)
    fee = FeeType(code="membership", name="Членский взнос")
    db.add(fee)
    db.flush()
    owner = db.query(Person).filter_by(full_name="Сидоров Сидор").one()
    acc = MemberAccount(person_id=owner.id, fee_type_id=fee.id, account_number="15-1")
    db.add(acc)
    db.flush()
    db.add(Charge(account_id=acc.id, year=TODAY.year, amount=Decimal("700")))
    db.commit()
    bot = Bot(monkeypatch)
    with app.app_context():
        bot.say("долги")
    assert "Сидоров Сидор: 700,00 ₽" in bot.last_text()


def test_multi_coop_choice_and_tagged_callbacks(app, db, monkeypatch):
    app.config.update(MAX_ROUTE_SECRET="s", MAX_ROUTE_PEERS="a2=http://127.0.0.1:8001")
    _world(db)
    db.add(MaxUserRoute(max_user_id=BOARD_MAX, coop_code="a2"))
    db.commit()
    bot = Bot(monkeypatch)
    forwarded = []

    def post(url, json=None, headers=None, timeout=None, **kw):
        if not json["updates"]:
            return FakeResponse(200, {"linked": [], "coop_name": "Автомотолюбитель-2"})
        forwarded.extend(json["updates"])
        return FakeResponse(200, {"linked": []})

    monkeypatch.setattr(max_router.requests, "post", post)
    with app.app_context():
        bot.say("15")
        assert "С каким работаем" in bot.last_text()
        names = [b["text"] for b in bot.last_buttons()]
        assert "Автомотолюбитель-2" in names
        bot.press("coop|a2")
        assert "Автомотолюбитель-2" in bot.answers[-1][1]["message"]["text"]
        bot.say("15")
        assert forwarded[-1]["message"]["body"]["text"] == "15"  # ушло в А-2
        bot.press("a2:rd|1|2000|2026-01-01|0|SN-1", cid="cb2")
        assert forwarded[-1]["update_type"] == "message_callback"
        n = len(forwarded)
        bot.press("rd|1|1100|" + TODAY.isoformat() + "|0|SN-1", cid="cb3")  # без метки — своя кнопка
        assert len(forwarded) == n
        bot.say("кооператив")
        assert "С каким работаем" in bot.last_text()
    assert db.query(ElectricityReading).order_by(ElectricityReading.id.desc()).first().reading == Decimal("1100")
