import pytest


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    """Тесты никогда не трогают настоящую Связку ключей macOS."""
    import teacher.yadisk as yd
    monkeypatch.setattr(yd, "KeyringTokenStore", yd.MemoryTokenStore)
