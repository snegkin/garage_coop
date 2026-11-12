#!/usr/bin/env python3
"""
Агент локального бэкапа: работает на компьютере председателя и раз в сутки
забирает с сервера кооператива свежую копию данных в локальный
restic-репозиторий с историей версий. Это третья копия, рядом с
instance/backups/ на самом сервере и внешним репозиторием
(scripts/backup_offsite.py).

Почему pull, а не push с сервера: компьютер за NAT, и у сервера нет
никакого доступа к этой копии. Если сервер взломают, отсюда ничего не
сотрут. Ключ ssh агента на сервере ограничен forced command'ом
scripts/backup_pull_gate.sh (rrsync -ro): только чтение и только каталог
instance/. С украденным ключом на сервере тоже ничего не сделать.

Проход:
  1. rsync backup_staging/ (снимок БД, файл окружения, MANIFEST.json —
     готовит scripts/backup_offsite.py по cron на сервере);
  2. MANIFEST: проверка свежести (старше max_age_hours — предупреждение:
     на сервере не работает cron) и список каталогов данных;
  3. rsync каждого каталога из MANIFEST (uploads/, bank_certs/, …) в
     зеркало — с --delete: зеркало повторяет сервер, история — в restic;
  4. restic backup зеркала → forget --prune по политике хранения;
  5. state_dir/status.json — итог прохода.

Команды:
  coop_backup_agent.py run        один проход (systemd: coop-backup.timer)
  coop_backup_agent.py snapshots  список снимков в локальном репозитории
  coop_backup_agent.py status     итог последнего прохода

Конфиг — /etc/coop-backup/coop_backup.ini (см. coop_backup.example.ini).
Восстановление — README.md рядом.
"""
import argparse
import configparser
import datetime as dt
import json
import os
import shlex
import subprocess
import sys

DEFAULT_CONFIG = "/etc/coop-backup/coop_backup.ini"
STAGING = "backup_staging"
RSYNC_TIMEOUT = 6 * 3600
RESTIC_TIMEOUT = 6 * 3600


def log(message: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


class AgentError(Exception):
    pass


class Config:
    def __init__(self, path: str):
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(path, encoding="utf-8"):
            raise AgentError(f"не найден конфиг {path}")
        try:
            server = parser["server"]
            local = parser["local"]
        except KeyError as exc:
            raise AgentError(f"в конфиге нет секции {exc}") from exc
        self.host = server.get("host", "").strip()
        self.user = server.get("user", "").strip()
        self.port = server.getint("port", 22)
        self.identity_file = server.get("identity_file", "").strip()
        self.known_hosts = server.get("known_hosts", "").strip()
        if not self.host or not self.user or not self.identity_file:
            raise AgentError("в [server] нужны host, user и identity_file")

        self.mirror_dir = local.get("mirror_dir", "/var/lib/coop-backup/mirror").strip()
        self.repository = local.get("repository", "").strip()
        self.password_file = local.get("password_file", "").strip()
        if not self.repository or not self.password_file:
            raise AgentError("в [local] нужны repository и password_file")
        self.state_dir = local.get("state_dir", "/var/lib/coop-backup").strip()
        self.keep_daily = local.getint("keep_daily", 14)
        self.keep_weekly = local.getint("keep_weekly", 8)
        self.keep_monthly = local.getint("keep_monthly", 12)
        self.keep_yearly = local.getint("keep_yearly", 5)
        self.max_age_hours = local.getint("max_age_hours", 36)
        self.restic = local.get("restic", "restic").strip()
        self.rsync = local.get("rsync", "rsync").strip()


def _ssh_command(cfg: Config) -> str:
    parts = ["ssh", "-p", str(cfg.port), "-i", cfg.identity_file, "-o", "BatchMode=yes",
             "-o", "IdentitiesOnly=yes", "-o", "ServerAliveInterval=30"]
    if cfg.known_hosts:
        parts += ["-o", f"UserKnownHostsFile={cfg.known_hosts}", "-o", "StrictHostKeyChecking=yes"]
    return shlex.join(parts)


def rsync_dir(cfg: Config, name: str) -> None:
    """Каталог name на сервере (относительно instance/, rrsync) →
    mirror_dir/name/."""
    target = os.path.join(cfg.mirror_dir, name)
    os.makedirs(target, exist_ok=True)
    argv = [cfg.rsync, "-a", "--delete", "--partial-dir=.rsync-partial", "-e", _ssh_command(cfg),
            f"{cfg.user}@{cfg.host}:{name}/", target + "/"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=RSYNC_TIMEOUT)
    except FileNotFoundError as exc:
        raise AgentError(f"не найден rsync ({cfg.rsync})") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"rsync {name}: не уложился в {RSYNC_TIMEOUT} с") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise AgentError(f"rsync {name} (код {result.returncode}){_rsync_hint(cfg, name, stderr)}: {stderr[-2000:]}")


