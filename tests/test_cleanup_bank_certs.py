"""Осиротевшие файлы сертификатов банка: scripts/cleanup_orphan_attachments.py
(cleanup_bank_certs) и замена сертификата в app/bank_sync.py."""
import importlib.util
import os
import time

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _load_cleanup():
    spec = importlib.util.spec_from_file_location("cleanup_under_test", os.path.join(SCRIPTS_DIR, "cleanup_orphan_attachments.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_removes_only_old_unreferenced_generated_pem(tmp_path):
    cleanup = _load_cleanup()
    now = time.time()
    old = now - 3 * 86400
    names = {
        "a" * 32 + ".cert.pem": old,   # сирота, старый → удалить
        "b" * 32 + ".key.pem": old,    # сирота, старый → удалить
        "c" * 32 + ".cert.pem": old,   # на него ссылается БД
        "d" * 32 + ".key.pem": now,    # свежий — может быть ещё не закоммичен
        "bundle.pem": old,             # чужой файл — не трогаем
    }
    for name, mtime in names.items():
        path = tmp_path / name
        path.write_text("x")
        os.utime(path, (mtime, mtime))

    removed = cleanup.cleanup_bank_certs(str(tmp_path), {"c" * 32 + ".cert.pem"}, now=now)
    assert removed == ["a" * 32 + ".cert.pem", "b" * 32 + ".key.pem"]
    assert sorted(os.listdir(tmp_path)) == sorted(["c" * 32 + ".cert.pem", "d" * 32 + ".key.pem", "bundle.pem"])


def test_missing_dir_is_noop(tmp_path):
    assert _load_cleanup().cleanup_bank_certs(str(tmp_path / "nope"), set()) == []
