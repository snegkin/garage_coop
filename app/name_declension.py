"""
Склонение ФИО по падежам — для грамматически корректных формулировок в
исковых документах (app/legal_docs.py): «взыскать с Иванова Ивана
Ивановича», а не дословно «с Иванов Иван Иванович».

Обёртка над библиотекой petrovich (github.com/damirazo/Petrovich) — она
НЕ идеальна (напр. «Ольга» → родительный «Ольгы» вместо «Ольги», известное
ограничение её таблицы правил на некоторых основах), поэтому результат —
только справочная подсказка для черновика, который правление в любом
случае обязано проверить перед подачей в суд (см. build_lawsuit_body),
а не гарантированно верная форма.

Пол определяется по окончанию отчества (мужские «-ович/-евич/-ич»,
женские «-овна/-евна/-инична») — у petrovich нет отдельного метода
определения пола. Если ФИО не в формате «Фамилия Имя Отчество» (ровно три
слова, как принято в карточке человека — см. Person.full_name) или пол не
определился, имя возвращается БЕЗ ИЗМЕНЕНИЙ: нейтральный именительный
падеж лучше, чем склонение наугад.
"""
from petrovich.main import Petrovich
from petrovich.enums import Case, Gender

_petrovich = Petrovich()

_MALE_PATRONYMIC_SUFFIXES = ("ович", "евич", "ич")
_FEMALE_PATRONYMIC_SUFFIXES = ("овна", "евна", "инична", "ична")


def _detect_gender(middle_name: str) -> "Gender | None":
    lower = middle_name.lower()
    if lower.endswith(_FEMALE_PATRONYMIC_SUFFIXES):
        return Gender.FEMALE
    if lower.endswith(_MALE_PATRONYMIC_SUFFIXES):
        return Gender.MALE
    return None


def decline(full_name: str, case: "Case") -> str:
    parts = (full_name or "").split()
    if len(parts) != 3:
        return full_name
    last, first, middle = parts
    gender = _detect_gender(middle)
    if gender is None:
        return full_name
    try:
        return " ".join([
            _petrovich.lastname(last, case, gender),
            _petrovich.firstname(first, case, gender),
            _petrovich.middlename(middle, case, gender),
        ])
    except Exception:
        return full_name


def gender(full_name: str) -> str | None:
    """"male"/"female" по отчеству, None — не определился (для согласования
    «являющийся/являющаяся» в тексте документов)."""
    parts = (full_name or "").split()
    if len(parts) != 3:
        return None
    detected = _detect_gender(parts[2])
    return {Gender.MALE: "male", Gender.FEMALE: "female"}.get(detected)


def genitive(full_name: str) -> str:
    """«кого/чего» — напр. «взыскать с Иванова Ивана Ивановича»."""
    return decline(full_name, Case.GENITIVE)


def dative(full_name: str) -> str:
    """«кому/чему» — напр. «иск к Иванову Ивану Ивановичу»."""
    return decline(full_name, Case.DATIVE)


def respected(full_name: str) -> str:
    """«Уважаемый»/«Уважаемая» по отчеству, «Уважаемый(ая)» — если род не
    определился."""
    return {"male": "Уважаемый", "female": "Уважаемая"}.get(gender(full_name), "Уважаемый(ая)")


def first_and_middle(full_name: str) -> str:
    """Имя и отчество («Анна Ивановна») — для обращения; без отчества или
    при неполном ФИО — ФИО как есть."""
    parts = (full_name or "").split()
    return " ".join(parts[1:]) if len(parts) == 3 else (full_name or "")


def greeting(full_name: str) -> str:
    """Обращение в письме: «Уважаемая Анна Ивановна»."""
    return f"{respected(full_name)} {first_and_middle(full_name)}"
