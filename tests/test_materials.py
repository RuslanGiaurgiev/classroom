"""Отправка материалов урока: файлы и папки, докачка, SHA-256, безопасность путей."""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from agent.core import AgentCore
from agent.platform.fake import FakePlatform
from shared.materials import UnsafePath, safe_rel_path
from teacher.materials import collect_items
from tests.test_files import make_core, write
from tests.test_integration import start_agent, stop_agent, teacher, ui, wait_for  # noqa: F401


@pytest.mark.parametrize("rel", ["../x", "a/../../x", "/etc/passwd", "C:/Windows/x", "a//b", "con.txt", "a/b:c",
                                 "dir/NUL", "x\\..\\y", "a/b. "])
def test_unsafe_paths(rel):
    with pytest.raises(UnsafePath):
        safe_rel_path(rel)


def test_safe_paths():
    assert safe_rel_path("Урок 5/картинки/кот.png") == "Урок 5/картинки/кот.png"


def make_tree(root: Path) -> dict[str, bytes]:
    files = {
        "Урок 5/задание.pdf": os.urandom(3 * 1024 * 1024 + 7),      # больше одного куска
        "Урок 5/картинки/кот.png": os.urandom(5000),
        "Урок 5/картинки/пёс.png": os.urandom(5000),
        "Урок 5/пусто/пустой.txt": b"",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "Урок 5" / ".DS_Store").write_bytes(b"x")                # служебный файл Mac — не отправляем
    return files


def test_collect_items(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "single.txt").write_text("1")
    items = collect_items([str(tmp_path / "Урок 5"), str(tmp_path / "single.txt")])
    rels = sorted(i["rel"] for i in items)
    assert rels == ["single.txt", "Урок 5/задание.pdf", "Урок 5/картинки/кот.png", "Урок 5/картинки/пёс.png",
                    "Урок 5/пусто/пустой.txt"]


def received(platform) -> dict[str, bytes]:
    root = platform.known_folder("desktop") / "Материалы"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


async def test_send_folder_to_two_laptops(teacher, tmp_path):
    src = tmp_path / "src"
    files = make_tree(src)
    agents = []
    for n in ("1", "2"):
        platform, core = make_core(tmp_path, n)
        agents.append((platform, core, *(await start_agent(teacher, tmp_path, n, core=core, platform=platform))[1:]))
    status, r = await ui(teacher, "POST", "/ui/api/materials/send", json={"paths": [str(src / "Урок 5")], "targets": "all"})
    assert status == 200 and r["count"] == 4
    pkg = teacher.materials.packages[r["package_id"]]
    await wait_for(lambda: all(t["state"] == "done" for t in pkg["targets"].values()), timeout=20)
    for platform, core, client, task in agents:
        assert received(platform) == files
        assert "получено файлов: 4" in pkg["targets"]["1"]["message"]
        # Материалы не уходят обратно учителю как «проекты»
        assert core.collector.ledger.pending_count() == 0
        await stop_agent(client, task)
    assert teacher.db.q("SELECT * FROM received") == []


async def test_resume_skip_and_no_overwrite(teacher, tmp_path):
    src = tmp_path / "src"
    files = make_tree(src)
    platform, core = make_core(tmp_path, "3")
    root = platform.known_folder("desktop") / "Материалы" / "Урок 5"
    # Половина большого файла уже скачана (обрыв связи в прошлый раз)
    big = files["Урок 5/задание.pdf"]
    write(root / f"задание.pdf.{hashlib.sha256(big).hexdigest()[:8]}.part", big[:1_000_000])
    # Такой же файл уже лежит — не перекачиваем
    write(root / "картинки" / "кот.png", files["Урок 5/картинки/кот.png"])
    # Ребёнок изменил файл с тем же именем — не перезаписываем
    write(root / "картинки" / "пёс.png", b"my drawing")
    _, client, task = await start_agent(teacher, tmp_path, "3", core=core, platform=platform)
    _, r = await ui(teacher, "POST", "/ui/api/materials/send", json={"paths": [str(src / "Урок 5")], "targets": ["3"]})
    pkg = teacher.materials.packages[r["package_id"]]
    await wait_for(lambda: pkg["targets"]["3"]["state"] == "done", timeout=20)
    got = received(platform)
    assert got["Урок 5/задание.pdf"] == big
    assert got["Урок 5/картинки/пёс.png"] == b"my drawing"
    assert got["Урок 5/картинки/пёс (2).png"] == files["Урок 5/картинки/пёс.png"]
    assert "уже были на месте: 1" in pkg["targets"]["3"]["message"]
    assert not list(root.rglob("*.part"))
    await stop_agent(client, task)


