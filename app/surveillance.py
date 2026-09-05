"""
Видеонаблюдение (`/surveillance/`) — периодически обновляемые превью-кадры
с камер, подключённых к DVR/NVR-регистраторам кооператива, сгруппированные
по регистратору. Пока только превью (кадр раз в минуту, см.
scripts/dvr_snapshot.py) — просмотр живого видео по клику на превью не
реализован, это отдельная задача на будущее.

Раздел ОБЩЕДОСТУПНЫЙ — виден и анонимному посетителю, без входа в систему
(по прямой просьбе, как новости на странице входа), поэтому у роута
`view()` и у роута отдачи кадра `snapshot()` намеренно НЕТ
`@login_required`. Настройка регистраторов/камер (RTSP-логин/пароль —
чувствительные данные) — только председателю.

Кадр снимается ffmpeg с RTSP-потока регистратора:
    ffmpeg -y -rtsp_transport tcp -i "<rtsp_url>" -vframes 1 <файл>.jpg
Путь потока — `channel=<N>_stream=<M>.sdp`, N/M настраиваются по камере
(канал регистратора и номер потока — у большинства DVR 0 это основной
поток высокого разрешения, 1+ — дополнительные более низкого разрешения).
Регистратор выставлен наружу на нестандартном порту.

Помимо "живого" кадра (snapshot_path — один файл на камеру, перезаписывается
каждый прогон) scripts/dvr_snapshot.py откладывает КАЖДЫЙ снятый кадр ещё и в
историю (history_dir — по файлу на снимок, имя = отметка времени UTC),
глубиной HISTORY_RETENTION_HOURS (см. сам скрипт: обрезка старых файлов —
там же, при каждом прогоне). Галерея истории по камере — camera_history()
ниже, тем же общим лайтбоксом, что и остальные картинки в приложении (см.
base.html: initLightbox, класс "js-lightbox").
"""
import datetime as dt
import os
import re
import shutil
from urllib.parse import quote

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, send_file

from . import database
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, DvrRecorder, DvrCamera
from .bank_api import crypto

bp = Blueprint("surveillance", __name__, url_prefix="/surveillance")

# Имя файла кадра истории — отметка времени UTC, см. scripts/dvr_snapshot.py.
# Тот же паттерн используется здесь для защиты camera_history_frame() от
# path traversal (никаких "../" и т.п. — только это конкретное имя).
HISTORY_FRAME_NAME_RE = re.compile(r"^\d{8}_\d{6}\.jpg$")


def rtsp_url(recorder: DvrRecorder, camera: DvrCamera) -> str:
    """URL потока камеры для ffmpeg — используется и здесь (не сейчас — на
    будущее для живого просмотра), и в scripts/dvr_snapshot.py. Логин/пароль
    экранируются (urlencode) — иначе спецсимвол в пароле (например "@" или
    ":") сломал бы разбор самого URL."""
    password = crypto.decrypt(recorder.password_encrypted) or ""
    auth = ""
    if recorder.username:
        auth = f"{quote(recorder.username, safe='')}:{quote(password, safe='')}@"
    return f"rtsp://{auth}{recorder.host}:{recorder.port}/channel={camera.channel}_stream={camera.stream}.sdp"


def snapshot_dir(recorder_id: int) -> str:
    return os.path.join(current_app.config["DVR_SNAPSHOT_FOLDER"], str(recorder_id), "snapshots")


def snapshot_path(recorder_id: int, camera_id: int) -> str:
    return os.path.join(snapshot_dir(recorder_id), f"camera_{camera_id}.jpg")


def history_dir(recorder_id: int, camera_id: int) -> str:
    """Папка с историей кадров одной камеры (по файлу на снимок) — отдельно
    от "живого" snapshot_path, который всего один и перезаписывается."""
    return os.path.join(current_app.config["DVR_SNAPSHOT_FOLDER"], str(recorder_id), "history", f"camera_{camera_id}")


def combined_dir() -> str:
    """Общий смонтированный кадр (сетка из последних кадров ВСЕХ камер всех
    регистраторов сразу, см. scripts/dvr_snapshot.py: _build_combined_snapshot)
    — по одной камере отдельно смотреть неудобно, здесь всё на одной
    картинке. Не привязан к конкретному регистратору, отдельная папка."""
    return os.path.join(current_app.config["DVR_SNAPSHOT_FOLDER"], "combined")


def combined_snapshot_path() -> str:
    return os.path.join(combined_dir(), "snapshot.jpg")


def combined_history_dir() -> str:
    return os.path.join(combined_dir(), "history")


