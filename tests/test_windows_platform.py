"""Проверки настоящей Windows-платформы. Запускаются только на Windows (в GitHub Actions)."""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="только Windows")


@pytest.fixture
def wp():
    from agent.platform.windows import WindowsPlatform
    return WindowsPlatform()


def test_dpapi_roundtrip(wp):
    secret = "токен-🙂".encode()
    enc = wp.protect(secret)
    assert enc != secret
    assert wp.unprotect(enc) == secret


def test_known_folders_exist(wp):
    for name in ("downloads", "desktop"):
        assert wp.known_folder(name).is_dir()


def test_single_instance_mutex(wp):
    from agent.platform.windows import WindowsPlatform
    assert wp.acquire_single_instance()
    assert not WindowsPlatform().acquire_single_instance()


def test_autorun_registry(wp, tmp_path):
    import winreg
    from agent.platform.windows import APP_NAME, RUN_KEY
    wp.install_autorun(tmp_path / "agent.exe")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        value, _ = winreg.QueryValueEx(k, APP_NAME)
    assert value == f'"{tmp_path / "agent.exe"}"'
    wp.uninstall_autorun()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        with pytest.raises(FileNotFoundError):
            winreg.QueryValueEx(k, APP_NAME)


def test_detect_browsers_runs(wp):
    assert set(wp.detect_browsers()) <= {"chrome", "yandex", "edge"}


def test_hidden_attribute(wp, tmp_path):
    import ctypes
    f = tmp_path / "secret.txt"
    f.write_text("x")
    assert not wp.is_hidden(f)
    ctypes.windll.kernel32.SetFileAttributesW(str(f), 0x2)   # FILE_ATTRIBUTE_HIDDEN
    assert wp.is_hidden(f)


def test_close_missing_process(wp):
    assert "не было запущено" in wp.close_processes(["no_such_program_zz.exe"])


def test_overlay_starts_and_closes(wp, monkeypatch, tmp_path):
    """Полноэкранное окно запускается отдельным процессом и закрывается по команде."""
    import time
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    h = wp.spawn_overlay({"mode": "message", "text": "Тест", "seconds": 0})
    time.sleep(4)
    assert h.poll() is None, "окно должно быть открыто"
    h.terminate()
    for _ in range(50):
        if h.poll() is not None:
            break
        time.sleep(0.1)
    assert h.poll() is not None, "окно должно закрыться"


def test_running_exe_can_be_renamed(tmp_path):
    """На этом держится самообновление: работающий .exe можно переименовать (но не удалить)."""
    import os
    import shutil
    import subprocess
    import sys
    import time
    exe = tmp_path / "copy.exe"
    shutil.copy2(sys.executable, exe)
    proc = subprocess.Popen([str(exe), "-c", "import time; time.sleep(20)"])
    try:
        time.sleep(1)
        assert proc.poll() is None
        os.replace(exe, tmp_path / "copy-old.exe")
        shutil.copy2(sys.executable, exe)          # на старое место кладём «новую версию»
        assert exe.exists() and (tmp_path / "copy-old.exe").exists()
    finally:
        proc.kill()
