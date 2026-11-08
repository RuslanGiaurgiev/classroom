"""
Сквозные тесты: настоящий сервер учителя + настоящий клиент агента
с фейковой платформой. Всё работает на 127.0.0.1.
"""

import asyncio
import json

import aiohttp
import pytest

from agent.client import AgentClient, Backoff
from agent.pairing import PairingCancelled, PairingFlow
from agent.platform.fake import FakePlatform
from agent.state import AgentState
from shared.protocol import Message, MsgType
from teacher.server import TeacherServer
from teacher.storage import Storage


async def wait_for(cond, timeout=5.0):
    """Ждать, пока условие станет истинным."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("не дождались условия")
        await asyncio.sleep(0.02)


@pytest.fixture
async def teacher(tmp_path):
    server = TeacherServer(Storage(tmp_path / "teacher"), host_ip_fn=lambda: "127.0.0.1")
    await server.start("127.0.0.1", 0)
    yield server
    await server.stop()


async def ui(server, method, path, **kw):
    async with aiohttp.ClientSession() as s:
        async with s.request(method, f"http://127.0.0.1:{server.port}{path}",
                             headers={"X-UI-Token": server.ui_token}, **kw) as r:
            return r.status, await r.json()


async def approve_when_requested(server, hostname, number):
    """Ждём запрос от ноутбука и нажимаем «Принять», как учитель в панели."""
    await wait_for(lambda: any(r.hostname == hostname for r in server.pairing.pending()))
    req = next(r for r in server.pairing.pending() if r.hostname == hostname)
    status, _ = await ui(server, "POST", f"/ui/api/pair-requests/{req.id}/approve", json={"number": number})
    assert status == 200


async def pair(server, platform, number):
    flow = PairingFlow(platform, discover_port=server.port, discover_targets=["127.0.0.1"], poll_interval=0.05)
    approver = asyncio.create_task(approve_when_requested(server, platform.hostname(), number))
    state = await asyncio.wait_for(flow.run(), 10)
    await approver
    return state


async def start_agent(server, tmp_path, number="1", **platform_kw):
    platform_kw.setdefault("hostname", f"PC-{number}")
    platform = FakePlatform(tmp_path / f"agent-{number}", **platform_kw)
    state = await pair(server, platform, number)
    client = AgentClient(platform, state, use_mdns=False, backoff=Backoff(initial=0.05, maximum=0.2))
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.connected.wait(), 5)
    return platform, client, task


async def stop_agent(client, task):
    await client.stop()
    await asyncio.wait_for(task, 5)


async def test_pair_connect_status_and_open_url(teacher, tmp_path):
    platform, client, task = await start_agent(teacher, tmp_path, "3", username="Маша", browsers=["yandex"])
    snap = teacher.state_snapshot()
    assert snap["laptops"] == [{
        "number": "3", "online": True, "username": "Маша", "hostname": "PC-3", "ip": "127.0.0.1",
        "agent_version": snap["laptops"][0]["agent_version"], "browsers": ["yandex"],
        "pending_files": 0, "last_seen": snap["laptops"][0]["last_seen"],
    }]

    # Панель подписана на события — проверим, что придёт результат команды
    events = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"http://127.0.0.1:{teacher.port}/ui/ws?t={teacher.ui_token}") as ui:
            await ui.receive()  # начальное состояние
            r = await teacher.send_command("all", "open_urls", {"urls": ["scratch.mit.edu"], "browser": "chrome"})
            assert r["statuses"] == {"3": "sent"}
            while True:
                ev = json.loads((await asyncio.wait_for(ui.receive(), 5)).data)
                events.append(ev)
                if ev["kind"] == "result":
                    break
    assert platform.actions == [("open_urls", ["https://scratch.mit.edu"], "chrome")]
    assert events[-1]["ok"] and "не установлен" in events[-1]["message"]  # Chrome нет → браузер по умолчанию

    await stop_agent(client, task)
    await wait_for(lambda: not teacher.state_snapshot()["laptops"][0]["online"])


async def test_offline_laptop_reported(teacher, tmp_path):
    platform, client, task = await start_agent(teacher, tmp_path, "1")
    await stop_agent(client, task)
    await wait_for(lambda: "1" not in teacher.agents)
    r = await teacher.send_command(["1"], "open_urls", {"urls": ["a.ru"]})
    assert r["statuses"] == {"1": "offline"}


async def test_laptop_found_by_broadcast_and_approved(teacher, tmp_path):
    """Ноутбук сам находит учителя (UDP), ждёт «Принять», получает токен — ничего не вводя."""
    platform = FakePlatform(tmp_path / "a", hostname="PC-7", username="Петя")
    statuses = []
    flow = PairingFlow(platform, on_status=statuses.append, discover_port=teacher.port,
                       discover_targets=["127.0.0.1"], poll_interval=0.05)
    task = asyncio.create_task(flow.run())
    await wait_for(lambda: teacher.state_snapshot()["pair_requests"])
    req = teacher.state_snapshot()["pair_requests"][0]
    assert (req["hostname"], req["username"]) == ("PC-7", "Петя")
    # Пока учитель не нажал «Принять», ноутбук не привязан и токена не получил
    await asyncio.sleep(0.2)
    assert not task.done() and teacher.storage.laptops() == {}
    waiting = [s for s in statuses if s.stage == "waiting"]
    assert waiting and waiting[0].match_code == req["match_code"]  # одно и то же число на обоих экранах

    status, _ = await ui(teacher, "POST", f"/ui/api/pair-requests/{req['id']}/approve", json={"number": "7"})
    assert status == 200
    state = await asyncio.wait_for(task, 5)
    assert state.laptop_number == "7" and state.teacher_id == teacher.storage.teacher_id
    assert AgentState.load(platform).token == state.token
    assert teacher.storage.find_laptop_by_token(state.token) == "7"
    assert teacher.state_snapshot()["pair_requests"] == []


async def test_rejected_request(teacher, tmp_path):
    flow = PairingFlow(FakePlatform(tmp_path / "a"), discover_port=teacher.port,
                       discover_targets=["127.0.0.1"], poll_interval=0.05)
    task = asyncio.create_task(flow.run())
    await wait_for(lambda: teacher.state_snapshot()["pair_requests"])
    rid = teacher.state_snapshot()["pair_requests"][0]["id"]
    await ui(teacher, "POST", f"/ui/api/pair-requests/{rid}/reject")
    with pytest.raises(PairingCancelled):
        await asyncio.wait_for(task, 5)
    assert teacher.storage.laptops() == {}


async def test_token_only_for_request_owner(teacher):
    """Чужой не может забрать токен, зная id запроса, — нужен секрет, который знает только ноутбук."""
    async with aiohttp.ClientSession() as s:
        base = f"http://127.0.0.1:{teacher.port}"
        async with s.post(f"{base}/api/pair-request", json={"secret": "x" * 32, "hostname": "PC"}) as r:
            rid = (await r.json())["request_id"]
        await ui(teacher, "POST", f"/ui/api/pair-requests/{rid}/approve", json={"number": "1"})
        async with s.get(f"{base}/api/pair-request/{rid}", headers={"X-Pair-Secret": "y" * 32}) as r:
            assert r.status == 404 and "token" not in await r.json()
        async with s.get(f"{base}/api/pair-request/{rid}", headers={"X-Pair-Secret": "x" * 32}) as r:
            assert (await r.json())["status"] == "approved"
        # Токен выдаётся один раз
        async with s.get(f"{base}/api/pair-request/{rid}", headers={"X-Pair-Secret": "x" * 32}) as r:
            assert r.status == 404


async def test_manual_address_fallback(teacher, tmp_path):
    """Если широковещательный поиск не работает, можно ввести адрес Mac вручную."""
    platform = FakePlatform(tmp_path / "a", hostname="PC-9")
    flow = PairingFlow(platform, discover_port=1, discover_targets=["127.0.0.1"], poll_interval=0.05)
    task = asyncio.create_task(flow.run())
    await asyncio.sleep(0.5)
    assert teacher.state_snapshot()["pair_requests"] == []  # не нашли
    flow.manual_address = ("127.0.0.1", teacher.port)
    approver = asyncio.create_task(approve_when_requested(teacher, "PC-9", "9"))
    state = await asyncio.wait_for(task, 10)
    await approver
    assert state.laptop_number == "9"


async def test_requests_without_valid_token_rejected(teacher):
    async with aiohttp.ClientSession() as s:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                await s.ws_connect(f"http://127.0.0.1:{teacher.port}/ws/agent", headers=headers)
            assert e.value.status == 401
        # API панели без токена
        async with s.get(f"http://127.0.0.1:{teacher.port}/ui/api/state") as r:
            assert r.status == 401


async def test_wrong_token_marks_agent_unpaired(teacher, tmp_path):
    platform = FakePlatform(tmp_path / "x")
    state = AgentState("tid", "9", "127.0.0.1", teacher.port, "not-a-real-token")
    client = AgentClient(platform, state, use_mdns=False, backoff=Backoff(initial=0.05))
    await asyncio.wait_for(client.run(), 5)
    assert client.unpaired


async def test_unpair_disconnects_agent(teacher, tmp_path):
    platform, client, task = await start_agent(teacher, tmp_path, "4")
    async with aiohttp.ClientSession() as s:
        async with s.delete(f"http://127.0.0.1:{teacher.port}/ui/api/laptops/4",
                            headers={"X-UI-Token": teacher.ui_token}) as r:
            assert (await r.json())["removed"]
    await asyncio.wait_for(task, 5)  # run() завершился сам
    assert client.unpaired
    assert teacher.state_snapshot()["laptops"] == []


async def test_agent_reconnects_after_teacher_restart(tmp_path):
    storage = Storage(tmp_path / "teacher")
    server = TeacherServer(storage, host_ip_fn=lambda: "127.0.0.1")
    port = await server.start("127.0.0.1", 0)
    platform, client, task = await start_agent(server, tmp_path, "2")

    await server.stop()  # Mac «перезапустили»
    await wait_for(lambda: not client.connected.is_set())

    server2 = TeacherServer(Storage(tmp_path / "teacher"), host_ip_fn=lambda: "127.0.0.1")
    await server2.start("127.0.0.1", port)
    try:
        await asyncio.wait_for(client.connected.wait(), 5)
        await wait_for(lambda: "2" in server2.agents)
        await stop_agent(client, task)
    finally:
        await server2.stop()


async def test_agent_refuses_forbidden_command_even_from_teacher(teacher, tmp_path):
    """Даже если по сети пришла «плохая» команда в обход проверки учителя — агент её не выполнит."""
    platform, client, task = await start_agent(teacher, tmp_path, "5")
    results = []
    orig = teacher._on_agent_message

    async def spy(conn, msg):
        if msg.type == MsgType.RESULT:
            results.append(msg.payload)
        await orig(conn, msg)

    teacher._on_agent_message = spy
    ws = teacher.agents["5"].ws
    for action, params in [("run_shell", {"cmd": "calc"}), ("open_urls", {"urls": ["javascript:alert(1)"]}),
                           ("open_urls", {"urls": ["file:///C:/"]})]:
        await ws.send_str(Message(MsgType.COMMAND, {"command_id": action, "action": action, "params": params}).to_json())
    await wait_for(lambda: len(results) == 3)
    assert all(not r["ok"] for r in results)
    assert platform.actions == []
    await stop_agent(client, task)


async def test_second_connection_replaces_first(teacher, tmp_path):
    platform, client, task = await start_agent(teacher, tmp_path, "6")
    state = AgentState.load(platform)
    client2 = AgentClient(platform, state, use_mdns=False, backoff=Backoff(initial=5))
    task2 = asyncio.create_task(client2.run())
    await asyncio.wait_for(client2.connected.wait(), 5)
    assert len(teacher.agents) == 1
    await client.stop()
    await client2.stop()
    await asyncio.gather(task, task2)
