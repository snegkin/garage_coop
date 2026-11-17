"""Разметка сообщений чатов (app/chat_markup.py)."""
import pytest

from app import chat_markup as cm


@pytest.mark.parametrize("text, html", [
    ("**жирный** и __курсив__", "<b>жирный</b> и <i>курсив</i>"),
    ("++под++ ~~зач~~ `код`", "<u>под</u> <s>зач</s> <code>код</code>"),
    ("```\nprint(1)\n```", "<pre>print(1)</pre>"),
    ("**[ссылка](https://a.ru) внутри**", '<b><a href="https://a.ru">ссылка</a> внутри</b>'),
    ("[сайт](ex.ru/x)", '<a href="https://ex.ru/x">сайт</a>'),
    ("см. https://ex.ru/a__b__c, и (www.ya.ru).",
     'см. <a href="https://ex.ru/a__b__c">https://ex.ru/a__b__c</a>, и (<a href="https://www.ya.ru">www.ya.ru</a>).'),
    # не разметка
    ("snake__case__x C++ и C++", "snake__case__x C++ и C++"),
    ("при**ВЕТ**", "при<b>ВЕТ</b>"),
    ("**незакрыт", "**незакрыт"),
    ("** не жирный **", "** не жирный **"),
    ("[x](javascript:alert(1))", "[x](javascript:alert(1))"),
    ('<script>&"', "&lt;script&gt;&amp;&#34;"),
    ('[a"b](https://x.ru/?q="1")', '<a href="https://x.ru/?q=&#34;1&#34;">a&#34;b</a>'),
])
def test_to_messenger_html(text, html):
    assert cm.to_messenger_html(text) == html


def test_site_links_open_in_new_tab():
    assert cm.to_html("[a](https://x.ru)") == (
        '<a href="https://x.ru" target="_blank" rel="noopener noreferrer nofollow">a</a>')


def test_plain():
    assert cm.to_plain("**жирный** [ссылка](https://a.ru) `x`") == "жирный ссылка x"


def test_from_telegram_entities_utf16():
    # 😀 — две единицы UTF-16; края выделения с пробелом подрезаются
    text = "Привет 😀 мир ссылка"
    entities = [{"type": "bold", "offset": 7, "length": 3},
                {"type": "italic", "offset": 10, "length": 4},
                {"type": "text_link", "offset": 14, "length": 6, "url": "https://x.ru"},
                {"type": "mention", "offset": 0, "length": 6}]
    assert cm.from_telegram(text, entities) == "Привет **😀** __мир__ [ссылка](https://x.ru)"


def test_from_telegram_nested_and_code():
    assert cm.from_telegram("ab cd", [{"type": "bold", "offset": 0, "length": 5},
                                      {"type": "italic", "offset": 3, "length": 2}]) == "**ab __cd__**"
    assert cm.from_telegram("x = 1", [{"type": "pre", "offset": 0, "length": 5},
                                      {"type": "bold", "offset": 0, "length": 1}]) == "```\nx = 1\n```"
    assert cm.from_telegram("t", None) == "t"


def test_from_max_markup():
    assert cm.from_max("жирный и ссылка", [
        {"type": "strong", "from": 0, "length": 6},
        {"type": "link", "from": 9, "length": 6, "url": "https://max.ru"},
    ]) == "**жирный** и [ссылка](https://max.ru)"


def test_round_trip_through_parser():
    text = cm.from_telegram("жирный и ссылка", [{"type": "bold", "offset": 0, "length": 6},
                                                {"type": "text_link", "offset": 9, "length": 6, "url": "https://x.ru"}])
    assert cm.to_messenger_html(text) == '<b>жирный</b> и <a href="https://x.ru">ссылка</a>'


# Кириллические домены и punycode: href — ASCII, показ — кириллицей.
PRES = "https://xn--d1abbgf6aiiy.xn--p1ai"


@pytest.mark.parametrize("text, html", [
    ("https://президент.рф/новости",
     f'<a href="{PRES}/%D0%BD%D0%BE%D0%B2%D0%BE%D1%81%D1%82%D0%B8">https://президент.рф/новости</a>'),
    (f"{PRES}/", f'<a href="{PRES}/">https://президент.рф/</a>'),
    ("см. президент.рф, ок", f'см. <a href="{PRES}">президент.рф</a>, ок'),
    ("xn--d1abbgf6aiiy.xn--p1ai", f'<a href="{PRES}">президент.рф</a>'),
    (f"[{PRES}](https://президент.рф/)", f'<a href="{PRES}/">https://президент.рф/</a>'),
    ("[сайт](президент.рф)", f'<a href="{PRES}">сайт</a>'),
    ("https://ru.wikipedia.org/wiki/%D0%93%D0%B0%D1%80%D0%B0%D0%B6?q=a%20b",
     '<a href="https://ru.wikipedia.org/wiki/%D0%93%D0%B0%D1%80%D0%B0%D0%B6?q=a%20b">'
     'https://ru.wikipedia.org/wiki/Гараж?q=a%20b</a>'),
    # смесь латиницы с кириллицей в одной части домена — punycode
    ("https://аpple.com", '<a href="https://xn--pple-43d.com">https://xn--pple-43d.com</a>'),
    # не адреса
    ("т.е. сайт.рфы user@почта.рф", "т.е. сайт.рфы user@почта.рф"),
])
def test_idn_links(text, html):
    assert cm.to_messenger_html(text) == html


def test_idn_display_helpers():
    assert cm.display_url("https://xn--e1afmkfd.xn--p1ai:8080/%D0%BF?a=%2F") == "https://пример.рф:8080/п?a=%2F"
    assert cm.href_url("https://Пример.РФ/путь") == "https://xn--e1afmkfd.xn--p1ai/%D0%BF%D1%83%D1%82%D1%8C"
