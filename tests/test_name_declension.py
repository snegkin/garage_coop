"""
app/name_declension.py — склонение ФИО для грамматически верных
формулировок в исковых документах («взыскать с Иванова Ивана Ивановича»,
не «с Иванов Иван Иванович»). Пол определяется по окончанию отчества, не
через petrovich (у него нет отдельного метода определения пола).
"""
from app.name_declension import genitive, dative


def test_genitive_male_name():
    assert genitive("Иванов Иван Иванович") == "Иванова Ивана Ивановича"


def test_genitive_female_name():
    assert genitive("Петрова Мария Сергеевна") == "Петровой Марии Сергеевны"


def test_dative_male_name():
    assert dative("Иванов Иван Иванович") == "Иванову Ивану Ивановичу"


def test_dative_female_name():
    assert dative("Петрова Мария Сергеевна") == "Петровой Марии Сергеевне"


def test_name_without_patronymic_is_returned_unchanged():
    """Пол по одному отчеству не определить без него — лучше нейтральный
    именительный падеж, чем склонение наугад."""
    assert genitive("Иванов Иван") == "Иванов Иван"


def test_name_with_unusual_word_count_is_returned_unchanged():
    assert genitive("Иванов-Петров Иван Иванович Третий") == "Иванов-Петров Иван Иванович Третий"


def test_empty_or_none_name_does_not_crash():
    assert genitive("") == ""
    assert genitive(None) is None
