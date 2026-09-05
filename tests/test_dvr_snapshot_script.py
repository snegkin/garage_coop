"""
scripts/dvr_snapshot.py:_capture — временный файл кадра ДОЛЖЕН заканчиваться
на ".jpg" (реальное расширение), а не на ".tmp": без явного -f ffmpeg сам
выбирает мьюксер по расширению ВЫХОДНОГО файла, и ".tmp" ему не знаком —
"Unable to choose an output format" / "Error opening output files: Invalid
argument", даже когда RTSP-поток открылся нормально (воспроизведено вручную
через ffmpeg -f lavfi -i testsrc ... out.jpg.tmp). Отсюда и баг: снятие кадра
с полностью доступной, правильно настроенной камеры всё равно завершалось
ошибкой на этапе открытия ВЫХОДНОГО файла.
"""
import datetime as dt
import importlib.util
import os

import pytest

from app.models import DvrRecorder, DvrCamera
from app.bank_api import crypto
from app.surveillance import snapshot_path, combined_snapshot_path, combined_history_dir


def _load_dvr_snapshot_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "dvr_snapshot.py")
    spec = importlib.util.spec_from_file_location("dvr_snapshot_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def dvr_snapshot_module():
    return _load_dvr_snapshot_module()


def make_recorder(db, name="Регистратор 1", host="192.168.1.20", port=554, username="admin", password="secret", **kwargs):
    rec = DvrRecorder(
        name=name, host=host, port=port, username=username,
        password_encrypted=crypto.encrypt(password) if password else None,
        **kwargs,
    )
    db.add(rec)
    db.flush()
    return rec


def make_camera(db, recorder, label="Камера 1", channel=1, stream=0, **kwargs):
    cam = DvrCamera(recorder_id=recorder.id, label=label, channel=channel, stream=stream, **kwargs)
    db.add(cam)
    db.flush()
    return cam


def test_capture_uses_tmp_path_with_real_extension_not_appended_after_it(app, db, dvr_snapshot_module, monkeypatch, tmp_path):
    """Регресс на конкретный баг: раньше tmp_path был "camera_5.jpg.tmp" —
    ffmpeg не мог выбрать мьюксер по ".tmp" и падал на открытии выходного
    файла. Теперь должно быть "camera_5.tmp.jpg" — расширение ".jpg" в
    конце, как и у настоящего целевого файла."""
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    captured_output_path = {}

    class FakeResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, capture_output, timeout):
        output_path = cmd[-1]
        captured_output_path["path"] = output_path
        assert output_path.endswith(".jpg"), f"ffmpeg получит выходной файл без распознаваемого расширения: {output_path}"
        with open(output_path, "wb") as fh:
            fh.write(b"fake-jpeg-bytes")
        return FakeResult()

    monkeypatch.setattr(dvr_snapshot_module.subprocess, "run", fake_run)

    error = dvr_snapshot_module._capture(camera)

    assert error is None
    assert captured_output_path["path"].endswith(".tmp.jpg")
    assert not captured_output_path["path"].endswith(".jpg.tmp")

    from app.surveillance import snapshot_path
    target = snapshot_path(camera.recorder_id, camera.id)
    assert os.path.exists(target)
    assert not os.path.exists(captured_output_path["path"])  # переименован в target, временного не осталось


def _fake_run_factory():
    class FakeResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, capture_output, timeout):
        output_path = cmd[-1]
        with open(output_path, "wb") as fh:
            fh.write(b"fake-jpeg-bytes")
        return FakeResult()

    return fake_run


# ---------------------------------------------------------------------------
# _prune_history — общая функция, используется только для истории общего
# смонтированного кадра регистратора (per-camera история убрана вместе с
# per-camera отображением, см. app/surveillance.py).
# ---------------------------------------------------------------------------

def test_prune_history_removes_only_files_older_than_retention_window(tmp_path, dvr_snapshot_module):
    now = dt.datetime(2026, 9, 5, 12, 0, 0)
    fresh_name = "20260905_113000.jpg"  # 30 минут назад — в пределах суток
    stale_name = "20260904_113000.jpg"  # чуть больше суток назад
    garbage_name = "not-a-timestamp.jpg"  # посторонний файл — не трогаем и не падаем
    for name in (fresh_name, stale_name, garbage_name):
        with open(os.path.join(tmp_path, name), "wb") as fh:
            fh.write(b"x")

    dvr_snapshot_module._prune_history(str(tmp_path), now)

    remaining = set(os.listdir(tmp_path))
    assert remaining == {fresh_name, garbage_name}


# ---------------------------------------------------------------------------
# Общий смонтированный кадр (_build_combined_snapshot) — все камеры ОДНОГО
# регистратора сразу в одну сетку, см. app.surveillance:
# combined_dir/combined_snapshot_path. По одной камере отдельно не
# смотрим, кросс-регистраторного общего кадра тоже больше нет — см.
# docstring скрипта, п.5.
# ---------------------------------------------------------------------------

def test_build_combined_snapshot_returns_false_for_empty_list(dvr_snapshot_module, tmp_path):
    out = os.path.join(str(tmp_path), "combined.jpg")
    assert dvr_snapshot_module._build_combined_snapshot([], out) is False
    assert not os.path.exists(out)


