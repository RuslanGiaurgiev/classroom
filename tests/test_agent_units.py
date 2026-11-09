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


# ---------- Поиск учителя в «необычных» сетях (точка доступа телефона) ----------

def test_broadcast_targets_cover_small_hotspot_networks(monkeypatch):
    from agent import discovery
    monkeypatch.setattr(discovery, "_local_ips", lambda: ["172.20.10.3"])     # iPhone: сеть /28
    t = discovery._broadcast_targets()
    assert "172.20.10.15" in t and "172.20.10.255" in t and "255.255.255.255" in t
    sweep = discovery.sweep_targets()
    assert len(sweep) == 253 and "172.20.10.1" in sweep and "172.20.10.3" not in sweep
    monkeypatch.setattr(discovery, "_local_ips", lambda: ["8.8.8.8"])         # не частная сеть — не опрашиваем
    assert discovery.sweep_targets() == []


async def test_discovery_survives_port_unreachable_errors():
    """Ответы «порт закрыт» (на Windows — ошибки сокета) не мешают получить ответ учителя."""
    import socket as s
    from agent.discovery import broadcast_discover
    from teacher.udp_discovery import start_responder
    closed = s.socket(s.AF_INET, s.SOCK_DGRAM)
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()                                   # порт, на котором никто не слушает
    tr = await start_responder("tid-1", 0, lambda: 9999)
    port = tr.get_extra_info("sockname")[1]
    try:
        found = await broadcast_discover(port, timeout=0.6, targets=["127.0.0.1"])
        assert [(f["ip"], f["teacher_id"]) for f in found] == [("127.0.0.1", "tid-1")]
        assert await broadcast_discover(closed_port, timeout=0.4, targets=["127.0.0.1"]) == []
    finally:
        tr.close()


def test_state_remembers_several_teacher_addresses(tmp_path):
    import json
    from agent.platform.fake import FakePlatform
    from agent.state import AgentState
    p = FakePlatform(tmp_path)
    st = AgentState("tid", "5", "192.168.3.96", 8765, "tok")
    st.remember("172.20.10.3", 8765)       # точка доступа
    st.remember("192.168.3.96", 8765)      # снова роутер
    st.save(p)
    loaded = AgentState.load(p)
    assert loaded.host == "192.168.3.96" and loaded.known_hosts == [("192.168.3.96", 8765), ("172.20.10.3", 8765)]
    # Состояние старой версии (без known_hosts) читается
    data = json.loads(AgentState.path(p).read_text())
    data.pop("known_hosts")
    AgentState.path(p).write_text(json.dumps(data))
    assert AgentState.load(p).known_hosts == []
    for i in range(10):
        loaded.remember(f"10.0.0.{i}", 8765)
    assert len(loaded.known_hosts) == AgentState.MAX_KNOWN
