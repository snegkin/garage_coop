"""
Синхронизация баланса ЛИЧНОГО КАБИНЕТА контрагента с внешним API (не
баланса расчётов с ним — тот считается отдельно, см.
accounting.counterparty_balance()). Тот же приём, что app/bank_sync.py для
расчётных счетов: sync_counterparty_balance() ничего не знает про
flash/redirect и вызывается и роутом (кнопка «Обновить баланс»), и
cron-скриптом scripts/sync_bank_accounts.py — вместе с банковскими
счетами, одним прогоном.
"""
import datetime as dt

from flask import Blueprint, redirect, url_for, flash

from . import database, audit
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, Counterparty
from .counterparty_api import get_client, unsupported_reason
from .counterparty_api.base import CounterpartyApiError

bp = Blueprint("counterparty_sync", __name__, url_prefix="/counterparties/<int:counterparty_id>")


def sync_counterparty_balance(counterparty: Counterparty) -> tuple[str, str]:
    """
    Возвращает (status, message): status — "unsupported" (для этого
    контрагента нет настроенной/реализованной интеграции, см. get_client),
    "error" (запрос к провайдеру не удался) или "success".
    """
    client = get_client(counterparty)
    if client is None:
        reason = unsupported_reason(counterparty)
        return "unsupported", _("Автоматическое обновление баланса недоступно: {reason}.").format(reason=reason)

    try:
        info = client.get_balance()
    except CounterpartyApiError as e:
        counterparty.external_balance_error = str(e)
        database.db_session.commit()
        return "error", _("Не удалось получить баланс: {error}").format(error=str(e))

    # В журнал — только если баланс реально изменился: этот прогон (и
    # ручной кнопкой, и по cron, см. докстринг выше) обычно ничего не
    # находит, писать запись на каждый такой "пустой" синк только шумело
    # бы в журнале.
    balance_changed = counterparty.external_balance != info.amount
    counterparty.external_balance = info.amount
    counterparty.external_balance_updated_at = dt.datetime.utcnow()
    counterparty.external_balance_error = None
    if balance_changed:
        audit.record(
            "counterparty_api.balance_sync", entity_type="counterparty", entity_id=counterparty.id,
            summary=f"Баланс личного кабинета контрагента {counterparty.name} обновлён: "
                    f"{audit.format_amount(info.amount)}",
        )
    database.db_session.commit()
    return "success", _("Баланс обновлён: {amount} ₽").format(amount=info.amount)


@bp.route("/sync-balance", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def sync_balance(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        flash(_("Контрагент не найден."), "danger")
        return redirect(url_for("counterparties.list_counterparties"))
    status, message = sync_counterparty_balance(counterparty)
    flash(message, "success" if status == "success" else ("warning" if status == "unsupported" else "danger"))
    return redirect(url_for("counterparties.detail", counterparty_id=counterparty_id))
