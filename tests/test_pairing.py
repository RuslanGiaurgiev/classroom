"""Запросы на подключение (учитель) и разбор вручную введённого адреса (агент)."""

import pytest

from agent.pairing import parse_manual_address
from teacher.pairing import REQUEST_TTL_SECONDS, PairRequestManager


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_request_lifecycle():
    pm = PairRequestManager()
    r = pm.create("PC-1", "Маша", "secret-123456789", "10.0.0.5")
    assert len(r.match_code) == 2 and pm.pending() == [r]
    assert pm.poll(r.id, "secret-123456789").status == "pending"
    assert pm.poll(r.id, "wrong") is None
    pm.approve(r.id, "5", "TOKEN")
    assert pm.pending() == []
    got = pm.poll(r.id, "secret-123456789")
    assert (got.status, got.token, got.laptop_number) == ("approved", "TOKEN", "5")
    assert pm.poll(r.id, "secret-123456789") is None  # токен отдаётся один раз


def test_cannot_approve_twice_or_after_reject():
    pm = PairRequestManager()
    r = pm.create("PC-1", "", "s" * 20, "10.0.0.5")
    assert pm.reject(r.id)
    assert pm.approve(r.id, "1", "T") is None


def test_repeat_request_from_same_pc_replaces_old():
    pm = PairRequestManager()
    r1 = pm.create("PC-1", "", "s" * 20, "10.0.0.5")
    r2 = pm.create("PC-1", "", "t" * 20, "10.0.0.5")
    assert pm.pending() == [r2] and pm.get(r1.id) is None


def test_match_codes_unique_among_pending():
    pm = PairRequestManager()
    codes = {pm.create(f"PC-{i}", "", "s" * 20, f"10.0.0.{i}").match_code for i in range(30)}
    assert len(codes) == 30


def test_requests_expire():
    clock = Clock()
    pm = PairRequestManager(clock=clock)
    r = pm.create("PC-1", "", "s" * 20, "10.0.0.5")
    clock.t += REQUEST_TTL_SECONDS + 1
    assert pm.pending() == [] and pm.approve(r.id, "1", "T") is None


@pytest.mark.parametrize("text, expected", [
    ("192.168.3.96", ("192.168.3.96", 8765)),
    (" 192.168.3.96 ", ("192.168.3.96", 8765)),
    ("192,168,3,96", ("192.168.3.96", 8765)),
    ("192ю168ю3ю96", ("192.168.3.96", 8765)),          # русская раскладка
    ("http://192.168.3.96:8765/", ("192.168.3.96", 8765)),
    ("192.168.3.96:9000", ("192.168.3.96", 9000)),
    ("192.168.3", None),
    ("300.1.1.1", None),
    ("mac.local", None),
    ("", None),
])
def test_manual_address(text, expected):
    assert parse_manual_address(text) == expected


async def test_unpaired_laptop_knocks_again_and_teacher_reconnects_it(teacher, tmp_path):  # noqa: F811
    """Отвязали → ноутбук сам появляется в «Ноутбуки рядом» с прежним номером → «Подключить» → снова на связи.
    Отвязали и «Отклонить» → агент перестаёт стучаться и завершается."""
    import asyncio

    from agent.client import AgentClient, Backoff
    from agent.main import run_agent
    from agent.pairing import PairingFlow
    from agent.platform.fake import FakePlatform
    from tests.test_integration import pair, ui, wait_for

    platform = FakePlatform(tmp_path / "agent-12", hostname="SCHOOL-PC-12")
    state = await pair(teacher, platform, "12")
    clients = []

    def make_client(st, core):
        c = AgentClient(platform, st, use_mdns=False, backoff=Backoff(initial=0.05, maximum=0.2), core=core)
        clients.append(c)
        return c

    def make_flow():
        return PairingFlow(platform, discover_port=teacher.port, discover_targets=["127.0.0.1"], poll_interval=0.05)

    agent = asyncio.create_task(asyncio.to_thread(run_agent, platform, state, make_client, make_flow))
    await wait_for(lambda: teacher.state_snapshot()["laptops"][0]["online"], timeout=10)

    await ui(teacher, "DELETE", "/ui/api/laptops/12")
    await wait_for(lambda: teacher.state_snapshot()["pair_requests"], timeout=10)
    req = teacher.state_snapshot()["pair_requests"][0]
    assert req["hostname"] == "SCHOOL-PC-12" and req["former_number"] == "12"
    status, _ = await ui(teacher, "POST", f"/ui/api/pair-requests/{req['id']}/approve", json={"number": "12"})
    assert status == 200
    await wait_for(lambda: any(l["online"] for l in teacher.state_snapshot()["laptops"]), timeout=10)
    assert len(clients) == 2 and clients[1].state.laptop_number == "12"

    await ui(teacher, "DELETE", "/ui/api/laptops/12")
    await wait_for(lambda: teacher.state_snapshot()["pair_requests"], timeout=10)
    await ui(teacher, "POST", f"/ui/api/pair-requests/{teacher.state_snapshot()['pair_requests'][0]['id']}/reject")
    assert await asyncio.wait_for(agent, 10) == 0          # отклонили — агент завершился
    from agent.state import AgentState
    assert AgentState.load(platform) is None

from tests.test_integration import teacher  # noqa: E402,F401  (фикстура сервера учителя)
