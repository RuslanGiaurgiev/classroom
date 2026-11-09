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


# ---------- Несколько учителей у одного ноутбука ----------

async def _teacher(tmp_path, name):
    from teacher.server import TeacherServer
    from teacher.storage import Storage
    st = Storage(tmp_path / name)
    st.update_settings(projects_root=str(tmp_path / f"projects-{name}"))
    srv = TeacherServer(st, host_ip_fn=lambda: "127.0.0.1")
    await srv.start("127.0.0.1", 0)
    return srv


def _hub(platform, config, find_port, **kw):
    from agent.client import AgentClient, Backoff
    from agent.core import AgentCore
    from agent.hub import AgentHub
    core = AgentCore(platform)
    hub = AgentHub(platform, core, config, discover_port=find_port, discover_targets=["127.0.0.1"],
                   watch_interval=0.2, poll_interval=0.05, **kw,
                   make_client=lambda st: AgentClient(platform, st, core=core, use_mdns=False,
                                                      backoff=Backoff(initial=0.05, maximum=0.2)))
    return hub


def _online(srv, number):
    return any(l["number"] == number and l["online"] for l in srv.state_snapshot()["laptops"])


async def test_laptop_stays_connected_to_two_teachers(tmp_path):
    """Привязали ко второму учителю — с первым связь не пропала; отвязал первый — второй остался."""
    import asyncio

    from agent.platform.fake import FakePlatform
    from agent.state import AgentConfig
    from tests.test_integration import pair, ui, wait_for

    a, b = await _teacher(tmp_path, "A"), await _teacher(tmp_path, "B")
    try:
        platform = FakePlatform(tmp_path / "agent", hostname="SCHOOL-PC-12")
        await pair(a, platform, "12")
        hub = _hub(platform, AgentConfig.load(platform), b.port)
        task = asyncio.create_task(hub.run())
        await wait_for(lambda: _online(a, "12"), timeout=10)

        # Ноутбук сам постучался ко второму учителю — тот нажимает «Подключить»
        await wait_for(lambda: b.state_snapshot()["pair_requests"], timeout=10)
        req = b.state_snapshot()["pair_requests"][0]
        assert req["hostname"] == "SCHOOL-PC-12"
        await ui(b, "POST", f"/ui/api/pair-requests/{req['id']}/approve", json={"number": "3"})
        await wait_for(lambda: _online(b, "3") and _online(a, "12"), timeout=10)
        assert {x.teacher_id for x in AgentConfig.load(platform).accounts} == {a.storage.teacher_id, b.storage.teacher_id}

        # Команды от обоих учителей выполняются; «ведущий» — тот, кто прислал команду последним
        await b.send_command(["3"], "show_message", {"text": "Урок у Б"})
        await wait_for(lambda: platform.current_overlay() and platform.current_overlay().spec["text"] == "Урок у Б")
        assert hub.base_url.endswith(f":{b.port}")
        await a.send_command(["12"], "show_message", {"text": "Урок у А"})
        await wait_for(lambda: platform.current_overlay().spec["text"] == "Урок у А")
        assert hub.base_url.endswith(f":{a.port}")

        # Первый учитель отвязал ноутбук — со вторым связь остаётся
        await ui(a, "DELETE", "/ui/api/laptops/12")
        await wait_for(lambda: [x.teacher_id for x in AgentConfig.load(platform).accounts] == [b.storage.teacher_id],
                       timeout=10)
        await asyncio.sleep(0.5)
        assert _online(b, "3")
        assert hub.base_url.endswith(f":{b.port}")      # ведущим стал оставшийся учитель
        await hub.stop()
        await asyncio.wait_for(task, 10)
    finally:
        await a.stop()
        await b.stop()


