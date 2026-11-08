"""Задержка переподключения и сохранение состояния агента."""

from agent.client import Backoff
from agent.platform.fake import FakePlatform
from agent.state import AgentState


def test_backoff_grows_and_caps():
    b = Backoff(initial=1, maximum=30, jitter=0)
    assert [b.next_delay() for _ in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    b.reset()
    assert b.next_delay() == 1


def test_backoff_jitter_in_range():
    b = Backoff(initial=10, maximum=10, jitter=0.2)
    for _ in range(100):
        assert 8 <= b.next_delay() <= 12


def test_state_token_is_protected(tmp_path):
    p = FakePlatform(tmp_path)
    AgentState("tid", "7", "10.0.0.2", 8765, "secret-token").save(p)
    raw = AgentState.path(p).read_text()
    assert "secret-token" not in raw  # токен хранится только в зашифрованном виде
    s = AgentState.load(p)
    assert (s.laptop_number, s.token) == ("7", "secret-token")


def test_state_from_other_computer_is_ignored(tmp_path):
    AgentState("tid", "7", "10.0.0.2", 8765, "tok").save(FakePlatform(tmp_path))

    class OtherPC(FakePlatform):
        def unprotect(self, data):
            raise OSError("DPAPI: чужой пользователь")

    assert AgentState.load(OtherPC(tmp_path)) is None
