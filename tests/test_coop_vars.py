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


JOBS = [{"name": "backup_db"}, {"name": "sync_bank_accounts"},
        {"name": "dvr_snapshot", "enabled": False}, {"name": "poll_ewelink", "enabled": False}]


def test_cron_lists_and_enabled():
    enabled = {"backup_db", "dvr_snapshot"}
    assert coop_vars.cron_lists(JOBS, enabled) == (["dvr_snapshot"], ["sync_bank_accounts"])
    item = {"cron_enable": ["dvr_snapshot"], "cron_disable": ["sync_bank_accounts"]}
    assert coop_vars.cron_enabled(JOBS, item) == enabled
    assert coop_vars.cron_lists(JOBS, coop_vars.cron_enabled(JOBS, {})) == ([], [])


def test_item_lines_round_trip():
    item = {
        "env_extra": {"FOO": 'a "b" {{ x }}'}, "name": "coop2", "domain": "c2.ru",
        "aliases": ["c2.example.ru"], "port": 8001, "secret_key": "s" * 40,
        "bank_api_encryption_key": "b", "recaptcha_site_key": "", "cron_disable": ["sync_bank_accounts"],
        "cron_minute_offset": 0, "backup_agent_pubkeys": ["ssh-ed25519 AAAA moto"],
        "rclone_yandex_token": '{"access_token":"t","expiry":"2026-01-01T00:00:00+03:00"}',
        "restore_db_file": "/home/u/Тест/coop.db",
    }
    lines = coop_vars.item_lines(item)
    assert lines[0] == "- name: coop2"
    assert not any("recaptcha" in line for line in lines)      # пустые не пишутся
    expected = {k: v for k, v in item.items() if v != ""}
    assert yaml.safe_load("\n".join(lines)) == [expected]


def test_replace_item_keeps_rest():
    text = ("a: 1\ncoop_extra_instances:\n"
            "  - name: a\n    domain: a.ru\n    port: 8001\n    # внутри\n"
            "  - name: b\n    domain: b.ru\n    port: 8002\n"
            "# после\nz: [1]\n")
    items = [_item("a", 8001), _item("b", 8002)]
    new = dict(items[0], cron_disable=["dvr_snapshot"])
    result = coop_vars.replace_item(text, "a", items, coop_vars.item_lines(new))
    assert yaml.safe_load(result) == {"a": 1, "coop_extra_instances": [new, items[1]], "z": [1]}
    assert "# после" in result


def test_editor_validates_with_playbook_filter():
    """Меню проверяет запись тем же фильтром, что и плейбук."""
    filters = coop_vars._filter_module()
    primary = {"app_user": "coop", "app_port": 8000, "coop_domain": "main.ru", "coop_domain_aliases": [],
               "app_secret_key": "p" * 40, "service_name": "garage-coop", "cron_jobs": JOBS}
    good = yaml.safe_load("\n".join(coop_vars.item_lines(
        {"name": "c2", "domain": "c2.ru", "port": 8001, "secret_key": "s" * 40,
         "bank_api_encryption_key": "b", "cron_disable": ["sync_bank_accounts"]})))
    assert filters.coop_instances(good, primary)[0]["cron_disable"] == ["sync_bank_accounts"]
    good[0]["cron_enable"] = ["nope"]
    with pytest.raises(Exception, match="nope"):
        filters.coop_instances(good, primary)
