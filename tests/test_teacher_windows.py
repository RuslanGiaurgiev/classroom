"""Проверки кросс-платформенности приложения учителя (логика, без настоящего Windows)."""

import sys

import pytest

from teacher import osbridge


def test_alert_escapes_quotes_and_backslashes():
    assert osbridge._esc('путь "C:\\школа"') == 'путь \\"C:\\\\школа\\"'


def test_token_store_selected_by_platform(tmp_path, monkeypatch):
    from teacher.yadisk import KeyringTokenStore, default_token_store
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(default_token_store(tmp_path), KeyringTokenStore)
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(default_token_store(tmp_path), KeyringTokenStore)


def test_data_dir_is_localappdata_on_windows(tmp_path, monkeypatch):
    import teacher.storage as storage
    monkeypatch.delenv("CLASSROOM_TEACHER_DATA", raising=False)
    monkeypatch.setattr(storage.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    d = storage.default_data_dir()
    assert d == tmp_path / "AppData" / "Local" / "ClassroomTeacher"
    monkeypatch.setattr(storage.sys, "platform", "darwin")
    assert "Library/Application Support/ClassroomTeacher" in str(storage.default_data_dir())


def test_env_override_wins_everywhere(tmp_path, monkeypatch):
    import teacher.storage as storage
    monkeypatch.setenv("CLASSROOM_TEACHER_DATA", str(tmp_path / "my"))
    monkeypatch.setattr(storage.sys, "platform", "win32")
    assert storage.default_data_dir() == tmp_path / "my"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI только на Windows")
def test_dpapi_token_roundtrip(tmp_path):
    from teacher.winsecret import DpapiTokenStore
    store = DpapiTokenStore(tmp_path)
    assert store.get() is None
    store.set("y0_секрет-токен-🙂")
    assert store.get() == "y0_секрет-токен-🙂"
    assert store.path.read_bytes() != "y0_секрет-токен-🙂".encode()  # зашифровано, не открытый текст
    store.delete()
    assert store.get() is None


def test_pick_dialog_delegates(monkeypatch):
    from teacher import materials
    monkeypatch.setattr(osbridge, "pick_dialog", lambda kind: [f"<{kind}>"])
    assert materials.pick_dialog("files") == ["<files>"]
