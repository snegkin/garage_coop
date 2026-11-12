"""Фильтры плейбука site.yml для нескольких кооперативов на одном сервере.

coop_instances — проверяет coop_extra_instances из vars.yml и дополняет
каждый кооператив вычисляемыми полями (пользователь, служба, пути), чтобы
соглашения об именах жили в одном месте.

cron_shift_minute — сдвигает поле «минута» строки cron, чтобы скрипты
разных кооперативов не стартовали в одну и ту же минуту.

Ansible подгружает этот каталог сам (filter_plugins/ рядом с плейбуком);
тесты — tests/test_ansible_filters.py.
"""

import re

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # тесты без ansible
    class AnsibleFilterError(Exception):
        pass


NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")

OPTIONAL = {
    "recaptcha_site_key": "",
    "recaptcha_secret_key": "",
    "env_extra": {},
    "cron_enable": [],
    "cron_disable": [],
    "backup_agent_pubkeys": [],
    "rclone_yandex_token": "",
    "restore_db_file": "",
    "restore_source_instance_dir": "",
}
REQUIRED = ("name", "domain", "port", "secret_key", "bank_api_encryption_key")
KNOWN = set(REQUIRED) | set(OPTIONAL) | {"cron_minute_offset"}


def coop_instances(instances, primary):
    """instances — coop_extra_instances; primary — {app_user, app_port,
    coop_domain, app_secret_key, service_name, cron_jobs}."""
    if not instances:
        return []
    if not isinstance(instances, list):
        raise AnsibleFilterError("coop_extra_instances — список кооперативов")
    cron_names = {job["name"] for job in primary.get("cron_jobs") or []}
    result, seen = [], {"name": set(), "port": set(), "domain": set()}
    for index, raw in enumerate(instances, start=1):
        if not isinstance(raw, dict):
            raise AnsibleFilterError(f"coop_extra_instances[{index}]: нужен словарь с name, domain, port, …")
        label = f"coop_extra_instances: {raw.get('name') or f'№{index}'}"
        missing = [key for key in REQUIRED if not raw.get(key)]
        if missing:
            raise AnsibleFilterError(f"{label}: не заданы {', '.join(missing)}")
        unknown = sorted(set(raw) - KNOWN)
        if unknown:
            raise AnsibleFilterError(f"{label}: неизвестные поля {', '.join(unknown)}")

        name = str(raw["name"])
        if not NAME_RE.match(name):
            raise AnsibleFilterError(f"{label}: name — латиница в нижнем регистре, цифры и «-», с буквы")
        if name == primary["app_user"]:
            raise AnsibleFilterError(f"{label}: name совпадает с app_user основного кооператива")

        domain = str(raw["domain"])
        if not DOMAIN_RE.match(domain):
            raise AnsibleFilterError(
                f"{label}: domain — в punycode: python3 -c \"print('пример.рф'.encode('idna').decode())\"")
        if domain == primary.get("coop_domain"):
            raise AnsibleFilterError(f"{label}: domain совпадает с coop_domain основного кооператива")

        try:
            port = int(raw["port"])
        except (TypeError, ValueError):
            raise AnsibleFilterError(f"{label}: port — число") from None
        if not 1024 <= port <= 65535 or port == int(primary["app_port"]):
            raise AnsibleFilterError(f"{label}: port — 1024…65535 и не app_port основного ({primary['app_port']})")

        secret = str(raw["secret_key"])
        if len(secret) < 32:
            raise AnsibleFilterError(f"{label}: secret_key короче 32 символов")
        if secret == primary.get("app_secret_key"):
            raise AnsibleFilterError(f"{label}: secret_key как у основного кооператива — нужен свой")

        for key, value in (("name", name), ("port", port), ("domain", domain)):
            if value in seen[key]:
                raise AnsibleFilterError(f"{label}: {key} {value} уже занят другим кооперативом из списка")
            seen[key].add(value)

        item = {key: raw.get(key, default) for key, default in OPTIONAL.items()}
        bad_cron = sorted((set(item["cron_enable"]) | set(item["cron_disable"])) - cron_names) if cron_names else []
        if bad_cron:
            raise AnsibleFilterError(f"{label}: нет таких задач в cron_jobs: {', '.join(bad_cron)}")

        home = f"/home/{name}"
        item.update(
            name=name,
            domain=domain,
            port=port,
            secret_key=secret,
            bank_api_encryption_key=str(raw["bank_api_encryption_key"]),
            cron_minute_offset=int(raw.get("cron_minute_offset", 7 * index)) % 60,
            user=name,
            home=home,
            env_file=f"{home}/coop.env",
            instance_dir=f"{home}/instance",
            service=f"{primary['service_name']}-{name}",
        )
        result.append(item)
    return result


def _shift_part(part, offset):
    if part.isdigit():
        return str((int(part) + offset) % 60)
    step = re.fullmatch(r"\*/(\d+)", part)
    if step:
        n = int(step.group(1))
        start = offset % n
        return f"{start}-59/{n}" if start else part
    return part  # *, диапазоны и прочее — как есть


def cron_shift_minute(minute, offset):
    """«30» + 7 → «37», «*/5» + 7 → «2-59/5», «0,30» + 7 → «7,37», «*» — как
    есть. Через час не переносит: «55» + 7 → «2» того же часа."""
    offset = int(offset) % 60
    minute = str(minute)
    if offset == 0:
        return minute
    return ",".join(_shift_part(part, offset) for part in minute.split(","))


class FilterModule:
    def filters(self):
        return {"coop_instances": coop_instances, "cron_shift_minute": cron_shift_minute}
