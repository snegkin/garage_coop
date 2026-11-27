"""Фильтры плейбука site.yml для нескольких кооперативов на одном сервере.

coop_instances — проверяет coop_extra_instances из vars.yml и дополняет
каждый кооператив вычисляемыми полями (пользователь, служба, пути), чтобы
соглашения об именах жили в одном месте.

coop_landings — проверяет coop_landing_sites (лендинги на свободных
доменах): punycode, не занят кооперативом, без повторов.

coop_lan_vpn — проверяет lan_vpn_peers (WireGuard от сервера до роутеров
кооперативов): ключи, адреса в туннеле, подсети без пересечений.

cron_shift_minute — сдвигает поле «минута» строки cron, чтобы скрипты
разных кооперативов не стартовали в одну и ту же минуту.

Ansible подгружает этот каталог сам (filter_plugins/ рядом с плейбуком);
тесты — tests/test_ansible_filters.py.
"""

import base64
import ipaddress
import re

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # тесты без ansible
    class AnsibleFilterError(Exception):
        pass


NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")

OPTIONAL = {
    "aliases": [],
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
    coop_domain, coop_domain_aliases, app_secret_key, service_name, cron_jobs}."""
    if not instances:
        return []
    if not isinstance(instances, list):
        raise AnsibleFilterError("coop_extra_instances — список кооперативов")
    cron_names = {job["name"] for job in primary.get("cron_jobs") or []}
    primary_domains = {primary.get("coop_domain")} | set(primary.get("coop_domain_aliases") or [])
    primary_domains.discard("")
    primary_domains.discard(None)
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
        if name.startswith(f"{primary['service_name']}-"):
            raise AnsibleFilterError(
                f"{label}: name без «{primary['service_name']}-» в начале — служба и так {primary['service_name']}-ИМЯ")

        domain = str(raw["domain"])
        aliases = raw.get("aliases") or []
        if not isinstance(aliases, list):
            raise AnsibleFilterError(f"{label}: aliases — список доменов")
        aliases = [str(alias) for alias in aliases]
        for value in [domain] + aliases:
            if not DOMAIN_RE.match(value):
                raise AnsibleFilterError(
                    f"{label}: {value} — домен в punycode: python3 -c \"print('пример.рф'.encode('idna').decode())\"")
            if value in primary_domains:
                raise AnsibleFilterError(f"{label}: домен {value} уже у основного кооператива (coop_domain или coop_domain_aliases)")
        if len(set([domain] + aliases)) != len(aliases) + 1:
            raise AnsibleFilterError(f"{label}: домены в domain и aliases повторяются")

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

        for key, value in [("name", name), ("port", port)] + [("domain", d) for d in [domain] + aliases]:
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
            aliases=aliases,
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


def coop_landings(landings, taken):
    """landings — coop_landing_sites [{domain, aliases?}]; taken — домены
    кооперативов. Проверка и вычисляемое имя vhost (service)."""
    if not landings:
        return []
    if not isinstance(landings, list):
        raise AnsibleFilterError("coop_landing_sites — список {domain, aliases}")
    taken = {str(d) for d in taken or [] if d}
    seen, result = set(), []
    for index, raw in enumerate(landings, start=1):
        if not isinstance(raw, dict) or not raw.get("domain"):
            raise AnsibleFilterError(f"coop_landing_sites[{index}]: нужен словарь с domain")
        unknown = sorted(set(raw) - {"domain", "aliases"})
        if unknown:
            raise AnsibleFilterError(f"coop_landing_sites[{index}]: неизвестные поля {', '.join(unknown)}")
        aliases = raw.get("aliases") or []
        if not isinstance(aliases, list):
            raise AnsibleFilterError(f"coop_landing_sites[{index}]: aliases — список доменов")
        domain, aliases = str(raw["domain"]), [str(a) for a in aliases]
        for value in [domain] + aliases:
            if not DOMAIN_RE.match(value):
                raise AnsibleFilterError(
                    f"coop_landing_sites: {value} — домен в punycode: "
                    f"python3 -c \"print('пример.рф'.encode('idna').decode())\"")
            if value in taken:
                raise AnsibleFilterError(f"coop_landing_sites: {value} уже у кооператива — уберите его оттуда")
            if value in seen:
                raise AnsibleFilterError(f"coop_landing_sites: {value} указан дважды")
            seen.add(value)
        result.append({"domain": domain, "aliases": aliases, "service": f"landing-{domain}"})
    return result


def _natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def idna_label(domain):
    """Домен для показа людям: xn--…  → кириллица; не вышло — как есть."""
    try:
        return str(domain).encode("ascii").decode("idna")
    except UnicodeError:
        return str(domain)


def landing_links(domains, landing_domain):
    """Ссылки лендинга на сайты кооперативов — домены из domains, которые
    являются поддоменами landing_domain: [{url, label}], 2.… раньше 10.…"""
    suffix = "." + str(landing_domain)
    links = [{"url": f"https://{d}", "label": idna_label(d)}
             for d in dict.fromkeys(str(d) for d in domains or [] if d) if d.endswith(suffix)]
    return sorted(links, key=lambda link: _natural_key(link["label"]))


LAN_VPN_FIELDS = {"name", "public_key", "address", "subnets", "coop"}


def coop_lan_vpn(peers, server_address, coops):
    """peers — lan_vpn_peers [{name, public_key, address, subnets, coop?}];
    server_address — адрес сервера в туннеле (10.77.0.1/24); coops — имена
    дополнительных кооперативов ("" в coop — основной). Возвращает пиры с
    allowed_ips для конфига сервера."""
    if not peers:
        return []
    if not isinstance(peers, list):
        raise AnsibleFilterError("lan_vpn_peers — список роутеров {name, public_key, address, subnets}")
    try:
        server = ipaddress.ip_interface(str(server_address))
    except ValueError:
        raise AnsibleFilterError(f"lan_vpn_address: {server_address} — адрес с маской, например 10.77.0.1/24") from None
    coops = {str(c) for c in coops or []}
    result, keys, addresses, nets = [], set(), {server.ip}, []
    for index, raw in enumerate(peers, start=1):
        if not isinstance(raw, dict):
            raise AnsibleFilterError(f"lan_vpn_peers[{index}]: нужен словарь с name, public_key, address, subnets")
        label = f"lan_vpn_peers: {raw.get('name') or f'№{index}'}"
        missing = [key for key in ("name", "public_key", "address", "subnets") if not raw.get(key)]
        if missing:
            raise AnsibleFilterError(f"{label}: не заданы {', '.join(missing)}")
        unknown = sorted(set(raw) - LAN_VPN_FIELDS)
        if unknown:
            raise AnsibleFilterError(f"{label}: неизвестные поля {', '.join(unknown)}")

        key = str(raw["public_key"]).strip()
        try:
            valid_key = len(base64.b64decode(key, validate=True)) == 32
        except ValueError:
            valid_key = False
        if not valid_key:
            raise AnsibleFilterError(f"{label}: public_key — открытый ключ WireGuard роутера (44 символа base64)")
        if key in keys:
            raise AnsibleFilterError(f"{label}: этот public_key уже у другого роутера")
        keys.add(key)

        try:
            address = ipaddress.ip_address(str(raw["address"]).split("/")[0])
        except ValueError:
            raise AnsibleFilterError(f"{label}: address — адрес роутера в туннеле, например 10.77.0.2") from None
        if address not in server.network:
            raise AnsibleFilterError(f"{label}: address {address} вне сети туннеля {server.network}")
        if address in addresses:
            raise AnsibleFilterError(f"{label}: address {address} уже занят (сервер или другой роутер)")
        addresses.add(address)

        subnets = raw["subnets"]
        if isinstance(subnets, str):
            subnets = [part for part in re.split(r"[,\s]+", subnets) if part]
        if not isinstance(subnets, list):
            raise AnsibleFilterError(f"{label}: subnets — список подсетей, например [192.168.2.0/24]")
        parsed = []
        for value in subnets:
            try:
                net = ipaddress.ip_network(str(value), strict=False)
            except ValueError:
                raise AnsibleFilterError(f"{label}: {value} — не подсеть") from None
            if net.overlaps(server.network):
                raise AnsibleFilterError(f"{label}: подсеть {net} пересекается с туннелем {server.network}")
            for other, owner in nets:
                if net.overlaps(other):
                    raise AnsibleFilterError(
                        f"{label}: подсеть {net} пересекается с {other} роутера «{owner}» — сервер не поймёт, "
                        f"куда слать пакеты. Смените подсеть локальной сети на одном из роутеров")
            nets.append((net, str(raw["name"])))
            parsed.append(str(net))

        coop = str(raw.get("coop") or "")
        if coop and coop not in coops:
            raise AnsibleFilterError(f"{label}: coop {coop} — нет такого в coop_extra_instances (пусто — основной)")
        result.append({"name": str(raw["name"]), "public_key": key, "address": str(address),
                       "subnets": parsed, "coop": coop,
                       "allowed_ips": ", ".join([f"{address}/{address.max_prefixlen}"] + parsed)})
    return result


class FilterModule:
    def filters(self):
        return {"coop_instances": coop_instances, "cron_shift_minute": cron_shift_minute,
                "coop_landings": coop_landings, "coop_lan_vpn": coop_lan_vpn, "landing_links": landing_links}
