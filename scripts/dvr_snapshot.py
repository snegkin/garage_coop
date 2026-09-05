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
     этот же момент). Кадр отдельной камеры (snapshot_path) — только
     сырьё для сборки общего кадра регистратора (см. п.5), сам по себе
     никуда не отдаётся и историю не ведёт — по одной камере отдельно не
     смотрим (см. docstring app/surveillance.py).
  3. Успех/ошибка — per-camera try/except, как в poll_ewelink.py:
     единственная зависшая/недоступная камера не должна останавливать
     снятие кадров с остальных. last_error сохраняется, но last_snapshot_at
     НЕ трогается при ошибке — на странице продолжает показываться
     последний удачный (общий) кадр, а не пустота.
  4. Один commit в конце.
  5. Для КАЖДОГО регистратора — общий смонтированный кадр (сетка из
     последних снятых кадров ВСЕХ его камер сразу, см.
     _build_combined_snapshot и app.surveillance.combined_dir). Строится
     ffmpeg (фильтр xstack, без масштабирования — в исходном разрешении
     каждой камеры) — без Pillow, чтобы не тащить ещё одну
     Python-зависимость только ради этого. Берутся кадры, которые
     реально есть на диске, даже если конкретно в этом прогоне съёмка
     камеры не удалась (тот же принцип, что last_snapshot_at) — иначе
     единственная недоступная камера выбивала бы общий кадр регистратора
     целиком. Копируется в историю регистратора и подчищается
     _prune_history.

Требует системный бинарник ffmpeg (не входит в requirements.txt — не
Python-пакет). Установка, например: `apt install ffmpeg`.

Запуск вручную:
    cd /path/to/project && python3 scripts/dvr_snapshot.py
"""
import datetime as dt
import math
import os
import shutil
import subprocess
import sys
from itertools import groupby

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database
from app.models import DvrCamera
from app.surveillance import (
    rtsp_url, snapshot_dir, snapshot_path,
    combined_dir, combined_snapshot_path, combined_history_dir,
)

FFMPEG_TIMEOUT_SECONDS = 20
HISTORY_RETENTION_HOURS = 24  # глубина хранения истории смонтированных кадров на регистратор


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _prune_history(dir_path: str, now: dt.datetime) -> None:
    """Удаляет файлы истории старше HISTORY_RETENTION_HOURS. Имя файла —
    сама метка времени (см. _update_combined_snapshot) — не нужно ни
    трогать mtime, ни хранить это отдельно в БД: не смог распарсить имя
    (посторонний файл) — пропускаем, не трогаем."""
    cutoff = now - dt.timedelta(hours=HISTORY_RETENTION_HOURS)
    for name in os.listdir(dir_path):
        try:
            ts = dt.datetime.strptime(name, "%Y%m%d_%H%M%S.jpg")
        except ValueError:
            continue
        if ts < cutoff:
            try:
                os.remove(os.path.join(dir_path, name))
            except OSError:
                pass


def _capture(camera: DvrCamera) -> str | None:
    """Снимает один кадр в целевой файл камеры — сырьё для общего кадра
    регистратора (см. docstring модуля). Возвращает текст ошибки или None
    при успехе."""
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


def _build_combined_snapshot(image_paths: list[str], out_path: str) -> bool:
    """Монтирует уже отснятые кадры камер (image_paths) в одну сетку NxM
    через ffmpeg (фильтр xstack) БЕЗ масштабирования — каждая ячейка в
    полном разрешении, как снята камерой. xstack сам выравнивает тайлы по
    сетке через символьные ссылки w{i}/h{i} на реальные размеры конкретных
    входов (официальный приём ffmpeg для сетки без единого общего размера
    тайла: x — сумма ширин предыдущих тайлов ТОЙ ЖЕ строки, y — сумма
    высот первых тайлов предыдущих строк). Без Pillow — ffmpeg и так
    обязателен для самой съёмки. Возвращает True при успехе.

    Предполагает, что все камеры одной строки/столбца дают одинаковый
    размер кадра (обычно так и есть — камеры настраиваются на один и тот
    же stream), иначе в сетке возможны небольшие пустоты/наложения —
    приемлемо ради полного разрешения без сжатия."""
    n = len(image_paths)
    if n == 0:
        return False
    cols = math.ceil(math.sqrt(n))

    inputs = []
    for p in image_paths:
        inputs += ["-i", p]

    positions = []
    for i in range(n):
        row, col = divmod(i, cols)
        x_expr = "+".join(f"w{j}" for j in range(row * cols, i)) or "0"
        y_expr = "+".join(f"h{j * cols}" for j in range(row)) or "0"
        positions.append(f"{x_expr}_{y_expr}")

    filter_complex = f"xstack=inputs={n}:layout={'|'.join(positions)}[out]"

    root, ext = os.path.splitext(out_path)
    tmp_path = f"{root}.tmp{ext}"  # см. _capture — ".tmp" перед расширением, иначе ffmpeg не распознаёт формат по имени
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex, "-map", "[out]", tmp_path],
            capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False

    if result.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    os.replace(tmp_path, out_path)
    return True


def _update_combined_snapshot(recorder_id: int, cameras: list[DvrCamera]) -> None:
    """Общий смонтированный кадр ОДНОГО регистратора — по кадрам его камер,
    которые реально есть на диске (в т.ч. с прошлых прогонов, если
    конкретно в этом прогоне камера не снялась — см. docstring модуля,
    п.5). Вынесено из main() отдельной функцией ради тестируемости: сам
    main() не тестируется напрямую — создаёт свой собственный Flask-app
    через create_app(), поэтому подменить конфиг (DVR_SNAPSHOT_FOLDER) в
    тесте, где app — фикстура pytest, на него не подействует."""
    image_paths = [
        p for c in cameras
        for p in [snapshot_path(c.recorder_id, c.id)]
        if os.path.exists(p)
    ]
    if not image_paths:
        return

    os.makedirs(combined_dir(recorder_id), exist_ok=True)
    combined_target = combined_snapshot_path(recorder_id)
    if not _build_combined_snapshot(image_paths, combined_target):
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Регистратор {recorder_id}: не удалось построить общий смонтированный кадр.", file=sys.stderr)
        return

    now = _utcnow()
    try:
        hist_dir = combined_history_dir(recorder_id)
        os.makedirs(hist_dir, exist_ok=True)
        shutil.copyfile(combined_target, os.path.join(hist_dir, now.strftime("%Y%m%d_%H%M%S") + ".jpg"))
        _prune_history(hist_dir, now)
    except OSError:
        pass


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"ffmpeg не найден в PATH — снятие кадров пропущено (см. docstring скрипта).", file=sys.stderr)
        return 1

    app = create_app()
    with app.app_context():
        cameras = database.db_session.query(DvrCamera).order_by(DvrCamera.recorder_id).all()
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

        # order_by(recorder_id) выше — обязательное условие для groupby
        # (группирует только СОСЕДНИЕ элементы с одинаковым ключом).
        for recorder_id, group in groupby(cameras, key=lambda c: c.recorder_id):
            _update_combined_snapshot(recorder_id, list(group))

        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
