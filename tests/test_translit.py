"""app/translit.py — транслитерация кириллицы в латиницу, общая для логина
по умолчанию в мастере настройки (setup_wizard.py) и slug страницы вики
(wiki.py)."""
from app.translit import transliterate


def test_transliterate_basic_word():
    assert transliterate("иванов") == "ivanov"


def test_transliterate_preserves_case():
    assert transliterate("Иванов") == "Ivanov"
    assert transliterate("ИВАНОВ") == "IVANOV"


def test_transliterate_multi_letter_mappings():
    assert transliterate("щука") == "schuka"
    assert transliterate("ёж") == "ezh"


def test_transliterate_drops_soft_and_hard_signs():
    assert transliterate("объём") == "obem"


def test_transliterate_leaves_non_cyrillic_as_is():
    assert transliterate("Петров-Водкин 2") == "Petrov-Vodkin 2"


def test_transliterate_empty_string():
    assert transliterate("") == ""
