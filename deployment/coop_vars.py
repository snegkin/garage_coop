#!/usr/bin/env python3
"""Список coop_extra_instances в vars.yml — для add-coop.sh и remove-coop.sh.

    coop_vars.py add VARS_FILE DEFAULTS_FILE [INVENTORY_FILE]
        меню: новый кооператив (имя, домен — кириллицу в punycode, алиасы,
        порт, ключи генерируются) или правка существующего; все поля
        coop_extra_instances (reCAPTCHA, cron, бэкап, Яндекс.Диск, перенос БД,
        env_extra). Перед записью — та же проверка, что в плейбуке
        (ansible/filter_plugins/coop.py), и DNS
    coop_vars.py list VARS_FILE
        «имя<TAB>домен<TAB>порт» — по строке на кооператив
    coop_vars.py remove VARS_FILE NAME
        убрать кооператив из списка

Остальной vars.yml с комментариями не трогается: элемент вставляется или
вырезается текстом, затем файл перечитывается и сравнивается с ожидаемым —
не совпало, файл не меняется.
"""
import importlib.util
import json
import os
import re
import secrets
import socket
import sys
import tempfile

import yaml

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")
KEY_RE = re.compile(r"^coop_extra_instances:[ \t]*(?P<value>[^#\n]*?)[ \t]*(?:#.*)?$", re.M)


def die(message):
    print(f"Ошибка: {message}", file=sys.stderr)
    sys.exit(1)


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        print()
        die("ввод прерван")
    return answer or default


def to_punycode(domain):
    domain = domain.strip().strip(".").lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError(f"{domain} — не похоже на домен") from None


def resolve(host):
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


def inventory_host(path):
    """ansible_host первого хоста из inventory.ini (или имя хоста)."""
    if not path or not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("["):
                continue
            match = re.search(r"\bansible_host=(\S+)", line)
            return match.group(1) if match else line.split()[0]
    return ""


def insert_item(text, item_lines):
    """Текст vars.yml с новым элементом в конце списка coop_extra_instances."""
    match = KEY_RE.search(text)
    if not match:
        block = (
            "\n# Дополнительные кооперативы (добавлены deployment/add-coop.sh):\n"
            "coop_extra_instances:\n" + "".join(f"  {line}\n" for line in item_lines)
        )
        return text.rstrip("\n") + "\n" + block
    value = match.group("value")
    if value not in ("", "[]"):
        die("coop_extra_instances в vars.yml записан в одну строку — перепишите списком «- name: …» и запустите снова")
    lines = text.splitlines(keepends=True)
    start = text.count("\n", 0, match.start())
    if value == "[]":
        lines[start] = "coop_extra_instances:\n"
    # Конец блока — первая непустая строка с колонки 0, кроме «- » (элементы
    # без отступа); вставка — после последней строки блока, включая
    # комментарии внутри элемента, но до комментариев с колонки 0.
    end = start + 1
    last = start
    indent = None
    while end < len(lines):
        line = lines[end]
        stripped = line.strip()
        if stripped:
            at_column0 = not line[0].isspace() and not line.startswith("-")
            if at_column0 and not stripped.startswith("#"):
                break
            if not at_column0:
                if indent is None and stripped.startswith("- "):
                    indent = line[: len(line) - len(line.lstrip())]
                last = end
        end += 1
    indent = "  " if indent is None else indent
    if not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines[last + 1:last + 1] = [f"{indent}{line}\n" for line in item_lines]
    return "".join(lines)


def load_vars(vars_path):
    if not os.path.isfile(vars_path):
        die(f"нет {vars_path} — сначала настройте основной кооператив (ansible/README.md)")
    with open(vars_path, encoding="utf-8") as f:
        text = f.read()
    try:
        config = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        die(f"{vars_path} не читается как YAML: {exc}")
    existing = config.get("coop_extra_instances") or []
    if not isinstance(existing, list):
        die("coop_extra_instances в vars.yml — не список")
    return text, config, existing