def test_build_combined_snapshot_invokes_ffmpeg_with_scale_and_xstack(dvr_snapshot_module, tmp_path, monkeypatch):
    cam1 = os.path.join(str(tmp_path), "cam1.jpg")
    cam2 = os.path.join(str(tmp_path), "cam2.jpg")
    for p in (cam1, cam2):
        with open(p, "wb") as fh:
            fh.write(b"x")
    out = os.path.join(str(tmp_path), "combined.jpg")

    captured = {}

    class FakeResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, capture_output, timeout):
        captured["cmd"] = cmd
        output_path = cmd[-1]
        assert output_path.endswith(".jpg"), f"ffmpeg получит выходной файл без распознаваемого расширения: {output_path}"
        with open(output_path, "wb") as fh:
            fh.write(b"fake-combined-bytes")
        return FakeResult()

    monkeypatch.setattr(dvr_snapshot_module.subprocess, "run", fake_run)

    ok = dvr_snapshot_module._build_combined_snapshot([cam1, cam2], out)

    assert ok is True
    assert os.path.exists(out)
    with open(out, "rb") as fh:
        assert fh.read() == b"fake-combined-bytes"

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert cam1 in cmd and cam2 in cmd
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=" not in filter_complex  # без сжатия — каждая ячейка в исходном разрешении
    assert "xstack=inputs=2" in filter_complex
    assert "layout=0_0|w0_0" in filter_complex  # символьная ссылка на реальную ширину первого тайла, не литеральное число
    assert cmd[cmd.index("-map") + 1] == "[out]"


def test_build_combined_snapshot_returns_false_and_cleans_tmp_on_ffmpeg_failure(dvr_snapshot_module, tmp_path, monkeypatch):
    cam1 = os.path.join(str(tmp_path), "cam1.jpg")
    with open(cam1, "wb") as fh:
        fh.write(b"x")
    out = os.path.join(str(tmp_path), "combined.jpg")

    class FakeResult:
        returncode = 1
        stderr = b"boom"

    def fake_run(cmd, capture_output, timeout):
        with open(cmd[-1], "wb") as fh:
            fh.write(b"partial")
        return FakeResult()

    monkeypatch.setattr(dvr_snapshot_module.subprocess, "run", fake_run)

    ok = dvr_snapshot_module._build_combined_snapshot([cam1], out)

    assert ok is False
    assert not os.path.exists(out)
    root, ext = os.path.splitext(out)
    assert not os.path.exists(f"{root}.tmp{ext}")


def test_update_combined_snapshot_builds_montage_and_history(app, db, dvr_snapshot_module, monkeypatch, tmp_path):
    """_update_combined_snapshot (вызывается из main() после съёмки всех
    камер) должна построить общий смонтированный кадр и отложить его
    копию в историю — тем же принципом, что и у кадра отдельной камеры
    (см. _capture). main() тут не участвует: он создаёт свой собственный
    Flask-app через create_app(), из-за чего monkeypatch конфига
    pytest-фикстуры app на него не подействовал бы (см. docstring
    _update_combined_snapshot)."""
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera1 = make_camera(db, recorder, label="Камера 1", channel=1)
    camera2 = make_camera(db, recorder, label="Камера 2", channel=2)
    db.commit()

    for c in (camera1, camera2):
        p = snapshot_path(c.recorder_id, c.id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"fake-jpeg-bytes")

    class FakeResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, capture_output, timeout):
        with open(cmd[-1], "wb") as fh:
            fh.write(b"fake-combined-bytes")
        return FakeResult()

    monkeypatch.setattr(dvr_snapshot_module.subprocess, "run", fake_run)
    monkeypatch.setattr(dvr_snapshot_module, "_utcnow", lambda: dt.datetime(2026, 9, 5, 12, 0, 0))

    dvr_snapshot_module._update_combined_snapshot(recorder.id, [camera1, camera2])

    assert os.path.exists(combined_snapshot_path(recorder.id))
    assert os.path.exists(os.path.join(combined_history_dir(recorder.id), "20260905_120000.jpg"))


def test_update_combined_snapshot_noop_when_no_camera_files_exist(app, db, dvr_snapshot_module, monkeypatch, tmp_path):
    """Если ни у одной камеры на диске вообще нет кадра — строить общий
    смонтированный кадр не из чего, не пытаемся (и не падаем)."""
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder = make_recorder(db)
    camera = make_camera(db, recorder)
    db.commit()

    dvr_snapshot_module._update_combined_snapshot(recorder.id, [camera])

    assert not os.path.exists(combined_snapshot_path(recorder.id))


def test_update_combined_snapshot_scopes_to_its_own_recorder(app, db, dvr_snapshot_module, monkeypatch, tmp_path):
    """Кадры камер другого регистратора не должны попадать в сетку ЭТОГО
    регистратора — сырьё (image_paths) собирается только из переданного
    списка камер, не из всех, что есть в БД."""
    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    recorder1 = make_recorder(db, name="Регистратор А")
    recorder2 = make_recorder(db, name="Регистратор Б")
    camera1 = make_camera(db, recorder1, label="Камера А1", channel=1)
    camera2 = make_camera(db, recorder2, label="Камера Б1", channel=1)
    db.commit()

    for c in (camera1, camera2):
        p = snapshot_path(c.recorder_id, c.id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"fake-jpeg-bytes")

    captured = {}

    class FakeResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, capture_output, timeout):
        captured["cmd"] = cmd
        with open(cmd[-1], "wb") as fh:
            fh.write(b"fake-combined-bytes")
        return FakeResult()

    monkeypatch.setattr(dvr_snapshot_module.subprocess, "run", fake_run)
    monkeypatch.setattr(dvr_snapshot_module, "_utcnow", lambda: dt.datetime(2026, 9, 5, 12, 0, 0))

    dvr_snapshot_module._update_combined_snapshot(recorder1.id, [camera1])

    assert os.path.exists(combined_snapshot_path(recorder1.id))
    assert not os.path.exists(combined_snapshot_path(recorder2.id))
    assert snapshot_path(camera2.recorder_id, camera2.id) not in captured["cmd"]