@bp.route("/")
def view():
    recorders = (
        database.db_session.query(DvrRecorder)
        .order_by(DvrRecorder.sort_order, DvrRecorder.id)
        .all()
    )
    # Отдельного поля "когда обновился общий кадр" в БД нет — он общий на
    # всю систему, не привязан ни к одной модели, поэтому берём mtime
    # самого файла (та же логика, что camera.last_snapshot_at, только без
    # колонки).
    combined_updated_at = None
    combined_path = combined_snapshot_path()
    if os.path.exists(combined_path):
        combined_updated_at = dt.datetime.utcfromtimestamp(os.path.getmtime(combined_path))
    return render_template("surveillance/view.html", recorders=recorders, combined_updated_at=combined_updated_at)


@bp.route("/cameras/<int:camera_id>/snapshot")
def snapshot(camera_id):
    camera = database.db_session.get(DvrCamera, camera_id)
    if camera is None:
        abort(404)
    path = snapshot_path(camera.recorder_id, camera.id)
    if not os.path.exists(path):
        abort(404)
    # Кадр обновляется раз в минуту поверх того же файла — без max_age=0
    # браузер мог бы закэшировать старый и не увидеть новый даже после
    # смены cache-busting параметра в src (see surveillance/view.html) при
    # повторном показе того же <img>.
    return send_file(path, mimetype="image/jpeg", max_age=0)


def _list_history_frames(dir_path: str) -> list[tuple[str, dt.datetime]]:
    """Общий список (имя файла, метка времени) для галереи истории — и
    по одной камере (camera_history), и по общему смонтированному кадру
    (combined_history): та же схема имён (см. HISTORY_FRAME_NAME_RE)."""
    frames = []
    if os.path.isdir(dir_path):
        for name in os.listdir(dir_path):
            try:
                ts = dt.datetime.strptime(name, "%Y%m%d_%H%M%S.jpg")
            except ValueError:
                continue  # посторонний файл в папке — пропускаем, не падаем
            frames.append((name, ts))
    frames.sort(key=lambda pair: pair[1], reverse=True)
    return frames


@bp.route("/cameras/<int:camera_id>/history")
def camera_history(camera_id):
    """Галерея кадров камеры за последние сутки (см. history_dir,
    scripts/dvr_snapshot.py — там же обрезка старше HISTORY_RETENTION_HOURS).
    Общедоступно, как и остальной раздел — см. docstring модуля."""
    camera = database.db_session.get(DvrCamera, camera_id)
    if camera is None:
        abort(404)
    frames = _list_history_frames(history_dir(camera.recorder_id, camera.id))
    return render_template("surveillance/camera_history.html", camera=camera, frames=frames)


@bp.route("/cameras/<int:camera_id>/history/<filename>")
def camera_history_frame(camera_id, filename):
    camera = database.db_session.get(DvrCamera, camera_id)
    if camera is None:
        abort(404)
    if not HISTORY_FRAME_NAME_RE.match(filename):  # защита от path traversal — только ожидаемое имя файла
        abort(404)
    path = os.path.join(history_dir(camera.recorder_id, camera.id), filename)
    if not os.path.exists(path):
        abort(404)
    # Кадры истории неизменны после создания (в отличие от "живого" snapshot) — кэш браузера безопасен.
    return send_file(path, mimetype="image/jpeg", max_age=3600)


@bp.route("/combined/snapshot")
def combined_snapshot():
    """Живой смонтированный кадр всех камер сразу — см. combined_dir()."""
    path = combined_snapshot_path()
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=0)


@bp.route("/combined/history")
def combined_history():
    """Галерея смонтированных кадров (все камеры сразу) за последние сутки —
    тот же принцип, что camera_history(), но не привязано к одной камере."""
    frames = _list_history_frames(combined_history_dir())
    return render_template("surveillance/combined_history.html", frames=frames)


@bp.route("/combined/history/<filename>")
def combined_history_frame(filename):
    if not HISTORY_FRAME_NAME_RE.match(filename):
        abort(404)
    path = os.path.join(combined_history_dir(), filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=3600)