async def test_files_go_to_the_teacher_running_the_lesson(tmp_path):
    """Работы ученика получает ведущий учитель, а не оба сразу."""
    import asyncio
    import hashlib
    import os

    from agent.collector import Collector
    from agent.files import ReadinessTracker
    from agent.platform.fake import FakePlatform
    from agent.state import AgentConfig
    from tests.test_integration import pair, wait_for

    a, b = await _teacher(tmp_path, "A"), await _teacher(tmp_path, "B")
    try:
        platform = FakePlatform(tmp_path / "agent", hostname="PC-5")
        await pair(a, platform, "5")
        await pair(b, platform, "5")          # первая привязка не потерялась
        config = AgentConfig.load(platform)
        assert len(config.accounts) == 2
        hub = _hub(platform, config, b.port, watch=False)
        hub.core.collector = Collector(platform, tracker=ReadinessTracker(stable_seconds=0.2), use_watchdog=False,
                                       full_scan_every=0.3)
        hub.core.collector.client = hub
        task = asyncio.create_task(hub.run())
        await wait_for(lambda: _online(a, "5") and _online(b, "5"), timeout=10)

        await b.send_command(["5"], "hide_message", {})     # урок ведёт Б
        await wait_for(lambda: hub.base_url and hub.base_url.endswith(f":{b.port}"))
        data = os.urandom(5000)
        p = platform.known_folder("downloads") / "Маша кот.sb3"
        p.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        await wait_for(lambda: b.db.find_received("5", sha), timeout=10)
        assert a.db.find_received("5", sha) is None          # первому учителю копия не ушла
        await hub.stop()
        await asyncio.wait_for(task, 10)
    finally:
        await a.stop()
        await b.stop()


async def test_declined_teacher_is_not_pestered(tmp_path):
    """«Отклонить» — ноутбук больше не просится к этому учителю (12 часов), даже после перезапуска."""
    import asyncio

    from agent.platform.fake import FakePlatform
    from agent.state import AgentConfig
    from tests.test_integration import ui, wait_for

    b = await _teacher(tmp_path, "B")
    try:
        platform = FakePlatform(tmp_path / "agent", hostname="PC-7")
        hub = _hub(platform, AgentConfig.load(platform), b.port)
        task = asyncio.create_task(hub.run())
        await wait_for(lambda: b.state_snapshot()["pair_requests"], timeout=10)
        await ui(b, "POST", f"/ui/api/pair-requests/{b.state_snapshot()['pair_requests'][0]['id']}/reject")
        await wait_for(lambda: (platform.data_dir() / "declined_teachers.json").exists(), timeout=10)
        await hub.stop()
        await asyncio.wait_for(task, 10)

        hub2 = _hub(platform, AgentConfig.load(platform), b.port)   # «перезапуск» агента
        task2 = asyncio.create_task(hub2.run())
        await asyncio.sleep(1.0)
        assert b.state_snapshot()["pair_requests"] == [] and not hub2._offers
        await hub2.stop()
        await asyncio.wait_for(task2, 10)
    finally:
        await b.stop()


def test_old_single_teacher_state_migrates(tmp_path):
    """Ноутбук со старой версией (один учитель в state.json) — после обновления учитель не теряется."""
    import json

    from agent.platform.fake import FakePlatform
    from agent.state import AgentConfig, AgentState
    p = FakePlatform(tmp_path)
    AgentState("tid-A", "4", "10.0.0.2", 8765, "tok-A").save(p)
    AgentConfig.path(p).unlink()                        # как у старой версии: только state.json
    cfg = AgentConfig.load(p)
    assert [(x.teacher_id, x.token) for x in cfg.accounts] == [("tid-A", "tok-A")]
    cfg.add(AgentState("tid-B", "9", "10.0.0.3", 8765, "tok-B")).save(p)
    cfg.add(AgentState("tid-A", "4", "10.0.0.5", 8765, "tok-A2")).save(p)   # повторная привязка к А — замена
    loaded = AgentConfig.load(p)
    assert [(x.teacher_id, x.token, x.host) for x in loaded.accounts] == \
        [("tid-B", "tok-B", "10.0.0.3"), ("tid-A", "tok-A2", "10.0.0.5")]
    assert "tok-B" not in AgentConfig.path(p).read_text()            # токены зашифрованы
    data = json.loads(AgentConfig.path(p).read_text())
    data["accounts"][0]["token_protected"] = "испорчено"
    AgentConfig.path(p).write_text(json.dumps(data))
    assert [x.teacher_id for x in AgentConfig.load(p).accounts] == ["tid-A"]   # повреждённый не ломает остальных


from tests.test_integration import teacher  # noqa: E402,F401  (фикстура сервера учителя)
