"""Фильтры плейбука (deployment/ansible/filter_plugins/coop.py): проверка
coop_extra_instances и сдвиг минут cron. Модуль загружается по пути, без
ansible."""

import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "deployment", "ansible", "filter_plugins", "coop.py")
_spec = importlib.util.spec_from_file_location("coop_filters", _PATH)
coop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coop)

PRIMARY = {
    "app_user": "coop",
    "app_port": 8000,
    "coop_domain": "xn--main.xn--p1ai",
    "coop_domain_aliases": ["main.example.ru"],
    "app_secret_key": "p" * 40,
    "service_name": "garage-coop",
    "cron_jobs": [{"name": "backup_db"}, {"name": "poll_messengers"}],
}


def _inst(**kw):
    base = {"name": "coop2", "domain": "xn--second.xn--p1ai", "port": 8001,
            "secret_key": "s" * 40, "bank_api_encryption_key": "b"}
    base.update(kw)
    return base


def test_empty():
    assert coop.coop_instances([], PRIMARY) == []
    assert coop.coop_instances(None, PRIMARY) == []


def test_derived_fields_and_defaults():
    [a, b] = coop.coop_instances(
        [_inst(), _inst(name="coop3", domain="c3.example.ru", port="8002", cron_minute_offset=65)], PRIMARY)
    assert a["user"] == "coop2"
    assert a["env_file"] == "/home/coop2/coop.env"
    assert a["instance_dir"] == "/home/coop2/instance"
    assert a["service"] == "garage-coop-coop2"
    assert a["cron_minute_offset"] == 7          # 7 × номер в списке
    assert a["env_extra"] == {} and a["backup_agent_pubkeys"] == []
    assert b["port"] == 8002 and b["cron_minute_offset"] == 5


@pytest.mark.parametrize("kw, message", [
    ({"name": ""}, "не заданы name"),
    ({"name": "Coop2"}, "name —"),
    ({"name": "coop"}, "app_user"),
    ({"domain": "гм-восход.рф"}, "punycode"),
    ({"domain": "xn--main.xn--p1ai"}, "coop_domain"),
    ({"port": 8000}, "port"),
    ({"port": "x"}, "port — число"),
    ({"secret_key": "short"}, "короче 32"),
    ({"secret_key": "p" * 40}, "как у основного"),
    ({"cron_enable": ["nope"]}, "nope"),
    ({"typo_field": 1}, "неизвестные поля typo_field"),
    ({"aliases": "second.example.ru"}, "aliases — список"),
    ({"aliases": ["второй.рф"]}, "punycode"),
    ({"aliases": ["main.example.ru"]}, "основного"),
    ({"domain": "main.example.ru"}, "основного"),
    ({"aliases": ["xn--second.xn--p1ai"]}, "повторяются"),
])
def test_validation(kw, message):
    with pytest.raises(coop.AnsibleFilterError, match=message):
        coop.coop_instances([_inst(**kw)], PRIMARY)


@pytest.mark.parametrize("field, value", [("name", "coop2"), ("port", 8001), ("domain", "xn--second.xn--p1ai")])
def test_duplicates(field, value):
    other = {"name": "coop3", "port": 8002, "domain": "c3.example.ru", field: value}
    with pytest.raises(coop.AnsibleFilterError, match="уже занят"):
        coop.coop_instances([_inst(), _inst(**other)], PRIMARY)


def test_aliases():
    [a] = coop.coop_instances([_inst(aliases=["second.example.ru"])], PRIMARY)
    assert a["aliases"] == ["second.example.ru"]
    assert coop.coop_instances([_inst()], PRIMARY)[0]["aliases"] == []


def test_alias_taken_by_other_instance():
    with pytest.raises(coop.AnsibleFilterError, match="уже занят"):
        coop.coop_instances([_inst(aliases=["c3.example.ru"]),
                             _inst(name="coop3", port=8002, domain="c3.example.ru")], PRIMARY)


@pytest.mark.parametrize("minute, offset, expected", [
    ("30", 7, "37"),
    ("55", 7, "2"),
    ("*/5", 7, "2-59/5"),
    ("*/5", 10, "*/5"),
    ("*", 7, "*"),
    ("0,30", 7, "7,37"),
    ("10-20", 7, "10-20"),
    (0, 0, "0"),
])
def test_cron_shift_minute(minute, offset, expected):
    assert coop.cron_shift_minute(minute, offset) == expected
