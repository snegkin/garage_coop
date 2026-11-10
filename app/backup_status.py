"""
Свежесть внешних копий для дашборда председателя. Бэкап, переставший
работать молча, хуже, чем никакого: о нём вспоминают, когда он нужен.

Источники — файлы, которые пишут сами бэкапы, в БД ничего не хранится:
  logs/backup_offsite_status.json — итог scripts/backup_offsite.py
      (внешний restic-репозиторий + каталог для агента председателя);
  logs/backup_pull.log — заходы агента председателя
      (scripts/backup_pull_gate.sh), последняя строка — последний забор.

Чтобы не пугать на машине разработчика, где cron не стоит: если
backup_offsite ни разу не запускался, предупреждение — только когда рядом
есть свежие снимки backup_db (значит, cron здесь работает, а внешнего
бэкапа нет).
"""
import datetime as dt
import json
import os

OFFSITE_MAX_AGE = dt.timedelta(hours=36)
# Компьютер председателя может быть выключен на выходные.
PULL_MAX_AGE = dt.timedelta(days=3)
LOCAL_BACKUP_RECENT = dt.timedelta(days=2)


def _parse_time(value) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # backup_pull.log пишет date -Iseconds со смещением, status.json — локальное
    # время без смещения; сравниваем всё в локальном наивном.
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _read_status(log_dir: str) -> dict | None:
    try:
        with open(os.path.join(log_dir, "backup_offsite_status.json"), encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def _last_pull(log_dir: str) -> dt.datetime | None:
    path = os.path.join(log_dir, "backup_pull.log")
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 4096))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        moment = _parse_time(line.split(" ", 1)[0])
        if moment:
            return moment
    return None


def _has_recent_local_backups(backup_dir: str, now: dt.datetime) -> bool:
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return False
    cutoff = (now - LOCAL_BACKUP_RECENT).timestamp()
    for name in names:
        if name.startswith("coop_") and name.endswith((".db", ".db.gz")):
            try:
                if os.path.getmtime(os.path.join(backup_dir, name)) >= cutoff:
                    return True
            except OSError:
                continue
    return False


def backup_warnings(log_dir: str, backup_dir: str, now: dt.datetime | None = None) -> list[str]:
    """Строки предупреждений (по-русски, уже готовые к _()) — пусто, если
    всё в порядке."""
    from .i18n import translate as _

    now = now or dt.datetime.now()
    status = _read_status(log_dir)
    if status is None:
        if _has_recent_local_backups(backup_dir, now):
            return [_("Внешний бэкап ни разу не запускался: копии БД есть только на этом сервере. "
                      "Проверьте cron backup_offsite (docs/deployment.md, «Внешний бэкап»).")]
        return []

    warnings = []
    finished = _parse_time(status.get("finished_at"))
    if not status.get("ok"):
        warnings.append(_("Внешний бэкап завершился ошибкой: {error}", error=status.get("error") or "?"))
    elif finished is None or now - finished > OFFSITE_MAX_AGE:
        warnings.append(_("Внешний бэкап не запускался с {date}: проверьте cron backup_offsite.",
                          date=finished.strftime("%d.%m.%Y %H:%M") if finished else "?"))
    elif not status.get("restic"):
        warnings.append(_("Внешний репозиторий бэкапа не настроен (RESTIC_REPOSITORY): "
                          "копии есть только на сервере и у председателя."))

    last_pull = _last_pull(log_dir)
    if last_pull is None:
        warnings.append(_("Агент локального бэкапа на компьютере председателя ни разу не забирал данные "
                          "(backup_agent/README.md)."))
    elif now - last_pull > PULL_MAX_AGE:
        warnings.append(_("Локальная копия на компьютере председателя не обновлялась с {date}.",
                          date=last_pull.strftime("%d.%m.%Y %H:%M")))
    return warnings
