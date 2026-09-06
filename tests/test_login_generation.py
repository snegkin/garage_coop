"""app/login_generation.py — генерация логина по умолчанию из ФИО и его
эскалирующее автоматическое разрешение коллизий (для auth.login_by_phone,
где спросить человека некому — в отличие от setup_wizard, где коллизии
показываются в форме для ручной правки)."""
from app.login_generation import default_login, escalated_login_candidates, generate_unique_login


def test_default_login_first_initial_plus_surname():
    assert default_login("Иванов Иван Иванович") == "iivanov"


def test_default_login_single_word_name():
    assert default_login("Иванов") == "ivanov"


def test_escalated_candidates_include_patronymic_variant_when_available():
    assert escalated_login_candidates("Иванов Иван Иванович") == ["iivanov", "iiivanov"]


def test_escalated_candidates_skip_patronymic_variant_when_missing():
    assert escalated_login_candidates("Иванов Иван") == ["iivanov"]


def test_generate_unique_login_uses_default_when_free():
    assert generate_unique_login("Иванов Иван Иванович", existing_usernames=set()) == "iivanov"


def test_generate_unique_login_escalates_to_patronymic_variant_on_collision():
    assert generate_unique_login("Иванов Иван Иванович", existing_usernames={"iivanov"}) == "iiivanov"


def test_generate_unique_login_appends_numeric_suffix_when_still_colliding():
    assert generate_unique_login(
        "Иванов Иван Иванович", existing_usernames={"iivanov", "iiivanov"},
    ) == "iiivanov2"


def test_generate_unique_login_appends_numeric_suffix_when_no_patronymic():
    """Без отчества эскалационного варианта нет — сразу числовой суффикс у
    единственного (базового) кандидата."""
    assert generate_unique_login("Иванов Иван", existing_usernames={"iivanov"}) == "iivanov2"


def test_generate_unique_login_finds_first_free_numeric_suffix():
    assert generate_unique_login(
        "Иванов Иван Иванович", existing_usernames={"iivanov", "iiivanov", "iiivanov2", "iiivanov3"},
    ) == "iiivanov4"
