#!/usr/bin/env python3
"""
Снятие превью-кадра с каждой камеры видеонаблюдения — для запуска по cron
раз в минуту (см. scripts/dvr_snapshot.sh и README.md, раздел
«Автоматизация»).

По аналогии с scripts/poll_ewelink.py: отдельный скрипт, не фоновый поток
внутри веб-процесса.

Логика:
  1. Если ffmpeg не установлен в системе (shutil.which) — тихо выйти с
     кодом 1 и понятным сообщением в лог, ничего не пытаясь (все камеры
     всё равно не снимутся).
  2. Для каждой DvrCamera каждого DvrRecorder — собрать RTSP URL
     (app.surveillance.rtsp_url) и вызвать
         ffmpeg -y -rtsp_transport tcp -i "<url>" -vframes 1 <tmp>.jpg
     во временный файл РЯДОМ с целевым (та же папка — os.rename внутри
     одной файловой системы атомарен; если писать сразу в целевой файл,
     веб-процесс мог бы отдать наполовину записанный кадр, читая его в
     этот же момент).
  3. Успех/ошибка — per-camera try/except, как в poll_ewelink.py:
     единственная зависшая/недоступная камера не должна останавливать
     снятие кадров с остальных. last_error сохраняется, но last_snapshot_at
     НЕ трогается при ошибке — на странице продолжает показываться
     последний удачный кадр, а не пустота.
  4. Один commit в конце.

Требует системный бинарник ffmpeg (не входит в requirements.txt — не
Python-пакет). Установка, например: `apt install ffmpeg`.

Запуск вручную:
    cd /path/to/project && python3 scripts/dvr_snapshot.py
"""
import datetime as dt
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database
from app.models import DvrCamera
from app.surveillance import rtsp_url, snapshot_dir, snapshot_path

FFMPEG_TIMEOUT_SECONDS = 20


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _capture(camera: DvrCamera) -> str | None:
    """Снимает один кадр в целевой файл камеры. Возвращает текст ошибки или
    None при успехе."""
    os.makedirs(snapshot_dir(camera.recorder_id), exist_ok=True)
    target = snapshot_path(camera.recorder_id, camera.id)
    # ".tmp" ПЕРЕД расширением, не после (не "camera_5.jpg.tmp") — ffmpeg
    # без явного -f выбирает мьюксер по расширению ВЫХОДНОГО файла, а
    # ".tmp" ему незнакомо: "Unable to choose an output format for
    # '...jpg.tmp'" / "Error opening output files: Invalid argument",
    # даже когда RTSP-поток открылся нормально (воспроизведено вручную).
    root, ext = os.path.splitext(target)
    tmp_path = f"{root}.tmp{ext}"

    url = rtsp_url(camera.recorder, camera)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", url, "-vframes", "1", tmp_path],
            capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"таймаут ffmpeg ({FFMPEG_TIMEOUT_SECONDS} сек)"
    except OSError as exc:
        return f"не удалось запустить ffmpeg: {exc}"

    if result.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        stderr_tail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] if result.stderr else []
        return f"ffmpeg завершился с кодом {result.returncode}" + (f": {stderr_tail[0]}" if stderr_tail else "")

    os.replace(tmp_path, target)  # атомарная замена — читатели никогда не увидят наполовину записанный файл
    return None


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"ffmpeg не найден в PATH — снятие кадров пропущено (см. docstring скрипта).", file=sys.stderr)
        return 1

    app = create_app()
    with app.app_context():
        cameras = database.db_session.query(DvrCamera).all()
        if not cameras:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] Камер не настроено — снимать нечего.")
            return 0

        saved, failed = 0, 0
        for camera in cameras:
            try:
                error = _capture(camera)
            except Exception as exc:  # неожиданная ошибка одной камеры не должна ронять весь прогон
                error = str(exc)

            camera.last_error = error
            if error is None:
                camera.last_snapshot_at = _utcnow()
                saved += 1
            else:
                failed += 1
                print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                      f"{camera.recorder.name} / {camera.label}: {error}", file=sys.stderr)

        database.db_session.commit()
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Снято кадров: {saved}, ошибок: {failed}.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
