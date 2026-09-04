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
import importlib.util
import os

import pytest

from app.models import DvrRecorder, DvrCamera
from app.bank_api import crypto


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
