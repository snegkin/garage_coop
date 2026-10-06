"""
Общая логика генерации логина по умолчанию из ФИО (Фамилия Имя Отчество —
порядок слов, как везде в проекте, см. Person.short_name) — первая буква
имени + фамилия целиком, транслитерированные в латиницу (см. translit.py).

Используется в двух местах с разной стратегией разрешения коллизий:
- setup_wizard.py (массовое создание учётных записей правлением) —
  коллизии НЕ разрешаются автоматически, показываются человеку списком для
  ручной правки прямо в форме (см. setup_wizard._build_account_rows) —
  оттуда нужен только default_login().
- auth.py (самостоятельная "регистрация" по номеру телефона при первом
  входе — см. auth.login_by_phone) — человек не видит список коллизий,
  логин ему нужен готовым сразу же, поэтому коллизии разрешаются
  эскалацией без участия человека: сначала первая буква имени + фамилия,
  при занятости — первые буквы имени И отчества + фамилия, если и это
  занято — числовой суффикс у последнего варианта. Отсюда нужен
  generate_unique_login().
"""
from .translit import transliterate


def login_key(login: str) -> str:
    """Нормализованная форма логина для нестрогого сравнения: без учёта
    регистра и с кириллицей, транслитерированной в латиницу — чтобы
    «Starasov», «STarasov», «СТарасов», «старасов» находили одного и того
    же «starasov» (люди путают регистр и раскладку). Отсюда же требование
    к уникальности: два логина с одинаковым ключом заводить нельзя (см.
    persons.create_account/change_username, setup_wizard), иначе вход по
    ключу стал бы неоднозначным."""
    return transliterate(login.strip()).lower()


def default_login(full_name: str) -> str:
    """Первая буква имени + фамилия целиком."""
    parts = full_name.strip().split()
    if not parts:
        return ""
    raw = (parts[1][0] + parts[0]) if len(parts) > 1 else parts[0]
    return transliterate(raw).lower()


def escalated_login_candidates(full_name: str) -> list[str]:
    """default_login() + вариант с добавлением первой буквы отчества (если
    оно есть в ФИО — иначе этот вариант совпал бы с первым и ничего не
    решал бы) — по возрастанию "уникальности", для автоматического
    разрешения коллизий там, где спросить человека некому."""
    parts = full_name.strip().split()
    if not parts:
        return [""]
    surname = parts[0]
    first = parts[1] if len(parts) > 1 else ""
    patronymic = parts[2] if len(parts) > 2 else ""

    candidates = [default_login(full_name)]
    if first and patronymic:
        candidates.append(transliterate(first[0] + patronymic[0] + surname).lower())
    return candidates


def generate_unique_login(full_name: str, existing_usernames) -> str:
    """Первый свободный из escalated_login_candidates(); если и они все
    заняты — числовой суффикс (2, 3...) у последнего варианта. Занятость
    проверяется по login_key() — кандидаты и так в нижнем регистре
    латиницей, т.е. сами себе ключ."""
    existing = {login_key(u) for u in existing_usernames}
    candidates = escalated_login_candidates(full_name)
    for candidate in candidates:
        if candidate and candidate not in existing:
            return candidate

    base = candidates[-1] or "user"
    n = 2
    while f"{base}{n}" in existing:
        n += 1
    return f"{base}{n}"
