#!/usr/bin/env python3
"""
Сторонние библиотеки фронтенда (app/static/vendor/) — проверка обновлений и
уязвимостей, обновление, восстановление и сверка файлов.

Сборщика (npm/webpack) в проекте нет намеренно: файлы берутся как есть из
официальных пакетов реестра npm. Что именно взято, лежит в манифесте
app/static/vendor/vendor.json: версия, контрольная сумма пакета (integrity
из реестра — sha512 tar-архива), какие файлы из него нужны и sha256 каждого
скопированного файла. Карты исходников (*.map) копируются автоматически —
для каждого файла, где есть sourceMappingURL, если карта есть в пакете.
Шаблоны берут пути через vendor_url() (app/vendor.py), поэтому версию после
обновления нигде, кроме манифеста, менять не нужно.

Команды (запуск из корня проекта, нужен только Python 3, без pip-пакетов):

    python3 scripts/vendor.py check
        Текущие версии, последняя в пределах той же старшей версии (её ставит
        update), самая последняя вообще и известные уязвимости текущих версий
        (база реестра npm). Код выхода 2, если уязвимости есть — удобно для CI.

    python3 scripts/vendor.py update [пакет ...] [--to ВЕРСИЯ] [--major]
        Обновить пакеты (по умолчанию — все) до последней версии той же
        старшей версии; --major — до самой последней (могут быть несовместимые
        изменения, проверьте страницы, где пакет используется — они перечислены
        в манифесте), --to — до конкретной (только с одним пакетом). Старая
        папка удаляется, манифест переписывается.

    python3 scripts/vendor.py verify
        Без сети: все файлы из манифеста на месте и совпадают по sha256 —
        никто не правил их руками. Это же проверяет tests/test_vendor_static.py.

    python3 scripts/vendor.py install
        Скачать заново то, чего не хватает или что не совпадает, строго тех
        версий, что в манифесте, со сверкой integrity и с реестром, и с
        записанной в манифесте (пакет не подменили в реестре задним числом).

После update: прогнать тесты и открыть страницы из поля "used_on" манифеста.
"""
import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import posixpath
import re
import shutil
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = "https://registry.npmjs.org"
ADVISORIES_URL = f"{REGISTRY}/-/npm/v1/security/advisories/bulk"
TIMEOUT = 60

# app/vendor.py грузим файлом, а не `import app.vendor`: иначе выполнился бы
# app/__init__.py со всей конфигурацией приложения, скрипту она не нужна.
_spec = importlib.util.spec_from_file_location("app_vendor", os.path.join(ROOT, "app", "vendor.py"))
app_vendor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app_vendor)
VENDOR_DIR = app_vendor.VENDOR_DIR
MANIFEST_PATH = app_vendor.MANIFEST_PATH
package_dir = app_vendor.package_dir

SOURCE_MAP_RE = re.compile(rb"sourceMappingURL=([^\s*'\"]+)")


# ---------------------------------------------------------------------------
# Реестр npm
# ---------------------------------------------------------------------------

def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "garage-coop-vendor-script"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "garage-coop-vendor-script"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _version_key(version: str):
    return [int(part) for part in version.split(".")]


