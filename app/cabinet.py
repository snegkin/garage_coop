"""
Личный кабинет: раздел, где любой залогиненный пользователь может посмотреть
свои данные и предложить изменения контактной информации. Изменения
применяются только после одобрения председателем — см. PersonDataRevision.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g
import json
import secrets
import datetime as dt

from werkzeug.security import check_password_hash, generate_password_hash

from . import database
from . import audit
from . import notifications
from . import telegram_bot
from .auth import login_required
from .permissions import is_board
from .i18n import translate as _
from .models import (
    Person, Phone, GarageOwnership, GarageContact, MemberAccount, PersonDataRevision, PersonDataRevisionStatus,
    User, NotificationChannel,
)
from .accounting import balance as account_balance
from sqlalchemy.orm import joinedload

bp = Blueprint("cabinet", __name__, url_prefix="/cabinet")


def _current_person():
    if g.user.person_id is None:
        return None
    return database.db_session.get(Person, g.user.person_id)


@bp.route("/")
@login_required
def index():
    return redirect(url_for("cabinet.profile"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    person = _current_person()
    if person is None:
        flash(_("Ваша учётная запись пока не привязана к карточке члена кооператива. Обратитесь в правление."), "warning")
        return render_template("cabinet/profile.html", person=None)

    if request.method == "POST":
        f = request.form

        # Сохраняем текущие (одобренные) данные для сравнения
        current = {
            "email": person.email,
            "telegram": person.telegram,
            "vk": person.vk,
            "max_messenger": person.max_messenger,
            "registration_address": person.registration_address,
            "residence_address": person.residence_address,
            "phones": sorted([p.number for p in person.phones]),
            "passport_series": person.passport_series,
            "passport_number": person.passport_number,
            "passport_issue_date": person.passport_issue_date.isoformat() if person.passport_issue_date else None,
            "membership_start_date": person.membership_start_date.isoformat() if person.membership_start_date else None,
        }
        new_data = {
            "email": f.get("email") or None,
            "telegram": f.get("telegram") or None,
            "vk": f.get("vk") or None,
            "max_messenger": f.get("max") or None,
            "registration_address": f.get("registration_address") or None,
            "residence_address": f.get("residence_address") or None,
            "phones": sorted([p.strip() for p in f.get("phones", "").split(",") if p.strip()]),
            "passport_series": f.get("passport_series") or None,
            "passport_number": f.get("passport_number") or None,
            "passport_issue_date": f.get("passport_issue_date") or None,
            "membership_start_date": f.get("membership_start_date") or None,
        }
        # Если ничего не изменилось — предупреждаем
        if current == new_data:
            flash(_("Нет изменений для отправки."), "info")
            return redirect(url_for("cabinet.profile"))
        # Создаём ревизию — данные не применяются сразу
        revision = PersonDataRevision(
            person_id=person.id,
            submitted_by_user_id=g.user.id,
            fields_snapshot=json.dumps(new_data, ensure_ascii=False),
            previous_snapshot=json.dumps(current, ensure_ascii=False),
            status=PersonDataRevisionStatus.PENDING,
        )
        database.db_session.add(revision)
        database.db_session.commit()
        flash(_("Изменения отправлены на рассмотрение председателю."), "success")
        return redirect(url_for("cabinet.profile"))

    # Для GET: определяем, есть ли pending-ревизии у этого человека
    pending_revision = (
        database.db_session.query(PersonDataRevision)
        .filter_by(person_id=person.id, status=PersonDataRevisionStatus.PENDING)
        .order_by(PersonDataRevision.submitted_at.desc())
        .first()
    )
    snap = None
    if pending_revision:
        try:
            snap = json.loads(pending_revision.fields_snapshot)
        except Exception:
            snap = None

    # Если есть pending — подставляем данные из ревизии в форму
    display_person = person
    if snap:
        issue_date_str = snap.get("passport_issue_date")
        issue_date = None
        if issue_date_str:
            try:
                issue_date = dt.date.fromisoformat(issue_date_str)
            except (ValueError, TypeError):
                pass
        membership_start_str = snap.get("membership_start_date")
        membership_start = None
        if membership_start_str:
            try:
                membership_start = dt.date.fromisoformat(membership_start_str)
            except (ValueError, TypeError):
                pass
        display_person = type('Person', (), {
            'id': person.id,
            'full_name': person.full_name,
            'email': snap.get('email'),
            'telegram': snap.get('telegram'),
            'vk': snap.get('vk'),
            'max_messenger': snap.get('max_messenger'),
            'registration_address': snap.get('registration_address'),
            'residence_address': snap.get('residence_address'),
            'phones': [Phone(id=-1, number=n) for n in snap.get('phones', [])],
            'passport_series': snap.get('passport_series'),
            'passport_number': snap.get('passport_number'),
            'passport_issue_date': issue_date,
            'membership_start_date': membership_start,
            'membership_end_date': person.membership_end_date,
            'comment': person.comment,
            'telegram_chat_id': person.telegram_chat_id,
        })()

    telegram_link_url = None
    if person.telegram_link_token:
        settings = telegram_bot.get_settings()
        if settings and settings.bot_username:
            telegram_link_url = f"https://t.me/{settings.bot_username}?start={person.telegram_link_token}"

    return render_template(
        "cabinet/profile.html", person=display_person, pending_revision=pending_revision, pending_data=snap,
        telegram_link_url=telegram_link_url,
    )


@bp.route("/profile/notifications", methods=["POST"])
@login_required
def notification_settings():
    """Подписка на уведомления о событиях сайта (app/notifications.py) —
    отдельная форма и отдельный роут от profile(): это личная настройка,
    сохраняется сразу, а не через PersonDataRevision, куда уходят
    контактные/паспортные данные и ждут одобрения председателя. Если
    держать это той же формой/кнопкой, что и «Отправить на рассмотрение»,
    выглядело бы так, будто и подписка на уведомления тоже ждёт одобрения
    — это не так."""
    f = request.form
    notify_channel_raw = f.get("notify_channel") or None
    try:
        notify_channel = NotificationChannel(notify_channel_raw) if notify_channel_raw else None
    except ValueError:
        notify_channel = None
    notify_events = {
        "notify_charge": bool(f.get("notify_charge")),
        "notify_payment": bool(f.get("notify_payment")),
        "notify_news": bool(f.get("notify_news")),
        "notify_forum": bool(f.get("notify_forum")),
        "notify_board_chat": bool(f.get("notify_board_chat")) and is_board(),
    }
    if notify_channel is not None and any(notify_events.values()) and not notifications.channel_is_ready(g.user, notify_channel):
        flash(_("Чтобы получать уведомления этим способом, сначала укажите и сохраните соответствующий контакт в профиле."), "danger")
    else:
        g.user.notify_channel = notify_channel
        for field, value in notify_events.items():
            setattr(g.user, field, value)
        database.db_session.commit()
        flash(_("Настройки уведомлений сохранены."), "success")
    return redirect(url_for("cabinet.profile"))


@bp.route("/profile/telegram/link", methods=["POST"])
@login_required
def telegram_link_start():
    """Генерирует одноразовый токен привязки Telegram (см. app/telegram_bot.py
    докстринг — сама привязка завершается scripts/poll_telegram.py, когда
    бот получит /start с этим токеном)."""
    person = _current_person()
    if person is None:
        return redirect(url_for("cabinet.profile"))
    person.telegram_link_token = secrets.token_urlsafe(24)
    database.db_session.commit()
    return redirect(url_for("cabinet.profile"))


@bp.route("/profile/telegram/unlink", methods=["POST"])
@login_required
def telegram_unlink():
    person = _current_person()
    if person is None:
        return redirect(url_for("cabinet.profile"))
    person.telegram_chat_id = None
    person.telegram_link_token = None
    database.db_session.commit()
    flash(_("Telegram отвязан."), "success")
    return redirect(url_for("cabinet.profile"))


@bp.route("/change-password", methods=["POST"])
@login_required
def change_password():
    f = request.form
    current_password = f.get("current_password", "")
    new_password = f.get("new_password", "")
    confirm_password = f.get("confirm_password", "")

    if not check_password_hash(g.user.password_hash, current_password):
        flash(_("Текущий пароль указан неверно."), "danger")
        return redirect(url_for("cabinet.profile"))
    if len(new_password) < 4:
        flash(_("Новый пароль слишком короткий (минимум 4 символа)."), "danger")
        return redirect(url_for("cabinet.profile"))
    if new_password != confirm_password:
        flash(_("Новый пароль и подтверждение не совпадают."), "danger")
        return redirect(url_for("cabinet.profile"))

    g.user.password_hash = generate_password_hash(new_password)
    audit.record(
        "account.password_change", entity_type="user", entity_id=g.user.id,
        summary=f"Пользователь «{g.user.username}» сменил свой пароль",
    )
    database.db_session.commit()
    flash(_("Пароль изменён."), "success")
    return redirect(url_for("cabinet.profile"))


@bp.route("/garages")
@login_required
def garages():
    person = _current_person()
    ownerships = []
    contact_garages = []
    member_accounts_by_garage = {}
    electricity_by_garage = {}
    if person is not None:
        ownerships = (
            database.db_session.query(GarageOwnership)
            .filter_by(person_id=person.id)
            .all()
        )
        owned_garage_ids = {o.garage_id for o in ownerships}

        # Гаражи, где человек указан лицом для связи (GarageContact — может
        # не быть собственником, напр. супруга/доверенное лицо), но своих
        # гаражей у него при этом может и не быть вовсе — то же право
        # смотреть/вести гараж, что и у собственника (см.
        # permissions.is_owner_or_board), просто карточка помечена, чей
        # это гараж, а не выдаётся за собственный.
        contacts = (
            database.db_session.query(GarageContact)
            .filter_by(person_id=person.id)
            .all()
        )
        seen_contact_garage_ids = set()
        for c in contacts:
            if c.garage_id in owned_garage_ids or c.garage_id in seen_contact_garage_ids:
                continue
            seen_contact_garage_ids.add(c.garage_id)
            contact_garages.append(c.garage)

        accounts = (
            database.db_session.query(MemberAccount)
            .filter_by(person_id=person.id)
            .options(joinedload(MemberAccount.charges))
            .all()
        )
        for acc in accounts:
            if acc.fee_type.is_penalty and not acc.charges:
                continue
            member_accounts_by_garage.setdefault(acc.garage_id, []).append(acc)
        for garage in [o.garage for o in ownerships] + contact_garages:
            if garage.account is not None:
                electricity_by_garage[garage.id] = (garage.account, account_balance(garage))
    return render_template(
        "cabinet/garages.html", person=person, ownerships=ownerships, contact_garages=contact_garages,
        member_accounts_by_garage=member_accounts_by_garage,
        electricity_by_garage=electricity_by_garage,
    )
