"""deployment/coop_vars.py: вставка и удаление кооператива в
coop_extra_instances текстом, не задевая остальной vars.yml. Модуль
загружается по пути."""

import importlib.util
import os

import pytest

yaml = pytest.importorskip("yaml")  # requirements-dev.txt

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "deployment", "coop_vars.py")
_spec = importlib.util.spec_from_file_location("coop_vars", _PATH)
coop_vars = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coop_vars)

ITEM = ["- name: coop3", "  domain: c3.ru", "  port: 8003", "  # подсказка"]


def _item(name, port):
    return {"name": name, "domain": f"{name}.ru", "port": port}


@pytest.mark.parametrize("text", [
    # списка нет (в образце он закомментирован)
    "a: 1\n# coop_extra_instances:\n#   - name: x\nb: [2]\n",
    # пустой
    "a: 1\ncoop_extra_instances: []\nb: [2]\n",
    # с отступом, комментарий внутри элемента и после блока
    "a: 1\ncoop_extra_instances:\n  - name: coop2\n    domain: coop2.ru\n    port: 8002\n"
    "    # внутри\n# после блока\nb: [2]\n",
    # без отступа
    "a: 1\ncoop_extra_instances:\n- name: coop2\n  domain: coop2.ru\n  port: 8002\nb: [2]\n",
])
def test_insert_then_remove_keeps_rest(text):
    before = yaml.safe_load(text)
    existing = before.get("coop_extra_instances") or []

    added = coop_vars.insert_item(text, ITEM)
    after = yaml.safe_load(added)
    assert after["coop_extra_instances"] == existing + [{"name": "coop3", "domain": "c3.ru", "port": 8003}]
    assert {k: v for k, v in after.items() if k != "coop_extra_instances"} == \
        {k: v for k, v in before.items() if k != "coop_extra_instances"}

    removed = coop_vars.remove_item(added, "coop3", after["coop_extra_instances"])
    assert removed is not None
    final = yaml.safe_load(removed)
    assert (final.get("coop_extra_instances") or []) == existing
    assert final["a"] == 1 and final["b"] == [2]
    assert "подсказка" not in removed
    if "после блока" in text:
        assert "# после блока" in removed


def test_remove_middle_and_last():
    text = ("coop_extra_instances:\n"
            "  - name: a\n    domain: a.ru\n    port: 8001\n"
            "  - name: b\n    domain: b.ru\n    port: 8002\n"
            "  - name: c\n    domain: c.ru\n    port: 8003\n"
            "z: 1\n")
    items = [_item("a", 8001), _item("b", 8002), _item("c", 8003)]
    for item in items:
        item["domain"] = item["name"] + ".ru"

    without_b = coop_vars.remove_item(text, "b", items)
    assert yaml.safe_load(without_b)["coop_extra_instances"] == [items[0], items[2]]

    only_a = coop_vars.remove_item(without_b, "c", [items[0], items[2]])
    none_left = coop_vars.remove_item(only_a, "a", [items[0]])
    assert "coop_extra_instances: []" in none_left
    assert yaml.safe_load(none_left) == {"coop_extra_instances": [], "z": 1}


def test_one_line_list_is_refused():
    with pytest.raises(SystemExit):
        coop_vars.insert_item("coop_extra_instances: [{name: a}]\n", ITEM)


def test_punycode():
    assert coop_vars.to_punycode("Гаражи-Тест.РФ") == "xn----7sbakneo5doic.xn--p1ai"
    assert coop_vars.to_punycode("https://example.ru/") == "example.ru"
