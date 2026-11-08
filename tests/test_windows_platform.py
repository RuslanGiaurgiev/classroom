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