def write_vars(vars_path, old_config, new_text, expected_list):
    """Записать new_text, если в нём изменился только coop_extra_instances
    и стал ровно expected_list."""
    try:
        new_config = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        die(f"после правки vars.yml не читается ({exc}) — файл не изменён")
    rest = {k: v for k, v in old_config.items() if k != "coop_extra_instances"}
    new_rest = {k: v for k, v in new_config.items() if k != "coop_extra_instances"}
    if (new_config.get("coop_extra_instances") or []) != expected_list or new_rest != rest:
        die("правка задела другие настройки — файл не изменён; поправьте vars.yml вручную (ansible/README.md)")
    mode = os.stat(vars_path).st_mode & 0o777
    directory = os.path.dirname(os.path.abspath(vars_path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".vars.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.chmod(tmp, mode & 0o700 or 0o600)
    os.replace(tmp, vars_path)


def cmd_list(vars_path):
    _, _, existing = load_vars(vars_path)
    for inst in existing:
        print(f"{inst.get('name')}\t{inst.get('domain')}\t{inst.get('port')}")


def _item_ranges(text):
    """(строка «coop_extra_instances:», строки файла, [[первая, после
    последней], …] — строки каждого элемента списка); None — списка нет."""
    match = KEY_RE.search(text)
    if not match:
        return None
    lines = text.splitlines(keepends=True)
    start = text.count("\n", 0, match.start())
    # Строки элементов: «- …» начинает элемент, дальше — его строки до
    # следующего «- » того же отступа или конца блока.
    items, current, indent = [], None, None
    end = start + 1
    while end < len(lines):
        line = lines[end]
        stripped = line.strip()
        at_column0 = stripped and not line[0].isspace() and not line.startswith("-")
        if at_column0 and not stripped.startswith("#"):
            break
        if stripped.startswith("- ") and not at_column0:
            line_indent = line[: len(line) - len(line.lstrip())]
            if indent is None:
                indent = line_indent
            if line_indent == indent:
                current = [end, end + 1]
                items.append(current)
                end += 1
                continue
        if current is not None and stripped and not at_column0:
            current[1] = end + 1
        end += 1
    return start, lines, items


def _first_matching(candidates, expected):
    for candidate in candidates:
        try:
            parsed = yaml.safe_load(candidate) or {}
        except yaml.YAMLError:
            continue
        if (parsed.get("coop_extra_instances") or []) == expected:
            return candidate
    return None


def remove_item(text, name, existing):
    """Текст vars.yml без элемента name; None — не удалось вырезать."""
    found = _item_ranges(text)
    if found is None:
        return None
    start, lines, items = found
    expected = [i for i in existing if str(i.get("name")) != name]

    def candidates():
        for first, last in items:
            candidate_lines = lines[:first] + lines[last:]
            if len(expected) == 0:
                candidate_lines[start] = "coop_extra_instances: []\n"
            yield "".join(candidate_lines)

    return _first_matching(candidates(), expected)


def replace_item(text, name, existing, item_lines):
    """Текст vars.yml, где элемент name заменён на item_lines (с тем же
    отступом); None — не удалось. Комментарии внутри элемента пропадают."""
    found = _item_ranges(text)
    if found is None:
        return None
    _, lines, items = found
    new_item = yaml.safe_load("\n".join(item_lines))[0]
    expected = [new_item if str(i.get("name")) == name else i for i in existing]

    def candidates():
        for first, last in items:
            indent = lines[first][: len(lines[first]) - len(lines[first].lstrip())]
            yield "".join(lines[:first] + [f"{indent}{line}\n" for line in item_lines] + lines[last:])

    return _first_matching(candidates(), expected)


def cmd_remove(vars_path, name):
    text, config, existing = load_vars(vars_path)
    if name not in [str(i.get("name")) for i in existing]:
        print(f"{name} в coop_extra_instances нет — vars.yml не изменён")
        return
    expected = [i for i in existing if str(i.get("name")) != name]
    new_text = remove_item(text, name, existing)
    if new_text is None:
        die(f"не удалось вырезать {name} из vars.yml — удалите его запись из coop_extra_instances вручную")
    write_vars(vars_path, config, new_text, expected)
    print(f"{name} убран из coop_extra_instances в {vars_path}")


# --- запись кооператива --------------------------------------------------------

# Порядок полей в vars.yml; обязательные — всегда, остальные — если заданы.
FIELD_ORDER = (
    "name", "domain", "aliases", "port", "secret_key", "bank_api_encryption_key",
    "recaptcha_site_key", "recaptcha_secret_key", "cron_enable", "cron_disable",
    "cron_minute_offset", "backup_agent_pubkeys", "rclone_yandex_token",
    "restore_db_file", "restore_source_instance_dir", "env_extra",
)
PLAIN = ("name", "domain", "port", "cron_minute_offset")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PUBKEY_RE = re.compile(r"^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp\d+|sk-\S+) [A-Za-z0-9+/=]+( .*)?$")


def _q(value):
    """Строка в YAML: JSON-строка — корректная строка YAML в двойных кавычках."""
    return json.dumps(str(value), ensure_ascii=False)


def item_lines(item):
    """Элемент coop_extra_instances строками YAML (первая — «- name: …»);
    пустые необязательные поля не пишутся."""
    lines = []
    for key in FIELD_ORDER:
        if key not in item:
            continue
        value = item[key]
        if key not in ("name", "domain", "port", "secret_key", "bank_api_encryption_key") and value in ("", [], {}, None):
            continue
        if key in PLAIN:
            text = str(value)
        elif key in ("aliases", "cron_enable", "cron_disable"):
            text = "[" + ", ".join(str(v) for v in value) + "]"
        elif key == "backup_agent_pubkeys":
            lines.append(f"{key}:")
            lines += [f"  - {_q(v)}" for v in value]
            continue
        elif key == "env_extra":
            lines.append(f"{key}:")
            lines += [f"  {k}: {_q(v)}" for k, v in value.items()]
            continue
        else:
            text = _q(value)
        lines.append(f"{key}: {text}")
    return ["- " + lines[0]] + ["  " + line for line in lines[1:]]


def cron_lists(jobs, enabled):
    """(cron_enable, cron_disable) — отличия набора enabled от включённых по
    умолчанию задач jobs (cron_jobs без retired)."""
    default_on = {j["name"] for j in jobs if j.get("enabled", True)}
    names = [j["name"] for j in jobs]
    return ([n for n in names if n in enabled and n not in default_on],
            [n for n in names if n not in enabled and n in default_on])


def cron_enabled(jobs, item):
    return {j["name"] for j in jobs
            if (j.get("enabled", True) or j["name"] in item.get("cron_enable", []))
            and j["name"] not in item.get("cron_disable", [])}


def _filter_module():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ansible", "filter_plugins", "coop.py")
    spec = importlib.util.spec_from_file_location("coop_filters", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _show_domain(host):
    try:
        return host.encode("ascii").decode("idna") if "xn--" in host else host
    except UnicodeError:
        return host


def _mask(value):
    return "задан" if value else "—"


def check_dns(hosts, inventory):
    """DNS: без A-записи на сервер certbot не выдаст сертификат (сайт будет по HTTP)."""
    server = inventory_host(inventory)
    server_ips = resolve(server) if server else []
    for host in hosts:
        ips = resolve(host)
        shown = _show_domain(host)
        if not ips:
            print(f"  ! {shown}: A-записи нет — сайт заработает по HTTP, HTTPS после следующего прогона")
        elif server_ips and not set(ips) & set(server_ips):
            print(f"  ! {shown} → {', '.join(ips)}, а сервер — {', '.join(server_ips)}: проверьте A-запись")
        else:
            print(f"  {shown} → {', '.join(ips)}")


# «да»: y/yes; «н» — та же клавиша Y в русской раскладке; д/да — по-русски.
YES = ("y", "yes", "н", "д", "да")


def _yes(prompt, default="y"):
    return ask(f"{prompt} (y/n)", default).lower() in YES


class Editor:
    """Меню настроек одного кооператива. item — его запись (меняется на
    месте), taken — что занято основным и остальными кооперативами."""

    def __init__(self, item, index, taken, jobs, is_new):
        self.item, self.index, self.taken, self.jobs, self.is_new = item, index, taken, jobs, is_new

    # --- ввод ---

    def ask_domain(self, prompt, default="", allow_empty=False):
        while True:
            raw = ask(prompt, _show_domain(default) if default else "")
            if not raw and allow_empty:
                return ""
            try:
                domain = to_punycode(raw)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            if not domain or "." not in domain or not DOMAIN_RE.match(domain):
                print("  Нужен домен вида пример.рф или example.ru.")
            elif domain in self.taken["domains"]:
                print(f"  {domain} уже используется другим кооперативом.")
            else:
                return domain

    def ask_name(self):
        while True:
            name = ask("Короткое имя латиницей (пользователь ОС, служба garage-coop-ИМЯ), например coop2")
            if not NAME_RE.match(name):
                print("  Только строчная латиница, цифры и «-», с буквы, до 31 символа.")
            elif name in self.taken["names"]:
                print(f"  Имя {name} уже занято.")
            else:
                self.item["name"] = name
                return

    def edit_domain(self):
        self.item["domain"] = self.ask_domain("Домен сайта (можно кириллицей: пример.рф)", self.item.get("domain", ""))
        aliases = [a for a in self.item.get("aliases", []) if a != self.item["domain"]]
        self.item["aliases"] = aliases

    def edit_aliases(self):
        print("Ещё домены этого кооператива: они в том же сертификате и редиректят на основной.")
        aliases = list(self.item.get("aliases", []))
        while True:
            if aliases:
                for n, alias in enumerate(aliases, 1):
                    print(f"  {n}) {_show_domain(alias)}")
            else:
                print("  (нет)")
            raw = ask("Новый домен, номер — убрать, Enter — готово")
            if not raw:
                break
            if raw.isdigit() and 1 <= int(raw) <= len(aliases):
                aliases.pop(int(raw) - 1)
                continue
            try:
                domain = to_punycode(raw)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            if not domain or "." not in domain or not DOMAIN_RE.match(domain):
                print("  Нужен домен вида пример.рф или example.ru.")
            elif domain in self.taken["domains"] or domain == self.item.get("domain") or domain in aliases:
                print(f"  {domain} уже используется.")
            else:
                aliases.append(domain)
        self.item["aliases"] = aliases

    def edit_port(self):
        default = self.item.get("port") or max(self.taken["ports"]) + 1
        while True:
            raw = ask("Порт gunicorn", str(default))
            if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
                print("  Число 1024…65535.")
            elif int(raw) in self.taken["ports"]:
                print(f"  Порт {raw} уже занят.")
            else:
                self.item["port"] = int(raw)
                return

    def _ask_value(self, key, prompt, secret=False):
        current = self.item.get(key, "")
        shown = ("задан" if secret else current) if current else "—"
        raw = ask(f"{prompt} (сейчас: {shown}; Enter — оставить, «-» — убрать)")
        if raw == "-":
            self.item[key] = ""
        elif raw:
            self.item[key] = raw

    def edit_recaptcha(self):
        print("Ключи reCAPTCHA v2 выпускаются на домен: google.com/recaptcha/admin,")
        print("в список доменов — домен этого кооператива (и дополнительные).")
        self._ask_value("recaptcha_site_key", "Ключ сайта (site key)")
        self._ask_value("recaptcha_secret_key", "Секретный ключ (secret key)", secret=True)
        if bool(self.item.get("recaptcha_site_key")) != bool(self.item.get("recaptcha_secret_key")):
            print("  ! Задан только один из ключей — reCAPTCHA работать не будет.")

    def edit_cron(self):
        enabled = cron_enabled(self.jobs, self.item)
        while True:
            print("Задачи cron этого кооператива (у основного — свои):")
            for n, job in enumerate(self.jobs, 1):
                mark = "x" if job["name"] in enabled else " "
                default = "" if (job["name"] in enabled) == job.get("enabled", True) else \
                    ("  (по умолчанию вкл.)" if job.get("enabled", True) else "  (по умолчанию выкл.)")
                when = " ".join(str(job.get(k, "*")) for k in ("minute", "hour", "day", "month", "weekday"))
                print(f"  {n:2}) [{mark}] {job['name']:<28} {when}{default}")
            raw = ask("Номера через пробел — переключить, Enter — готово")
            if not raw:
                break
            for part in raw.replace(",", " ").split():
                if part.isdigit() and 1 <= int(part) <= len(self.jobs):
                    enabled ^= {self.jobs[int(part) - 1]["name"]}
                else:
                    print(f"  Нет задачи {part}.")
        self.item["cron_enable"], self.item["cron_disable"] = cron_lists(self.jobs, enabled)

    def edit_offset(self):
        auto = 7 * self.index % 60
        current = self.item.get("cron_minute_offset")
        print("Сдвиг минут cron — чтобы скрипты кооперативов не стартовали в одну минуту.")
        raw = ask(f"Минут 0…59 (сейчас: {auto if current is None else current}"
                  f"{' — авто' if current is None else ''}; «-» — авто {auto})")
        if raw == "-":
            self.item.pop("cron_minute_offset", None)
        elif raw.isdigit() and int(raw) < 60:
            self.item["cron_minute_offset"] = int(raw)
        elif raw:
            print("  Число 0…59.")

    def edit_pubkeys(self):
        print("Открытые ключи агента бэкапа председателя этого кооператива")
        print("(agents/backup/README.md): агент забирает копии БД и файлов к себе.")
        keys = list(self.item.get("backup_agent_pubkeys", []))
        while True:
            for n, key in enumerate(keys, 1):
                parts = key.split()
                print(f"  {n}) {parts[0]} …{parts[1][-12:]} {' '.join(parts[2:])}")
            if not keys:
                print("  (нет)")
            raw = ask("Вставьте ключ («ssh-ed25519 AAAA… имя»), номер — убрать, Enter — готово")
            if not raw:
                break
            if raw.isdigit() and 1 <= int(raw) <= len(keys):
                keys.pop(int(raw) - 1)
            elif PUBKEY_RE.match(raw):
                keys.append(raw)
            else:
                print("  Не похоже на открытый ключ SSH (строка из файла .pub).")
        self.item["backup_agent_pubkeys"] = keys

    def edit_rclone(self):
        print("Копии на Яндекс.Диск: вывод `rclone authorize \"yandex\"` (JSON одной строкой).")
        raw = ask(f"Токен (сейчас: {_mask(self.item.get('rclone_yandex_token'))}; Enter — оставить, «-» — убрать)")
        if raw == "-":
            self.item["rclone_yandex_token"] = ""
        elif raw:
            try:
                token = json.loads(raw)
            except ValueError:
                print("  Это не JSON — токен не изменён.")
                return
            if not isinstance(token, dict) or "access_token" not in token:
                print("  В JSON нет access_token — токен не изменён.")
                return
            self.item["rclone_yandex_token"] = json.dumps(token, ensure_ascii=False, separators=(",", ":"))

    def edit_restore(self):
        print("Перенос БД и файлов с этого компьютера — только при первом прогоне:")
        print("если БД на сервере уже есть, она не трогается. Пусто — новая БД (seed.py).")
        while True:
            raw = ask(f"Файл coop.db (сейчас: {self.item.get('restore_db_file') or '—'}; Enter — оставить, «-» — убрать)")
            if raw == "-":
                self.item["restore_db_file"] = ""
                self.item["restore_source_instance_dir"] = ""
                return
            if not raw:
                break
            path = os.path.abspath(os.path.expanduser(raw))
            if os.path.isfile(path):
                self.item["restore_db_file"] = path
                break
            print(f"  Нет файла {path}.")
        if not self.item.get("restore_db_file"):
            return
        default = self.item.get("restore_source_instance_dir") or os.path.dirname(self.item["restore_db_file"])
        while True:
            raw = ask("Каталог instance/ с файлами (uploads, bank_certs, …; «-» — без файлов)", default)
            if raw == "-":
                self.item["restore_source_instance_dir"] = ""
                return
            path = os.path.abspath(os.path.expanduser(raw))
            if os.path.isdir(path):
                self.item["restore_source_instance_dir"] = path
                return
            print(f"  Нет каталога {path}.")

    def edit_env(self):
        print("Дополнительные строки .env этого кооператива (как app_env_extra).")
        env = dict(self.item.get("env_extra", {}))
        while True:
            for key, value in env.items():
                print(f"  {key}={value}")
            if not env:
                print("  (нет)")
            raw = ask("КЛЮЧ=значение — задать, «КЛЮЧ=» — убрать, Enter — готово")
            if not raw:
                break
            key, sep, value = raw.partition("=")
            key = key.strip()
            if not sep or not ENV_KEY_RE.match(key):
                print("  Нужно КЛЮЧ=значение, ключ — латиница в верхнем регистре, цифры и «_».")
            elif value:
                env[key] = value
            else:
                env.pop(key, None)
        self.item["env_extra"] = env

    # --- меню ---

    def entries(self):
        item = self.item
        on = cron_enabled(self.jobs, item)
        changed = item.get("cron_enable", []) + [f"−{n}" for n in item.get("cron_disable", [])]
        offset = item.get("cron_minute_offset")
        return [
            ("Домен", _show_domain(item.get("domain", "")), self.edit_domain),
            ("Дополнительные домены", ", ".join(_show_domain(a) for a in item.get("aliases", [])) or "—",
             self.edit_aliases),
            ("Порт", str(item.get("port", "")), self.edit_port),
            ("reCAPTCHA", "задана" if item.get("recaptcha_site_key") else "—", self.edit_recaptcha),
            ("Задачи cron", f"{len(on)} из {len(self.jobs)}" + (f" ({', '.join(changed)})" if changed else
                                                              " (по умолчанию)"), self.edit_cron),
            ("Сдвиг минут cron", f"{7 * self.index % 60} (авто)" if offset is None else str(offset),
             self.edit_offset),
            ("Агент бэкапа председателя", f"ключей: {len(item.get('backup_agent_pubkeys', []))}",
             self.edit_pubkeys),
            ("Копии на Яндекс.Диск", _mask(item.get("rclone_yandex_token")), self.edit_rclone),
            ("Перенос БД с этого компьютера", item.get("restore_db_file") or "— (новая БД)", self.edit_restore),
            ("Доп. переменные .env", ", ".join(item.get("env_extra", {})) or "—", self.edit_env),
        ]

    def run(self, validate):
        """True — сохранить, False — выйти без сохранения."""
        while True:
            entries = self.entries()
            title = "Новый кооператив" if self.is_new else "Кооператив"
            print(f"\n{title} {self.item['name']}:")
            for n, (label, value, _) in enumerate(entries, 1):
                print(f"  {n:2}) {label:<30} {value}")
            print("   s) Сохранить   q) Выйти без сохранения")
            choice = ask("Пункт").lower()
            if choice in ("s", "ы", "с"):
                error = validate(self.item)
                if error:
                    print(f"  ! {error}")
                    continue
                return True
            if choice in ("q", "й", "в"):
                if _yes("Выйти без сохранения?", "n"):
                    return False
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(entries):
                print()
                entries[int(choice) - 1][2]()
            else:
                print("  Нет такого пункта.")


def cmd_add(vars_path, defaults_path, inventory):
    with open(defaults_path, encoding="utf-8") as f:
        defaults = yaml.safe_load(f) or {}
    text, config, existing = load_vars(vars_path)

    def setting(key):
        return config.get(key, defaults.get(key))

    app_user = str(setting("app_user") or "coop")
    app_port = int(setting("app_port") or 8000)
    service_name = str(setting("service_name") or "garage-coop")
    all_jobs = setting("cron_jobs") or []
    jobs = [j for j in all_jobs if not j.get("retired")]
    if not config.get("certbot_email"):
        die("в vars.yml нет certbot_email — без него сертификат новому домену не выпустить")

    print(f"Файл настроек: {vars_path}")
    target = None
    if existing:
        print("\n   0) Добавить новый кооператив")
        for n, inst in enumerate(existing, 1):
            print(f"  {n:2}) Изменить {inst.get('name')} ({_show_domain(str(inst.get('domain')))}, порт {inst.get('port')})")
        while True:
            raw = ask("Пункт", "0")
            if raw.isdigit() and int(raw) <= len(existing):
                break
            print("  Нет такого пункта.")
        if int(raw):
            target = int(raw) - 1

    others = [i for n, i in enumerate(existing) if n != target]
    taken = {
        "names": {app_user} | {str(i.get("name")) for i in others},
        "ports": {app_port} | {int(i["port"]) for i in others if str(i.get("port", "")).isdigit()},
        "domains": {config.get("coop_domain")} | set(config.get("coop_domain_aliases") or []),
    }
    for inst in others:
        taken["domains"] |= {inst.get("domain")} | set(inst.get("aliases") or [])
    taken["domains"] -= {None, ""}

    if target is None:
        item = {"secret_key": secrets.token_hex(32), "bank_api_encryption_key": secrets.token_hex(32)}
        index = len(existing) + 1
        editor = Editor(item, index, taken, jobs, is_new=True)
        print()
        editor.ask_name()
        editor.edit_domain()
        editor.edit_aliases()
        editor.edit_port()
    else:
        item = dict(existing[target])
        index = target + 1
        editor = Editor(item, index, taken, jobs, is_new=False)
        print(f"Имя {item.get('name')} не меняется: для другого имени — remove-coop.sh и новый кооператив.")

    primary = {
        "app_user": app_user, "app_port": app_port, "coop_domain": config.get("coop_domain") or "",
        "coop_domain_aliases": config.get("coop_domain_aliases") or [],
        "app_secret_key": setting("app_secret_key"), "service_name": service_name, "cron_jobs": all_jobs,
    }
    filters = _filter_module()

    def candidate_list(candidate):
        new = yaml.safe_load("\n".join(item_lines(candidate)))[0]
        result = list(existing)
        if target is None:
            result.append(new)
        else:
            result[target] = new
        return result

    def validate(candidate):
        try:
            filters.coop_instances(candidate_list(candidate), primary)
        except Exception as exc:  # AnsibleFilterError
            return str(exc)
        return None

    if not editor.run(validate):
        die("отменено, vars.yml не изменён")

    lines = item_lines(item)
    expected = candidate_list(item)
    print()
    check_dns([item["domain"]] + item.get("aliases", []), inventory)
    print("\nБудет записано в coop_extra_instances:")
    for line in lines:
        key = line.lstrip("- ").split(":")[0]
        hidden = key in ("secret_key", "bank_api_encryption_key", "recaptcha_secret_key", "rclone_yandex_token")
        print("  " + (line.split(":")[0] + ": <скрыто>" if hidden else line))
    if not _yes("\nЗаписать?"):
        die("отменено, vars.yml не изменён")

    if target is None:
        new_text = insert_item(text, lines)
    else:
        new_text = replace_item(text, item["name"], existing, lines)
        if new_text is None:
            die(f"не удалось заменить {item['name']} в vars.yml — файл не изменён; поправьте его вручную")
    write_vars(vars_path, config, new_text, expected)

    print(f"\nЗаписано в {vars_path}.")
    if target is None:
        print("Ключи — только в этом файле: при переносе на другой компьютер берите его")
        print("целиком, иначе секреты в БД кооператива не расшифруются.")
    print(f"Служба: {service_name}-{item['name']}, сайт: https://{_show_domain(item['domain'])}")


def main():
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == "add":
        cmd_add(args[1], args[2], args[3] if len(args) > 3 else "")
    elif len(args) == 2 and args[0] == "list":
        cmd_list(args[1])
    elif len(args) == 3 and args[0] == "remove":
        cmd_remove(args[1], args[2])
    else:
        die("usage: coop_vars.py add VARS DEFAULTS [INVENTORY] | list VARS | remove VARS NAME")


if __name__ == "__main__":
    main()