def _stable_versions(name: str) -> list[str]:
    metadata = json.loads(_get(f"{REGISTRY}/{name}"))
    versions = [v for v in metadata["versions"] if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    return sorted(versions, key=_version_key)


def _latest(versions: list[str], same_major_as: str | None = None) -> str | None:
    if same_major_as is not None:
        major = same_major_as.split(".")[0]
        versions = [v for v in versions if v.split(".")[0] == major]
    return versions[-1] if versions else None


def _download(name: str, version: str, expected_integrity: str | None = None) -> tuple[bytes, str]:
    """Скачивает tar-архив пакета и проверяет его sha512 против integrity из
    реестра (и против записанного в манифесте, если передан). Возвращает
    (архив, integrity)."""
    metadata = json.loads(_get(f"{REGISTRY}/{name}/{version}"))
    integrity = metadata["dist"]["integrity"]
    if expected_integrity is not None and integrity != expected_integrity:
        raise SystemExit(
            f"{name}@{version}: integrity в реестре не совпадает с записанной в манифесте — "
            f"пакет мог быть подменён. В реестре: {integrity}"
        )
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512":
        raise SystemExit(f"{name}@{version}: неожиданный алгоритм integrity {algorithm}")
    tarball = _get(metadata["dist"]["tarball"])
    actual = base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    if actual != expected:
        raise SystemExit(f"{name}@{version}: контрольная сумма архива не совпадает с реестром — не устанавливаю")
    return tarball, integrity


# ---------------------------------------------------------------------------
# Файлы пакета
# ---------------------------------------------------------------------------

def _read_members(tarball: bytes) -> dict[str, bytes]:
    """Содержимое архива в памяти, без распаковки на диск — путь файла внутри
    архива никогда не становится путём в файловой системе (никакого
    «../» за пределы папки). Префикс package/ отрезается."""
    members = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            path = member.name.split("/", 1)[1] if "/" in member.name else member.name
            members[posixpath.normpath(path)] = archive.extractfile(member).read()
    return members


def _safe_dest(dest_dir: str, relative: str) -> str:
    target = os.path.normpath(os.path.join(dest_dir, relative))
    if not target.startswith(os.path.normpath(dest_dir) + os.sep):
        raise SystemExit(f"Недопустимый путь назначения в манифесте: {relative}")
    return target


def _install_files(name: str, package: dict, tarball: bytes, dest_dir: str) -> dict[str, str]:
    """Копирует нужные файлы (+ карты исходников, + лицензию) в dest_dir.
    Возвращает {путь в папке: sha256}."""
    members = _read_members(tarball)
    wanted: dict[str, str] = dict(package["files"])  # куда → откуда
    wanted[os.path.basename(package["license"])] = package["license"]

    for dest, source in list(package["files"].items()):
        content = members.get(posixpath.normpath(source))
        if content is None:
            raise SystemExit(f"{name}: в пакете нет файла {source}")
        match = SOURCE_MAP_RE.search(content[-2000:])
        if match and b":" not in match.group(1):  # не URL и не data:-карта, встроенная в сам файл
            map_name = match.group(1).decode()
            map_source = posixpath.normpath(posixpath.join(posixpath.dirname(source), map_name))
            if map_source in members:
                wanted.setdefault(posixpath.normpath(posixpath.join(posixpath.dirname(dest), map_name)), map_source)

    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    hashes = {}
    for dest, source in sorted(wanted.items()):
        content = members.get(posixpath.normpath(source))
        if content is None:
            raise SystemExit(f"{name}: в пакете нет файла {source}")
        target = _safe_dest(dest_dir, dest)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(content)
        hashes[dest] = hashlib.sha256(content).hexdigest()
    return hashes


def _file_problems(name: str, package: dict) -> list[str]:
    folder = os.path.join(VENDOR_DIR, package_dir(name, package["version"]))
    problems = []
    for relative, expected in package["sha256"].items():
        path = os.path.join(folder, relative)
        if not os.path.isfile(path):
            problems.append(f"нет файла {relative}")
            continue
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != expected:
                problems.append(f"файл изменён: {relative}")
    return problems


# ---------------------------------------------------------------------------
# Манифест
# ---------------------------------------------------------------------------

def _load() -> dict:
    return app_vendor.load_manifest(MANIFEST_PATH)


def _save(manifest: dict) -> None:
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _stale_dirs(manifest: dict) -> list[str]:
    expected = {package_dir(n, p["version"]) for n, p in manifest["packages"].items()}
    return sorted(
        d for d in os.listdir(VENDOR_DIR)
        if os.path.isdir(os.path.join(VENDOR_DIR, d)) and d not in expected
    )


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def cmd_check(args) -> int:
    manifest = _load()
    packages = manifest["packages"]
    print(f"{'пакет':28} {'сейчас':10} {'в пределах':12} {'последняя':10}")
    for name, package in packages.items():
        versions = _stable_versions(name)
        current = package["version"]
        same_major = _latest(versions, same_major_as=current)
        newest = _latest(versions)
        mark_minor = "" if same_major == current else "  ← update"
        mark_major = "" if newest == same_major else "  ← update --major"
        print(f"{name:28} {current:10} {same_major:12} {newest:10}{mark_minor or mark_major}")

    advisories = _post_json(ADVISORIES_URL, {n: [p["version"]] for n, p in packages.items()})
    if not advisories:
        print("\nИзвестных уязвимостей у текущих версий нет.")
        return 0
    print("\nИЗВЕСТНЫЕ УЯЗВИМОСТИ текущих версий:")
    for name, items in advisories.items():
        for item in items:
            print(f"  {name}@{packages[name]['version']}: [{item.get('severity')}] {item.get('title')}")
            print(f"    уязвимы {item.get('vulnerable_versions')}; {item.get('url')}")
    return 2


def cmd_update(args) -> int:
    manifest = _load()
    packages = manifest["packages"]
    names = args.packages or list(packages)
    unknown = [n for n in names if n not in packages]
    if unknown:
        raise SystemExit(f"Нет в манифесте: {', '.join(unknown)} (новый пакет добавьте в vendor.json вручную)")
    if args.to and len(names) != 1:
        raise SystemExit("--to — только вместе с одним пакетом")

    changed = []
    for name in names:
        package = packages[name]
        current = package["version"]
        if args.to:
            target = args.to
        else:
            versions = _stable_versions(name)
            target = _latest(versions) if args.major else _latest(versions, same_major_as=current)
        # Пакет, только что вписанный в манифест руками (sha256 ещё пуст), ставим
        # и при совпадении версии — иначе «update <пакет> --to <версия>» для
        # нового пакета ничего бы не сделал.
        if target == current and package.get("sha256") and not _file_problems(name, package):
            print(f"{name}: {current} — уже актуальна")
            continue
        tarball, integrity = _download(name, target)
        new_dir = os.path.join(VENDOR_DIR, package_dir(name, target))
        hashes = _install_files(name, package, tarball, new_dir)
        old_dir = os.path.join(VENDOR_DIR, package_dir(name, current))
        if old_dir != new_dir and os.path.isdir(old_dir):
            shutil.rmtree(old_dir)
        package.update(version=target, integrity=integrity, sha256=hashes)
        _save(manifest)
        changed.append(name)
        print(f"{name}: {current} → {target}" if target != current else f"{name}: {target} установлен")

    if changed:
        print("\nДальше: прогнать тесты и открыть страницы, где используются обновлённые пакеты:")
        for name in changed:
            print(f"  {name}: {packages[name]['used_on']}")
    return 0


def cmd_verify(args) -> int:
    manifest = _load()
    failed = False
    for name, package in manifest["packages"].items():
        problems = _file_problems(name, package)
        if problems:
            failed = True
            print(f"{name}@{package['version']}: " + "; ".join(problems))
    stale = _stale_dirs(manifest)
    if stale:
        failed = True
        print("Лишние папки (нет в манифесте): " + ", ".join(stale))
    if not failed:
        print("Все файлы на месте и совпадают с манифестом.")
    return 1 if failed else 0


def cmd_install(args) -> int:
    manifest = _load()
    for name, package in manifest["packages"].items():
        if not _file_problems(name, package):
            continue
        tarball, _ = _download(name, package["version"], expected_integrity=package["integrity"])
        hashes = _install_files(name, package, tarball, os.path.join(VENDOR_DIR, package_dir(name, package["version"])))
        if hashes != package["sha256"]:
            raise SystemExit(f"{name}: файлы из пакета не совпадают с манифестом по sha256")
        print(f"{name}@{package['version']}: восстановлен")
    return cmd_verify(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="версии и уязвимости").set_defaults(func=cmd_check)
    update = sub.add_parser("update", help="обновить пакеты")
    update.add_argument("packages", nargs="*")
    update.add_argument("--to", help="конкретная версия (с одним пакетом)")
    update.add_argument("--major", action="store_true", help="разрешить переход на новую старшую версию")
    update.set_defaults(func=cmd_update)
    sub.add_parser("verify", help="сверить файлы с манифестом, без сети").set_defaults(func=cmd_verify)
    sub.add_parser("install", help="восстановить недостающие файлы").set_defaults(func=cmd_install)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
