"""Неразрывная типографика документов (app/typography.py)."""
from markupsafe import Markup

from app.typography import NBSP, NOWRAP_OPEN, nobreak, nobreak_html


def _show(html) -> str:
    return str(html).replace(NBSP, "°")


def test_codes_articles_and_numbers_stay_together():
    out = _show(nobreak_html("<p>по ст. 7.19 КоАП РФ и КоАП РФ 17.10, гараж № 42, ИНН 6658123456</p>"))
    assert "ст.°7.19" in out and "КоАП°РФ°17.10" in out and "№°42" in out and "ИНН°6658123456" in out


def test_passport_dates_money_initials():
    out = _show(nobreak_html("<p>серия 65 12 № 345678; 23 ноября 2026 г. Иванов И. И. внёс 12 345,67 руб. — в срок</p>"))
    assert "серия°65°12 №°345678" in out
    assert "23°ноября°2026°г. Иванов" in out  # «г.» после года к фамилии не клеится
    assert "Иванов°И.°И." in out and "12°345,67°руб.°—" in out


def test_email_site_phone_wrapped_in_nowrap_without_trailing_punct():
    out = nobreak_html("<p>info@gsk-vostok.ru, https://gsk-vostok.ru/docs/. Тел. +7 (912) 345-67-89</p>")
    assert NOWRAP_OPEN + "info@gsk-vostok.ru</span>," in out
    assert NOWRAP_OPEN + "https://gsk-vostok.ru/docs/</span>." in out
    assert NOWRAP_OPEN + "+7 (912) 345-67-89</span>" in out


def test_idempotent_and_tags_untouched():
    html = '<p style="text-indent: 1.25cm">ул. Ленина, ' + NOWRAP_OPEN + 'a@b.ru</span> и c@d.ru</p>'
    once = nobreak_html(html)
    assert nobreak_html(once) == once
    assert '<p style="text-indent: 1.25cm">' in once
    assert once.count(NOWRAP_OPEN) == 2


def test_filter_escapes_plain_text_and_keeps_markup():
    assert nobreak("№ 5 <b>") == "№" + NBSP + "5 &lt;b&gt;"
    assert nobreak(Markup("<b>№ 5</b>")) == "<b>№" + NBSP + "5</b>"
    assert nobreak(None) == ""


def test_nowrap_span_survives_sanitizer():
    from app.applications import sanitize_html
    html = str(nobreak_html("<p>info@example.ru</p>"))
    assert sanitize_html(html) == html
