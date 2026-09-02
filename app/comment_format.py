"""
Рендер комментария начисления/платежа (Charge.comment / Payment.comment) с
кликабельным ФИО упомянутого человека — для автосгенерированных
комментариев про унаследованный долг/переплату при выбытии/замене
собственника (см. accounting.transfer_member_account_balance,
accounting.redistribute_member_account_balance — они же проставляют
related_person_id рядом с текстом).

Комментарий остаётся обычным текстом (нужен для печати, экспорта и т.п.);
ссылка строится только для отображения в HTML, только если известен
related_person (значит текст сгенерирован нашим кодом, а не введён вручную
председателем — в комментариях, введённых вручную, related_person_id не
проставляется, поэтому чужое ФИО в свободном тексте никогда не превращается
в ссылку случайно) и только зрителю, которому вообще можно ходить по
ссылкам на чужие карточки людей (правление).
"""
from flask import url_for
from markupsafe import Markup


def linkify_related_person(comment: str | None, related_person, can_link: bool) -> Markup | str | None:
    if not comment or not related_person or not can_link:
        return comment
    name = related_person.full_name
    if name not in comment:
        return comment
    before, _, after = comment.partition(name)
    link = Markup('<a href="{}">{}</a>').format(
        url_for("persons.detail", person_id=related_person.id), name,
    )
    return Markup("{}{}{}").format(before, link, after)