@bp.route("/recorders/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create_recorder():
    f = request.form
    name = f.get("name", "").strip()
    host = f.get("host", "").strip()
    if not name or not host:
        flash(_("Укажите название и адрес регистратора."), "danger")
        return redirect(url_for("surveillance.view"))

    port_raw = f.get("port", "").strip()
    try:
        port = int(port_raw) if port_raw else 554
    except ValueError:
        flash(_("Порт должен быть числом."), "danger")
        return redirect(url_for("surveillance.view"))

    password = f.get("password", "")
    recorder = DvrRecorder(
        name=name,
        host=host,
        port=port,
        username=f.get("username") or None,
        password_encrypted=crypto.encrypt(password) if password else None,
        comment=f.get("comment") or None,
    )
    database.db_session.add(recorder)
    database.db_session.flush()  # нужен recorder.id для камер

    labels = request.form.getlist("camera_label")
    channels = request.form.getlist("camera_channel")
    streams = request.form.getlist("camera_stream")
    added = 0
    for label, channel_raw, stream_raw in zip(labels, channels, streams):
        channel_raw = channel_raw.strip()
        if not channel_raw:
            continue  # пустая строка камеры (например, убрали кнопкой на клиенте, но строка осталась) — пропускаем
        try:
            channel = int(channel_raw)
            stream = int(stream_raw) if stream_raw.strip() else 0
        except ValueError:
            continue
        database.db_session.add(DvrCamera(
            recorder_id=recorder.id,
            label=label.strip() or _("Камера {n}", n=added + 1),
            channel=channel,
            stream=stream,
            sort_order=added,
        ))
        added += 1

    database.db_session.commit()
    flash(_("Регистратор добавлен."), "success")
    return redirect(url_for("surveillance.view"))


@bp.route("/recorders/<int:recorder_id>/edit", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit_recorder(recorder_id):
    """
    Правит и сам регистратор, и список его камер разом (та же форма, что и
    создание, см. view.html: editRecorderModal). Пустой пароль — не менять
    (как везде в приложении для секретов, см. bank_sync/electricity_monitor).

    Камеры сверяются с уже существующими ПО ID (camera_id — скрытое поле
    каждой строки формы, см. surveillance/_camera_row.html), а не
    удаляются все разом с созданием заново: иначе неизменившаяся камера
    теряла бы связь со уже снятым на диске кадром (snapshot_path — по id
    камеры) до следующего кадра раз в минуту (см. scripts/dvr_snapshot.py).
    Убранную из формы камеру удаляем вместе с её файлом кадра на диске,
    если он есть — иначе такие файлы копились бы бесхозными.
    """
    recorder = database.db_session.get(DvrRecorder, recorder_id)
    if recorder is None:
        abort(404)

    f = request.form
    name = f.get("name", "").strip()
    host = f.get("host", "").strip()
    if not name or not host:
        flash(_("Укажите название и адрес регистратора."), "danger")
        return redirect(url_for("surveillance.view"))

    port_raw = f.get("port", "").strip()
    try:
        port = int(port_raw) if port_raw else 554
    except ValueError:
        flash(_("Порт должен быть числом."), "danger")
        return redirect(url_for("surveillance.view"))

    recorder.name = name
    recorder.host = host
    recorder.port = port
    recorder.username = f.get("username") or None
    password = f.get("password", "")
    if password:
        recorder.password_encrypted = crypto.encrypt(password)
    recorder.comment = f.get("comment") or None

    existing_by_id = {c.id: c for c in recorder.cameras}
    camera_ids = request.form.getlist("camera_id")
    labels = request.form.getlist("camera_label")
    channels = request.form.getlist("camera_channel")
    streams = request.form.getlist("camera_stream")

    seen_ids = set()
    sort_order = 0
    for cam_id_raw, label, channel_raw, stream_raw in zip(camera_ids, labels, channels, streams):
        channel_raw = channel_raw.strip()
        if not channel_raw:
            continue  # пустая строка камеры (убрали кнопкой на клиенте, но строка осталась) — пропускаем
        try:
            channel = int(channel_raw)
            stream = int(stream_raw) if stream_raw.strip() else 0
        except ValueError:
            continue
        camera_label = label.strip() or _("Камера {n}", n=sort_order + 1)
        cam_id = int(cam_id_raw) if cam_id_raw.strip().isdigit() else None
        if cam_id is not None and cam_id in existing_by_id:
            camera = existing_by_id[cam_id]
            camera.label = camera_label
            camera.channel = channel
            camera.stream = stream
            camera.sort_order = sort_order
            seen_ids.add(cam_id)
        else:
            database.db_session.add(DvrCamera(
                recorder_id=recorder.id, label=camera_label,
                channel=channel, stream=stream, sort_order=sort_order,
            ))
        sort_order += 1

    for cam_id, camera in existing_by_id.items():
        if cam_id not in seen_ids:
            database.db_session.delete(camera)
            snap_path = snapshot_path(recorder.id, cam_id)
            if os.path.exists(snap_path):
                os.remove(snap_path)
            shutil.rmtree(history_dir(recorder.id, cam_id), ignore_errors=True)

    database.db_session.commit()
    flash(_("Регистратор изменён."), "success")
    return redirect(url_for("surveillance.view"))


@bp.route("/recorders/<int:recorder_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_recorder(recorder_id):
    recorder = database.db_session.get(DvrRecorder, recorder_id)
    if recorder is None:
        abort(404)
    database.db_session.delete(recorder)  # каскадом удалит и DvrCamera (ondelete=CASCADE)
    database.db_session.commit()
    shutil.rmtree(os.path.join(current_app.config["DVR_SNAPSHOT_FOLDER"], str(recorder_id)), ignore_errors=True)
    flash(_("Регистратор удалён."), "success")
    return redirect(url_for("surveillance.view"))
