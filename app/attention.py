"""
«Требует внимания» — счётчики для колокольчика в шапке (base.html,
_attention_menu.html), блока на дашборде и JSON-опроса /attention
(main.attention — обновление без перезагрузки, счётчик во вкладке и на
иконке приложения). Один источник: раньше считалось прямо в контекст-
процессоре (app/__init__.py: _inject_user), который по-прежнему отдаёт
отсюда прежние pending_*_count — их читает dashboard.html.

Только дешёвые SELECT: опрос идёт раз в минуту с каждой открытой вкладки.
Почта — кэш непрочитанного (MailboxSettings.unread_count /
User.pop3_mailbox_unread_count), не поход на IMAP/POP3.

Чаты правления/ревкомиссии сюда не входят — у них свой плавающий виджет
с бейджем (base.html, board_chat_unread_count).
"""
import datetime as dt

from flask import url_for

from . import database
from .i18n import translate as _
from .models import (
    PersonDataRevision, PersonDataRevisionStatus, Vote, VoteQuestion, VoteBallot, VoteStatus, RoleEnum,
    VoteProposal, VoteProposalBoardBallot, ProposalStatus, MailboxSettings, MailProtocol,
    Application, ApplicationStatus, CertificateRequest, CertificateRequestStatus,
)
from .permissions import is_board


def counts(user) -> dict[str, int]:
    """Счётчики по видам; ключи есть всегда (0, если пользователю вид не
    положен) — контекст-процессор отдаёт их шаблонам как pending_*_count."""
    result = {"pd": 0, "votes": 0, "proposals": 0, "certificates": 0, "applications": 0, "mail": 0}
    if not user:
        return result
    session = database.db_session

    # председателю — ожидающие одобрения изменения ПД
    if user.role == RoleEnum.CHAIRMAN:
        result["pd"] = session.query(PersonDataRevision).filter(
            PersonDataRevision.status == PersonDataRevisionStatus.PENDING
        ).count()

    # всем — открытые голосования, где у пользователя нет ни одного бюллетеня
    if user.person_id:
        result["votes"] = session.query(Vote).filter(
            Vote.status == VoteStatus.OPEN,
            Vote.closes_at > dt.datetime.utcnow(),
            ~session.query(VoteQuestion.id)
            .join(VoteBallot)
            .filter(
                VoteQuestion.vote_id == Vote.id,
                VoteBallot.person_id == user.person_id,
            ).exists(),
        ).count()

    # члену правления — предложения членов кооператива, ждущие его голоса
    if user.person_id:
        from .governance import current_board_member_ids
        if user.person_id in current_board_member_ids():
            result["proposals"] = session.query(VoteProposal).filter(
                VoteProposal.status == ProposalStatus.PENDING,
                ~session.query(VoteProposalBoardBallot.id).filter(
                    VoteProposalBoardBallot.proposal_id == VoteProposal.id,
                    VoteProposalBoardBallot.person_id == user.person_id,
                ).exists(),
            ).count()

    # председателю — запросы справок о владении гаражом
    if user.role == RoleEnum.CHAIRMAN:
        result["certificates"] = session.query(CertificateRequest).filter(
            CertificateRequest.status == CertificateRequestStatus.REQUESTED,
        ).count()

    if is_board():
        # заявления членов кооператива без решения (app/applications.py) —
        # ответить может любой член правления, поэтому счёт общий
        result["applications"] = session.query(Application).filter(
            Application.status == ApplicationStatus.SUBMITTED,
        ).count()

        # Непрочитанные во «Входящих» почты правления — кэш, который
        # обновляют scripts/poll_mailbox.py и mailbox (см.
        # MailboxSettings.unread_count). IMAP — общий счётчик (серверные
        # флаги одни на всех); POP3 — свой у каждого (флагов протокол не
        # хранит, см. MailboxPop3MessageState).
        settings = session.query(MailboxSettings).first()
        if settings:
            if settings.incoming_protocol == MailProtocol.POP3:
                result["mail"] = user.pop3_mailbox_unread_count or 0
            else:
                result["mail"] = settings.unread_count or 0
    return result


def items(user, by_key: dict[str, int] | None = None) -> list[dict]:
    """Пункты списка под колокольчиком — только с ненулевым счётчиком,
    кроме почты: её пункт у правления есть всегда (скрыт при нуле) —
    страница почты показывает/прячет его сама при смене статуса письма
    (mailbox/inbox.html: [data-mailbox-unread-item])."""
    if not user:
        return []
    c = by_key if by_key is not None else counts(user)
    candidates = [
        ("pd", "exclamation-triangle", _("Ожидает одобрения"), url_for("persons.list_persons")),
        ("votes", "check2-square", _("Не проголосовано"), url_for("voting.list_votes")),
        ("proposals", "lightbulb", _("Предложения ждут решения"), url_for("proposals.list_proposals")),
        ("certificates", "file-earmark-check", _("Запрошены справки"), url_for("certificates.list_certificates")),
        ("applications", "file-earmark-text", _("Заявления ждут решения"),
         url_for("applications.list_applications", status="submitted")),
    ]
    result = [
        {"key": key, "icon": icon, "label": label, "url": url, "count": c[key]}
        for key, icon, label, url in candidates if c[key]
    ]
    if is_board():
        result.append({"key": "mail", "icon": "envelope", "label": _("Непрочитанные письма"),
                       "url": url_for("mailbox.inbox"), "count": c["mail"]})
    return result


def total(by_key: dict[str, int]) -> int:
    return sum(by_key.values())
