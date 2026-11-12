"""
Каждая строка интерфейса в _("...") должна иметь английский перевод в
app/i18n.py (TRANSLATIONS["en"]) — иначе в английском интерфейсе она
показывается по-русски. Тест находит строки так же, как их видит Python:
в .py — через ast (неявная конкатенация "a" "b" внутри _() склеивается в
один ключ), в шаблонах — регуляркой по _("...")/_('...').

Подписи, которые передаются в _() переменной (словари статусов, категорий
и т.п. в модулях), этот тест не видит — их переводы проверяются вручную.
"""
import ast
import pathlib
import re

from app.i18n import TRANSLATIONS

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"
CYRILLIC = re.compile("[А-Яа-яЁё]")
TEMPLATE_CALL = re.compile(r'''(?<![A-Za-z0-9_])_\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')''')
TRANSLATE_FUNCS = {"_", "translate"}


def _python_strings(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        first = node.args[0]
        if name in TRANSLATE_FUNCS and isinstance(first, ast.Constant) and isinstance(first.value, str):
            yield first.value


def _template_strings(path: pathlib.Path):
    for match in TEMPLATE_CALL.finditer(path.read_text(encoding="utf-8")):
        text = match.group(1) if match.group(1) is not None else match.group(2)
        yield text.replace('\\"', '"').replace("\\'", "'")


def test_every_ui_string_has_english_translation():
    english = TRANSLATIONS["en"]
    missing = {}
    for path in APP_DIR.rglob("*.py"):
        for text in _python_strings(path):
            if CYRILLIC.search(text) and text not in english:
                missing.setdefault(text, path.relative_to(APP_DIR.parent))
    for path in APP_DIR.rglob("*.html"):
        for text in _template_strings(path):
            if CYRILLIC.search(text) and text not in english:
                missing.setdefault(text, path.relative_to(APP_DIR.parent))
    report = "\n".join(f"{where}: {text!r}" for text, where in sorted(missing.items(), key=lambda kv: str(kv[1])))
    assert not missing, f"Нет английского перевода ({len(missing)}), добавьте в app/i18n.py:\n{report}"


def test_no_duplicate_keys_in_english_dictionary():
    """Повторный ключ в литерале словаря молча перекрывает первый — при
    разных переводах один из них теряется незаметно."""
    import collections

    source = (APP_DIR / "i18n.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict) and len(node.keys) > 100:
            counts = collections.Counter(k.value for k in node.keys if isinstance(k, ast.Constant))
            duplicates = sorted(k for k, n in counts.items() if n > 1)
            assert not duplicates, f"Повторные ключи в app/i18n.py: {duplicates}"
