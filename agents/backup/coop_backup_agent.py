#!/usr/bin/env python3
"""
Агент локального бэкапа: работает на компьютере председателя и раз в сутки
забирает с серверов кооперативов свежую копию данных в локальный
restic-репозиторий с историей версий. Это третья копия, рядом с
instance/backups/ на самом сервере и внешним репозиторием
(scripts/backup_offsite.py).

Кооперативов может быть несколько (секции [coop:ИМЯ] конфига): каждый —
свой пользователь на сервере, своё зеркало mirror_dir/ИМЯ/ и свои снимки
(restic --host = snapshot_host) в общем репозитории — одинаковые файлы
restic хранит один раз. Ошибка у одного кооператива не мешает остальным.

Почему pull, а не push с сервера: компьютер за NAT, и у сервера нет
никакого доступа к этой копии. Если сервер взломают, отсюда ничего не
сотрут. Ключ ssh агента на сервере ограничен forced command'ом
scripts/backup_pull_gate.sh (rrsync -ro): только чтение и только каталог
instance/ кооператива. С украденным ключом на сервере тоже ничего не сделать.

Проход, для каждого кооператива:
  1. rsync backup_staging/ (снимок БД, файл окружения, MANIFEST.json —
     готовит scripts/backup_offsite.py по cron на сервере);
  2. MANIFEST: проверка свежести (старше max_age_hours — предупреждение:
     на сервере не работает cron) и список каталогов данных;
  3. rsync каждого каталога из MANIFEST (uploads/, bank_certs/, …) в
     зеркало — с --delete: зеркало повторяет сервер, история — в restic;
  4. restic backup зеркала → forget по политике хранения;
затем один restic prune и state_dir/status.json — итог прохода.

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
import re
import shlex
import subprocess
import sys

DEFAULT_CONFIG = "/etc/coop-backup/coop_backup.ini"
STAGING = "backup_staging"
# --host снимков в конфиге без секций [coop:…] (один кооператив) — как
# было до списка кооперативов, чтобы история продолжалась.
LEGACY_SNAPSHOT_HOST = "coop-server"
COOP_SECTION = "coop:"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RSYNC_TIMEOUT = 6 * 3600
RESTIC_TIMEOUT = 6 * 3600


def log(message: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


class AgentError(Exception):
    pass


class Coop:
    """Один кооператив: откуда забирать (user@host) и куда (mirror_dir,
    снимки с --host snapshot_host)."""

    def __init__(self, name: str, user: str, host: str, port: int, mirror_dir: str, snapshot_host: str):
        self.name = name
        self.user = user
        self.host = host
        self.port = port
        self.mirror_dir = mirror_dir
        self.snapshot_host = snapshot_host


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
        self.port = server.getint("port", 22)
        self.identity_file = server.get("identity_file", "").strip()
        self.known_hosts = server.get("known_hosts", "").strip()
        if not self.identity_file:
            raise AgentError("в [server] нужен identity_file")

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
        self.coops = self._coops(parser, server)

    def _coops(self, parser: configparser.ConfigParser, server) -> list[Coop]:
        sections = [s for s in parser.sections() if s.startswith(COOP_SECTION)]
        if not sections:
            # Один кооператив, user в [server]; зеркало — сам mirror_dir.
            user = server.get("user", "").strip()
            if not self.host or not user:
                raise AgentError("в [server] нужны host и user (или секции [coop:ИМЯ])")
            return [Coop(user, user, self.host, self.port, self.mirror_dir, LEGACY_SNAPSHOT_HOST)]
        coops = []
        for section in sections:
            name = section[len(COOP_SECTION):].strip()
            if not NAME_RE.match(name):
                raise AgentError(f"[{section}]: имя — латиница в нижнем регистре, цифры и «-»")
            sect = parser[section]
            user = sect.get("user", name).strip()
            host = sect.get("host", self.host).strip()
            if not user or not host:
                raise AgentError(f"[{section}]: нужны user и host (или host в [server])")
            coops.append(Coop(name, user, host, sect.getint("port", self.port),
                              os.path.join(self.mirror_dir, name),
                              sect.get("snapshot_host", name).strip() or name))
        hosts = [c.snapshot_host for c in coops]
        if len(set(hosts)) != len(hosts):
            raise AgentError("snapshot_host у кооперативов повторяется — снимки бы перемешались")
        return coops


def _ssh_command(cfg: Config, coop: Coop) -> str:
    parts = ["ssh", "-p", str(coop.port), "-i", cfg.identity_file, "-o", "BatchMode=yes",
             "-o", "IdentitiesOnly=yes", "-o", "ServerAliveInterval=30"]
    if cfg.known_hosts:
        parts += ["-o", f"UserKnownHostsFile={cfg.known_hosts}", "-o", "StrictHostKeyChecking=yes"]
    return shlex.join(parts)


def rsync_dir(cfg: Config, coop: Coop, name: str) -> None:
    """Каталог name на сервере (относительно instance/ кооператива, rrsync) →
    coop.mirror_dir/name/."""
    target = os.path.join(coop.mirror_dir, name)
    os.makedirs(target, exist_ok=True)
    argv = [cfg.rsync, "-a", "--delete", "--partial-dir=.rsync-partial", "-e", _ssh_command(cfg, coop),
            f"{coop.user}@{coop.host}:{name}/", target + "/"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=RSYNC_TIMEOUT)
    except FileNotFoundError as exc:
        raise AgentError(f"не найден rsync ({cfg.rsync})") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"rsync {name}: не уложился в {RSYNC_TIMEOUT} с") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise AgentError(f"rsync {name} (код {result.returncode}){_rsync_hint(cfg, coop, name, stderr)}: "
                         f"{stderr[-2000:]}")


def _rsync_hint(cfg: Config, coop: Coop, name: str, stderr: str) -> str:
    """Понятная причина для частых ошибок первого запуска — перед сырым
    выводом rsync/ssh."""
    if "Host key verification failed" in stderr or "host key is known" in stderr:
        return (f" — в {cfg.known_hosts or '~/.ssh/known_hosts'} нет ключа сервера {coop.host} "
                f"(или файл не читается пользователем агента): ssh-keyscan -p {coop.port} {coop.host} "
                f"| sudo tee {cfg.known_hosts or '/etc/coop-backup/known_hosts'}; домен — в punycode")
    if "Permission denied" in stderr and "publickey" in stderr:
        return (f" — ключа агента нет в ~/.ssh/authorized_keys пользователя {coop.user} на сервере "
                f"(backup_agent_pubkeys кооператива в vars.yml или deployment/setup.sh --tags local)")
    if "No such file or directory" in stderr and name == STAGING:
        return (f" — на сервере нет instance/{STAGING}/: ни разу не запускался backup_offsite "
                f"(cron в 03:20 или вручную: scripts/backup_offsite.sh под пользователем {coop.user})")
    if "No such file or directory" in stderr:
        return f" — на сервере нет каталога instance/{name}/"
    return ""


def read_manifest(coop: Coop) -> dict:
    path = os.path.join(coop.mirror_dir, STAGING, "MANIFEST.json")
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


def _restic(cfg: Config, args: list[str], check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, RESTIC_REPOSITORY=cfg.repository, RESTIC_PASSWORD_FILE=cfg.password_file)
    try:
        result = subprocess.run([cfg.restic, *args], cwd=cwd or cfg.state_dir, env=env,
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


def restic_backup(cfg: Config, coop: Coop, dirs: list[str]) -> str | None:
    # Пути — относительные (cwd — зеркало кооператива): внутри снимка
    # /backup_staging/coop.db, /uploads/… у всех кооперативов одинаково.
    result = _restic(cfg, ["backup", "--json", "--host", coop.snapshot_host, "--tag", "coop",
                           "--exclude", ".rsync-partial", "--", *dirs], cwd=coop.mirror_dir)
    snapshot_id = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("message_type") == "summary":
            snapshot_id = event.get("snapshot_id")
            log(f"{coop.name}: снимок {snapshot_id}: файлов {event.get('total_files_processed')}, "
                f"новых данных {event.get('data_added', 0) / 1024 / 1024:.1f} МБ")
    # group-by без paths: набор каталогов (MANIFEST) и путь зеркала могут
    # меняться, политика хранения при этом — одна на кооператив.
    _restic(cfg, ["forget", "--host", coop.snapshot_host, "--tag", "coop", "--group-by", "host,tags",
                  "--keep-daily", str(cfg.keep_daily), "--keep-weekly", str(cfg.keep_weekly),
                  "--keep-monthly", str(cfg.keep_monthly), "--keep-yearly", str(cfg.keep_yearly)])
    return snapshot_id


def backup_coop(cfg: Config, coop: Coop) -> dict:
    status = {"ok": False, "snapshot_id": None, "server_backup_at": None, "warning": None, "error": None}
    try:
        os.makedirs(coop.mirror_dir, mode=0o700, exist_ok=True)
        rsync_dir(cfg, coop, STAGING)
        manifest = read_manifest(coop)
        status["server_backup_at"] = manifest.get("created_at")
        age = manifest_age_hours(manifest)
        if age is None or age > cfg.max_age_hours:
            # Бэкап всё равно делаем: uploads/ и почта при этом свежие.
            status["warning"] = (f"снимок БД на сервере от {manifest.get('created_at')} — старше "
                                 f"{cfg.max_age_hours} ч: на сервере не работает cron backup_offsite")
            log(f"{coop.name}: ПРЕДУПРЕЖДЕНИЕ: {status['warning']}")
        dirs = [STAGING]
        for name in manifest.get("data_dirs", []):
            # Имена — от сервера: только простые имена каталогов, без путей.
            if not name or "/" in name or name.startswith("."):
                raise AgentError(f"недопустимое имя каталога в MANIFEST: {name!r}")
            rsync_dir(cfg, coop, name)
            dirs.append(name)
        status["snapshot_id"] = restic_backup(cfg, coop, dirs)
        status["ok"] = True
    except AgentError as exc:
        status["error"] = str(exc)
        log(f"{coop.name}: ОШИБКА: {exc}")
    return status


def write_status(cfg: Config, status: dict) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    path = os.path.join(cfg.state_dir, "status.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


def _joined(coops: dict, key: str) -> str | None:
    parts = [f"{name}: {st[key]}" for name, st in coops.items() if st[key]]
    return "; ".join(parts) or None


def run(cfg: Config) -> int:
    """Итог в status.json: ok — у всех кооперативов; error/warning —
    сводка «имя: текст; …»; coops — по каждому."""
    status = {"finished_at": None, "ok": False, "warning": None, "error": None, "coops": {}}
    error = None
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        ensure_repository(cfg)
        for coop in cfg.coops:
            log(f"{coop.name}: {coop.user}@{coop.host}")
            status["coops"][coop.name] = backup_coop(cfg, coop)
        if any(st["ok"] for st in status["coops"].values()):
            # forget — по каждому кооперативу, место освобождается один раз.
            _restic(cfg, ["prune"])
    except AgentError as exc:
        error = str(exc)
        log(f"ОШИБКА: {exc}")
    coops = status["coops"]
    status["ok"] = error is None and len(coops) == len(cfg.coops) and all(st["ok"] for st in coops.values())
    status["error"] = "; ".join(filter(None, [error, _joined(coops, "error")])) or None
    status["warning"] = _joined(coops, "warning")
    status["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_status(cfg, status)
    if not status["ok"]:
        return 1
    return 2 if status["warning"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный бэкап данных кооперативов")
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
            print(_restic(cfg, ["snapshots", "--group-by", "host"]).stdout, end="")
            return 0
        with open(os.path.join(cfg.state_dir, "status.json"), encoding="utf-8") as fh:
            print(fh.read())
        return 0
    except (AgentError, OSError) as exc:
        log(f"ОШИБКА: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