def _rsync_hint(cfg: Config, name: str, stderr: str) -> str:
    """Понятная причина для частых ошибок первого запуска — перед сырым
    выводом rsync/ssh."""
    if "Host key verification failed" in stderr or "host key is known" in stderr:
        return (f" — в {cfg.known_hosts or '~/.ssh/known_hosts'} нет ключа сервера {cfg.host} "
                f"(или файл не читается пользователем агента): ssh-keyscan -p {cfg.port} {cfg.host} "
                f"| sudo tee {cfg.known_hosts or '/etc/coop-backup/known_hosts'}; домен — в punycode")
    if "No such file or directory" in stderr and name == STAGING:
        return (f" — на сервере нет instance/{STAGING}/: ни разу не запускался backup_offsite "
                f"(cron в 03:20 или вручную: scripts/backup_offsite.sh под пользователем сайта)")
    if "No such file or directory" in stderr:
        return f" — на сервере нет каталога instance/{name}/"
    return ""


def read_manifest(cfg: Config) -> dict:
    path = os.path.join(cfg.mirror_dir, STAGING, "MANIFEST.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise AgentError(f"MANIFEST.json не прочитать ({exc}) — на сервере ни разу не запускался backup_offsite") from exc


def manifest_age_hours(manifest: dict, now: dt.datetime | None = None) -> float | None:
    try:
        created = dt.datetime.fromisoformat(manifest["created_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return ((now or dt.datetime.now()) - created).total_seconds() / 3600


def _restic(cfg: Config, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, RESTIC_REPOSITORY=cfg.repository, RESTIC_PASSWORD_FILE=cfg.password_file)
    try:
        result = subprocess.run([cfg.restic, *args], cwd=cfg.mirror_dir, env=env,
                                capture_output=True, text=True, timeout=RESTIC_TIMEOUT)
    except FileNotFoundError as exc:
        raise AgentError(f"не найден restic ({cfg.restic}) — apt install restic") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"restic {args[0]}: не уложился в {RESTIC_TIMEOUT} с") from exc
    if check and result.returncode != 0:
        raise AgentError(f"restic {args[0]} (код {result.returncode}): {result.stderr.strip()[-2000:]}")
    return result


def ensure_repository(cfg: Config) -> None:
    result = _restic(cfg, ["cat", "config"], check=False)
    if result.returncode == 0:
        return
    if result.returncode != 10:  # 10 — репозитория нет (restic ≥ 0.17)
        raise AgentError(f"нет доступа к репозиторию {cfg.repository} (код {result.returncode}): "
                         f"{result.stderr.strip()[-2000:]}")
    log(f"репозитория ещё нет — restic init {cfg.repository}")
    _restic(cfg, ["init"])


def restic_backup(cfg: Config, dirs: list[str]) -> str | None:
    ensure_repository(cfg)
    result = _restic(cfg, ["backup", "--json", "--host", "coop-server", "--tag", "coop",
                           "--exclude", ".rsync-partial", "--", *dirs])
    snapshot_id = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("message_type") == "summary":
            snapshot_id = event.get("snapshot_id")
            log(f"снимок {snapshot_id}: файлов {event.get('total_files_processed')}, "
                f"новых данных {event.get('data_added', 0) / 1024 / 1024:.1f} МБ")
    _restic(cfg, ["forget", "--host", "coop-server", "--tag", "coop", "--prune",
                  "--keep-daily", str(cfg.keep_daily), "--keep-weekly", str(cfg.keep_weekly),
                  "--keep-monthly", str(cfg.keep_monthly), "--keep-yearly", str(cfg.keep_yearly)])
    return snapshot_id


def write_status(cfg: Config, status: dict) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    path = os.path.join(cfg.state_dir, "status.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


def run(cfg: Config) -> int:
    status = {"finished_at": None, "ok": False, "snapshot_id": None, "server_backup_at": None,
              "warning": None, "error": None}
    try:
        os.makedirs(cfg.mirror_dir, mode=0o700, exist_ok=True)
        rsync_dir(cfg, STAGING)
        manifest = read_manifest(cfg)
        status["server_backup_at"] = manifest.get("created_at")
        age = manifest_age_hours(manifest)
        if age is None or age > cfg.max_age_hours:
            # Бэкап всё равно делаем: uploads/ и почта при этом свежие.
            status["warning"] = (f"снимок БД на сервере от {manifest.get('created_at')} — старше "
                                 f"{cfg.max_age_hours} ч: на сервере не работает cron backup_offsite")
            log(f"ПРЕДУПРЕЖДЕНИЕ: {status['warning']}")
        dirs = [STAGING]
        for name in manifest.get("data_dirs", []):
            # Имена — от сервера: только простые имена каталогов, без путей.
            if not name or "/" in name or name.startswith("."):
                raise AgentError(f"недопустимое имя каталога в MANIFEST: {name!r}")
            rsync_dir(cfg, name)
            dirs.append(name)
        status["snapshot_id"] = restic_backup(cfg, dirs)
        status["ok"] = True
    except AgentError as exc:
        status["error"] = str(exc)
        log(f"ОШИБКА: {exc}")
    status["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_status(cfg, status)
    if not status["ok"]:
        return 1
    return 2 if status["warning"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный бэкап данных кооператива")
    parser.add_argument("--config", default=os.environ.get("COOP_BACKUP_CONFIG", DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("snapshots")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    try:
        cfg = Config(args.config)
        if args.command == "run":
            return run(cfg)
        if args.command == "snapshots":
            print(_restic(cfg, ["snapshots"]).stdout, end="")
            return 0
        with open(os.path.join(cfg.state_dir, "status.json"), encoding="utf-8") as fh:
            print(fh.read())
        return 0
    except (AgentError, OSError) as exc:
        log(f"ОШИБКА: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