async def test_offline_laptop_gets_package_later(teacher, tmp_path):
    src = tmp_path / "src"
    files = make_tree(src)
    platform, core = make_core(tmp_path, "4")
    _, client, task = await start_agent(teacher, tmp_path, "4", core=core, platform=platform)
    await stop_agent(client, task)
    await wait_for(lambda: "4" not in teacher.agents)
    _, r = await ui(teacher, "POST", "/ui/api/materials/send", json={"paths": [str(src / "Урок 5")], "targets": ["4"]})
    pkg = teacher.materials.packages[r["package_id"]]
    assert pkg["targets"]["4"]["state"] == "pending"
    # Ноутбук включили
    from agent.client import AgentClient, Backoff
    from agent.state import AgentState
    core2 = AgentCore(platform)
    client2 = AgentClient(platform, AgentState.load(platform), use_mdns=False, backoff=Backoff(initial=0.05), core=core2)
    task2 = asyncio.create_task(client2.run())
    await wait_for(lambda: pkg["targets"]["4"]["state"] == "done", timeout=20)
    assert received(platform) == files
    await stop_agent(client2, task2)


async def test_agent_rejects_path_traversal(teacher, tmp_path):
    src = tmp_path / "src"
    make_tree(src)
    platform, core = make_core(tmp_path, "5")
    _, client, task = await start_agent(teacher, tmp_path, "5", core=core, platform=platform)
    pkg = await teacher.materials.create([str(src / "Урок 5")])
    pkg["files"][0]["path"] = "../../Автозагрузка/evil.exe"          # «взломанный» Mac
    await teacher.materials.send(pkg["id"], ["5"])
    await wait_for(lambda: pkg["targets"]["5"]["state"] == "error", timeout=10)
    assert "небезопасный путь" in pkg["targets"]["5"]["message"]
    assert not list(platform.data_dir().rglob("evil.exe")) and not list(tmp_path.rglob("evil.exe"))
    await stop_agent(client, task)


async def test_not_enough_disk_space(teacher, tmp_path, monkeypatch):
    import agent.materials as am
    src = tmp_path / "src"
    make_tree(src)
    platform, core = make_core(tmp_path, "6")
    _, client, task = await start_agent(teacher, tmp_path, "6", core=core, platform=platform)
    monkeypatch.setattr(am.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    _, r = await ui(teacher, "POST", "/ui/api/materials/send", json={"paths": [str(src / "Урок 5")], "targets": ["6"]})
    pkg = teacher.materials.packages[r["package_id"]]
    await wait_for(lambda: pkg["targets"]["6"]["state"] == "error", timeout=10)
    assert "мало места" in pkg["targets"]["6"]["message"]
    await stop_agent(client, task)


async def test_materials_folder_cannot_hide_projects(teacher):
    status, r = await ui(teacher, "PUT", "/ui/api/settings", json={"materials_dest": {"base": "desktop", "sub": ""}})
    assert status == 400
    status, r = await ui(teacher, "PUT", "/ui/api/settings", json={"materials_dest": {"base": "desktop", "sub": "Проекты"}})
    assert status == 400
    status, r = await ui(teacher, "PUT", "/ui/api/settings", json={"materials_dest": {"base": "documents", "sub": "Уроки"}})
    assert status == 200


async def test_materials_inside_watched_downloads_are_never_collected_or_cleaned(teacher, tmp_path):
    src = tmp_path / "src"
    files = make_tree(src)
    await ui(teacher, "PUT", "/ui/api/settings", json={"materials_dest": {"base": "downloads", "sub": "Материалы"}})
    platform, core = make_core(tmp_path, "7")
    core.collector.excluded_roots = core.materials_roots
    _, client, task = await start_agent(teacher, tmp_path, "7", core=core, platform=platform)
    await wait_for(lambda: core.materials.dest["base"] == "downloads")
    _, r = await ui(teacher, "POST", "/ui/api/materials/send", json={"paths": [str(src / "Урок 5")], "targets": ["7"]})
    pkg = teacher.materials.packages[r["package_id"]]
    await wait_for(lambda: pkg["targets"]["7"]["state"] == "done", timeout=20)
    write(platform.known_folder("downloads") / "проект.sb3", b"kid project")
    await wait_for(lambda: teacher.db.q("SELECT 1 FROM received WHERE name='проект.sb3'"), timeout=10)
    await asyncio.sleep(1)
    assert [r["name"] for r in teacher.db.q("SELECT name FROM received")] == ["проект.sb3"]   # материалы не вернулись
    await teacher.end_lesson.run(["7"])
    root = platform.known_folder("downloads") / "Материалы"
    assert len([p for p in root.rglob("*") if p.is_file()]) == len(files)                      # материалы на месте
    assert not (platform.known_folder("downloads") / "проект.sb3").exists()                    # проект убран
    await stop_agent(client, task)
