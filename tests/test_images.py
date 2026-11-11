"""Ужатие фото при загрузке (app/images.py) и разовый скрипт
scripts/shrink_uploads.py."""
import importlib.util
import io
import os

from PIL import Image
from werkzeug.datastructures import FileStorage

from app import images
from app.uploads import save_upload

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "shrink_uploads.py")


def _noisy(size, mode="RGB"):
    # Шум, чтобы JPEG не сжался в ничто и файл был «как с телефона».
    return Image.frombytes(mode, size, os.urandom(size[0] * size[1] * len(mode)))


def _jpeg(path, size=(4000, 3000), orientation=None, quality=95):
    img = _noisy(size)
    exif = Image.Exif()
    exif[0x0110] = "Phone X"  # Model
    if orientation:
        exif[0x0112] = orientation
    img.save(path, "JPEG", quality=quality, exif=exif)
    return path


def test_big_jpeg_resized_and_exif_dropped(tmp_path):
    path = _jpeg(str(tmp_path / "a.jpg"))
    before = os.path.getsize(path)
    result = images.shrink_image(path)
    assert result is not None and result[0] == before and result[1] < before
    with Image.open(path) as img:
        assert img.format == "JPEG" and max(img.size) == images.MAX_SIDE
        assert img.size == (2000, 1500)
        assert not img.getexif()
    assert not os.path.exists(path + ".shrink.tmp")


def test_orientation_applied_before_exif_dropped(tmp_path):
    # Orientation=6: снято «боком», показывать с поворотом на 90°.
    path = _jpeg(str(tmp_path / "r.jpg"), orientation=6)
    assert images.shrink_image(path) is not None
    with Image.open(path) as img:
        assert img.size == (1500, 2000)


def test_small_photo_untouched(tmp_path):
    path = str(tmp_path / "s.jpg")
    _noisy((800, 600)).save(path, "JPEG", quality=80)
    data = open(path, "rb").read()
    assert images.shrink_image(path) is None
    assert open(path, "rb").read() == data


def test_png_stays_png(tmp_path):
    path = str(tmp_path / "p.png")
    _noisy((3000, 1000)).save(path, "PNG")
    assert images.shrink_image(path) is not None
    with Image.open(path) as img:
        assert img.format == "PNG" and img.size == (2000, 667)


def test_gif_and_broken_files_left_alone(tmp_path):
    gif = str(tmp_path / "g.gif")
    _noisy((3000, 3000), "L").save(gif, "GIF")
    broken = tmp_path / "b.jpg"
    broken.write_bytes(b"\xff\xd8 not a jpeg" * 200_000)
    data = broken.read_bytes()
    assert images.shrink_image(gif) is None
    assert images.shrink_image(str(broken)) is None
    assert broken.read_bytes() == data
    assert not os.path.exists(str(broken) + ".shrink.tmp")


def test_save_upload_shrinks_photos_but_not_documents(tmp_path):
    buf = io.BytesIO()
    _noisy((4000, 3000)).save(buf, "JPEG", quality=95)
    photo = FileStorage(stream=io.BytesIO(buf.getvalue()), filename="IMG_0001.JPG")
    name = save_upload(photo, str(tmp_path))
    assert name.endswith(".jpg")
    with Image.open(tmp_path / name) as img:
        assert max(img.size) == images.MAX_SIDE

    pdf_bytes = b"%PDF-1.4 " + os.urandom(2 * 1024 * 1024)
    doc = save_upload(FileStorage(stream=io.BytesIO(pdf_bytes), filename="act.pdf"), str(tmp_path))
    assert (tmp_path / doc).read_bytes() == pdf_bytes


def _script():
    spec = importlib.util.spec_from_file_location("shrink_uploads", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_dry_run_changes_nothing_then_shrinks(tmp_path, capsys):
    folder = tmp_path / "uploads"
    folder.mkdir()
    big = _jpeg(str(folder / "big.jpg"))
    (folder / "doc.pdf").write_bytes(b"%PDF" * 1000)
    data = open(big, "rb").read()
    script = _script()

    assert script.main(["--dry-run", "--folder", str(folder)]) == 0
    assert open(big, "rb").read() == data
    assert "будет ужато файлов: 1" in capsys.readouterr().out
    assert sorted(os.listdir(folder)) == ["big.jpg", "doc.pdf"]

    assert script.main(["--folder", str(folder)]) == 0
    assert "ужато файлов: 1" in capsys.readouterr().out
    with Image.open(big) as img:
        assert max(img.size) == images.MAX_SIDE
