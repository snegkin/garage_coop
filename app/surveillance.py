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
"""
import datetime as dt
import os
import shutil
from urllib.parse import quote

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, send_file

from . import database
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, DvrRecorder, DvrCamera
from .bank_api import crypto

bp = Blueprint("surveillance", __name__, url_prefix="/surveillance")


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


@bp.route("/")
def view():
    recorders = (
        database.db_session.query(DvrRecorder)
        .order_by(DvrRecorder.sort_order, DvrRecorder.id)
        .all()
    )
    return render_template("surveillance/view.html", recorders=recorders)


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
