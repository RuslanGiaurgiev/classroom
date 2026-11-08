"""Игнорирование служебных файлов: встроенные правила + правила учителя, на Mac и на ноутбуке."""

import asyncio
import hashlib
import os

import aiohttp
import pytest

from shared.ignore import BUILTIN_PATTERNS, clean_patterns, ignored_by_patterns, pattern_for
from tests.test_files import make_core, write
from tests.test_integration import start_agent, stop_agent, teacher, ui, wait_for  # noqa: F401
from tests.test_names import receive


@pytest.mark.parametrize("rel, hit", [
    ("data/logs/agent-errors.dpapi", True), ("Logs/x.txt", True), ("a/b/cache/c.bin", True),
    ("кот.sb3", False), ("Проекты/робот.sb3", False), ("logs.sb3", False),
])
def test_builtin(rel, hit):
    assert bool(ignored_by_patterns(rel, BUILTIN_PATTERNS)) is hit


def test_user_patterns():
    assert clean_patterns([" *.XYZ ", "C:\\bad", "a/b/c", "data/", 5, "*.xyz"]) == ["*.xyz", "data/"]
    assert ignored_by_patterns("data/state.bin", ["data/"]) == "data/"
    assert pattern_for("agent-errors.dpapi") == "*.dpapi" and pattern_for("README") == "readme"


async def test_teacher_drops_junk_from_old_agent(teacher, tmp_path):
    """Старый агент (без новых правил) присылает .dpapi — Mac отвечает «готово» и ничего не сохраняет."""
    platform, core = make_core(tmp_path, "12")
    _, client, task = await start_agent(teacher, tmp_path, "12", core=core, platform=platform)
    await stop_agent(client, task)
    data = os.urandom(1000)
    meta = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "name": "agent-errors.dpapi",
            "rel_path": "data/logs/agent-errors.dpapi", "folder_key": "downloads"}
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {client.state.token}"}) as s:
        async with s.post(f"http://127.0.0.1:{teacher.port}/api/files/init", json=meta) as r:
            assert (await r.json()) == {"status": "done"}
    assert teacher.db.q("SELECT * FROM received") == []
    assert teacher.db.confirmed_shas("12") == []      # значит, с ноутбука этот файл не удалится


async def test_ignore_button_and_rule_reaches_laptop(teacher, tmp_path):
    for i in range(3):
        await receive(teacher, "12", "state.qqq", f"x{i}".encode())
    await receive(teacher, "12", "нужный.sb3", b"keep")
    teacher.sorter.trash_dir = lambda: tmp_path / "trash"
    status, r = await ui(teacher, "POST", "/ui/api/ignore", json={"pattern": "*.qqq"})
    assert status == 200 and r["removed"] == 3
    _, proj = await ui(teacher, "GET", "/ui/api/projects")
    assert [u["name"] for u in proj["unsorted"]] == ["нужный.sb3"]

    # Правило уходит на ноутбук: новый агент такие файлы даже не отправляет
    platform, core = make_core(tmp_path, "1")
    _, client, task = await start_agent(teacher, tmp_path, "1", core=core, platform=platform)
    await wait_for(lambda: core.collector.config["ignore"] == ["*.qqq"])
    write(platform.known_folder("downloads") / "ещё.qqq", b"junk")
    write(platform.known_folder("downloads") / "маша.sb3", b"project")
    await wait_for(lambda: teacher.db.q("SELECT 1 FROM received WHERE name='маша.sb3'"), timeout=10)
    await asyncio.sleep(0.5)
    assert not teacher.db.q("SELECT 1 FROM received WHERE name='ещё.qqq'")
    assert core.collector.ledger.pending_count() == 0
    await stop_agent(client, task)


async def test_junk_purged_on_start(tmp_path):
    from teacher.server import TeacherServer
    from teacher.storage import Storage
    storage = Storage(tmp_path / "t")
    storage.update_settings(projects_root=str(tmp_path / "p"))
    srv = TeacherServer(storage, host_ip_fn=lambda: "127.0.0.1")
    srv.sorter.trash_dir = lambda: tmp_path / "trash"
    await receive(srv, "12", "agent-errors.dpapi", b"junk")
    await receive(srv, "12", "кот.sb3", b"cat")
    await srv.start("127.0.0.1", 0, udp_discovery=False)
    try:
        assert [r["name"] for r in srv.db.q("SELECT name FROM received WHERE status='unsorted'")] == ["кот.sb3"]
    finally:
        await srv.stop()
