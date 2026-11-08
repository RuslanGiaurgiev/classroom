"""Смена номера ноутбука и обмен номерами."""

import asyncio

from tests.test_files import make_core, write
from tests.test_integration import start_agent, stop_agent, teacher, ui, wait_for  # noqa: F401


async def test_rename_keeps_history_and_connection(teacher, tmp_path):
    platform, core = make_core(tmp_path, "12")
    _, client, task = await start_agent(teacher, tmp_path, "12", core=core, platform=platform)
    write(platform.known_folder("downloads") / "кот.sb3", b"cat")
    await wait_for(lambda: teacher.db.q("SELECT 1 FROM received"), timeout=10)
    await teacher.send_command(["12"], "lock_screen", {})

    status, _ = await ui(teacher, "POST", "/ui/api/laptops/12/rename", json={"new_number": "3"})
    assert status == 200
    snap = teacher.state_snapshot()["laptops"]
    assert [(l["number"], l["online"], l["locked"]) for l in snap] == [("3", True, True)]
    assert teacher.db.confirmed_shas("3") and not teacher.db.confirmed_shas("12")   # история переехала
    # Команды по новому номеру доходят до того же ноутбука
    await teacher.send_command(["3"], "open_urls", {"urls": ["a.ru"]})
    await wait_for(lambda: any(a[0] == "open_urls" for a in platform.actions))
    # После переподключения ноутбук остаётся под новым номером (узнаём по токену)
    await stop_agent(client, task)
    from agent.client import AgentClient, Backoff
    from agent.state import AgentState
    client2 = AgentClient(platform, AgentState.load(platform), use_mdns=False, backoff=Backoff(initial=0.05), core=core)
    task2 = asyncio.create_task(client2.run())
    await wait_for(lambda: "3" in teacher.agents)
    assert "12" not in teacher.agents
    # «Конец урока» по новому номеру убирает файл, полученный под старым номером
    await teacher.end_lesson.run(["3"])
    assert teacher.end_lesson.progress["3"]["stage"] == "done", teacher.end_lesson.progress
    assert not (platform.known_folder("downloads") / "кот.sb3").exists()
    await stop_agent(client2, task2)


async def test_busy_number_and_swap(teacher, tmp_path):
    p1, c1 = make_core(tmp_path, "1")
    p2, c2 = make_core(tmp_path, "2")
    _, cl1, t1 = await start_agent(teacher, tmp_path, "1", core=c1, platform=p1)
    _, cl2, t2 = await start_agent(teacher, tmp_path, "2", core=c2, platform=p2)
    status, r = await ui(teacher, "POST", "/ui/api/laptops/1/rename", json={"new_number": "2"})
    assert status == 409 and r["busy"]
    status, _ = await ui(teacher, "POST", "/ui/api/laptops/1/rename", json={"new_number": "2", "swap": True})
    assert status == 200
    hosts = {l["number"]: l["hostname"] for l in teacher.state_snapshot()["laptops"]}
    assert hosts == {"1": "PC-2", "2": "PC-1"}
    await teacher.send_command(["2"], "open_urls", {"urls": ["a.ru"]})
    await wait_for(lambda: p1.actions)                      # «2» теперь — бывший ноутбук 1 (PC-1)
    assert not p2.actions
    status, r = await ui(teacher, "POST", "/ui/api/laptops/1/rename", json={"new_number": "../x"})
    assert status == 400
    await stop_agent(cl1, t1)
    await stop_agent(cl2, t2)
