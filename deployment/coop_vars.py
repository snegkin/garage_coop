#!/usr/bin/env python3
"""Список coop_extra_instances в vars.yml — для add-coop.sh и remove-coop.sh.

    coop_vars.py add VARS_FILE DEFAULTS_FILE [INVENTORY_FILE]
        спросить имя, домен (кириллицу — в punycode), алиасы и порт,
        сгенерировать ключи, проверить DNS и дописать кооператив в конец списка
    coop_vars.py list VARS_FILE
        «имя<TAB>домен<TAB>порт» — по строке на кооператив
    coop_vars.py remove VARS_FILE NAME
        убрать кооператив из списка

Остальной vars.yml с комментариями не трогается: элемент вставляется или
вырезается текстом, затем файл перечитывается и сравнивается с ожидаемым —
не совпало, файл не меняется.
"""
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


def remove_item(text, name, existing):
    """Текст vars.yml без элемента name; None — не удалось вырезать."""
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
    expected = [i for i in existing if str(i.get("name")) != name]
    for first, last in items:
        candidate_lines = lines[:first] + lines[last:]
        if len(expected) == 0:
            candidate_lines[start] = "coop_extra_instances: []\n"
        candidate = "".join(candidate_lines)
        try:
            parsed = yaml.safe_load(candidate) or {}
        except yaml.YAMLError:
            continue
        if (parsed.get("coop_extra_instances") or []) == expected:
            return candidate
    return None


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


def cmd_add(vars_path, defaults_path, inventory):
    with open(defaults_path, encoding="utf-8") as f:
        defaults = yaml.safe_load(f) or {}
    text, config, existing = load_vars(vars_path)

    def setting(key):
        return config.get(key, defaults.get(key))

    app_user = str(setting("app_user") or "coop")
    app_port = int(setting("app_port") or 8000)
    service_name = str(setting("service_name") or "garage-coop")
    if not config.get("certbot_email"):
        die("в vars.yml нет certbot_email — без него сертификат новому домену не выпустить")

    taken_names = {app_user} | {str(i.get("name")) for i in existing}
    taken_ports = {app_port} | {int(i["port"]) for i in existing if str(i.get("port", "")).isdigit()}
    taken_domains = {config.get("coop_domain")} | set(config.get("coop_domain_aliases") or [])
    for inst in existing:
        taken_domains |= {inst.get("domain")} | set(inst.get("aliases") or [])
    taken_domains.discard(None)
    taken_domains.discard("")

    print(f"Файл настроек: {vars_path}")
    if existing:
        print("Уже есть: " + ", ".join(f"{i.get('name')} ({i.get('domain')}, порт {i.get('port')})" for i in existing))
    print()

    while True:
        name = ask("Короткое имя латиницей (пользователь ОС, служба garage-coop-ИМЯ), например coop2")
        if not NAME_RE.match(name):
            print("  Только строчная латиница, цифры и «-», с буквы, до 31 символа.")
        elif name in taken_names:
            print(f"  Имя {name} уже занято.")
        else:
            break

    def ask_domain(prompt, allow_empty=False):
        while True:
            raw = ask(prompt)
            if not raw and allow_empty:
                return ""
            try:
                domain = to_punycode(raw)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            if not domain or "." not in domain or not DOMAIN_RE.match(domain):
                print("  Нужен домен вида пример.рф или example.ru.")
            elif domain in taken_domains:
                print(f"  {domain} уже используется другим кооперативом.")
            else:
                return domain

    domain = ask_domain("Домен сайта (можно кириллицей: пример.рф)")
    taken_domains.add(domain)
    aliases = []
    while True:
        alias = ask_domain("Ещё домен — редирект на основной (Enter — больше нет)", allow_empty=True)
        if not alias:
            break
        aliases.append(alias)
        taken_domains.add(alias)

    free_port = max(taken_ports) + 1
    while True:
        raw = ask("Порт gunicorn", str(free_port))
        if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
            print("  Число 1024…65535.")
        elif int(raw) in taken_ports:
            print(f"  Порт {raw} уже занят.")
        else:
            port = int(raw)
            break

    # DNS: без A-записи на сервер certbot не выдаст сертификат (сайт будет по HTTP).
    server = inventory_host(inventory)
    server_ips = resolve(server) if server else []
    print()
    for host in [domain] + aliases:
        ips = resolve(host)
        shown = host.encode("ascii").decode("idna") if host.startswith("xn--") or ".xn--" in host else host
        if not ips:
            print(f"  ! {shown}: A-записи нет — сайт заработает по HTTP, HTTPS после следующего прогона")
        elif server_ips and not set(ips) & set(server_ips):
            print(f"  ! {shown} → {', '.join(ips)}, а сервер — {', '.join(server_ips)}: проверьте A-запись")
        else:
            print(f"  {shown} → {', '.join(ips)}")

    item = [
        f"- name: {name}",
        f"  domain: {domain}",
    ]
    if aliases:
        item.append(f"  aliases: [{', '.join(aliases)}]")
    item += [
        f"  port: {port}",
        f'  secret_key: "{secrets.token_hex(32)}"',
        f'  bank_api_encryption_key: "{secrets.token_hex(32)}"',
        "  # recaptcha_site_key / recaptcha_secret_key, cron_enable, backup_agent_pubkeys,",
        "  # restore_db_file — по желанию, см. defaults.yml (coop_extra_instances)",
    ]
    print("\nБудет добавлено в coop_extra_instances:")
    for line in item:
        print("  " + (line if "_key:" not in line else line.split(":")[0] + ': "<новый случайный ключ>"'))
    if ask("\nЗаписать? (д/н)", "д").lower() not in ("д", "да", "y", "yes"):
        die("отменено, vars.yml не изменён")

    new_text = insert_item(text, item)
    expected = existing + [yaml.safe_load("\n".join(item))[0]]
    write_vars(vars_path, config, new_text, expected)

    print(f"\nЗаписано в {vars_path}. Ключи — только в этом файле: при переносе на другой")
    print("компьютер берите его целиком, иначе секреты в БД кооператива не расшифруются.")
    print(f"Служба: {service_name}-{name}, сайт: https://{domain}")


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
