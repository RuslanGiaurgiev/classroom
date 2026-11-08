"""Этап 3 и 6: сбор файлов, очередь, докачка, SHA-256, «удалять только подтверждённое»."""

import asyncio
import hashlib
import os
from pathlib import Path

import aiohttp
import pytest

from agent.collector import Collector, validate_config
from agent.core import AgentCore
from agent.files import ReadinessTracker, should_ignore
from agent.ledger import Ledger
from agent.platform.fake import FakePlatform
from tests.test_integration import start_agent, stop_agent, teacher, wait_for  # noqa: F401


# ---------- Правила игнорирования ----------

@pytest.mark.parametrize("name, size, ignored", [
    ("кот.sb3", 100, False), ("проект.py", 5, False), ("photo.PNG", 10, False),
    ("кот.sb3.crdownload", 100, True), ("a.part", 1, True), ("b.tmp", 1, True), ("~$доклад.docx", 1, True),
    ("desktop.ini", 1, True), ("Thumbs.db", 1, True), (".secret", 1, True), ("пусто.txt", 0, True),
    ("огромный.zip", 501 * 1024 * 1024, True),
])
def test_ignore_rules(name, size, ignored):
    reason = should_ignore(Path(name), size, 500 * 1024 * 1024, lambda p: False, Path(name))
    assert (reason is not None) is ignored


def test_hidden_attribute_and_hidden_dirs_ignored():
    assert should_ignore(Path(".git/config"), 5, 10**9, lambda p: False, Path("x"))
    assert should_ignore(Path("a.txt"), 5, 10**9, lambda p: True, Path("x"))


# ---------- Готовность файла ----------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_file_ready_after_3_stable_seconds():
    clock, opened = Clock(), {"ok": True}
    tr = ReadinessTracker(clock=clock, can_open=lambda p: opened["ok"])
    p = Path("кот.sb3")
    tr.observe(p, 100, 1)
    clock.t = 2.9
    tr.observe(p, 100, 1)
    assert not tr.is_ready(p)
    clock.t = 3.0
    assert tr.is_ready(p)


def test_growing_file_not_ready():
    clock = Clock()
    tr = ReadinessTracker(clock=clock, can_open=lambda p: True)
    p = Path("большой.zip")
    for i in range(10):
        clock.t = i * 2.0
        tr.observe(p, 100 * (i + 1), i)   # размер растёт — скачивается
        assert not tr.is_ready(p)


def test_locked_file_not_ready():
    clock = Clock()
    tr = ReadinessTracker(clock=clock, can_open=lambda p: False)  # открыт другой программой
    tr.observe(Path("x"), 1, 1)
    clock.t = 100
    assert not tr.is_ready(Path("x"))


# ---------- Учёт (ledger) ----------

def test_ledger_duplicates_and_baseline(tmp_path):
    led = Ledger(tmp_path / "f.db")
    led.add_baseline("downloads", "старое.pdf", 10, 1)
    assert led.pending_count() == 0 and led.is_known("downloads", "старое.pdf", 10, 1)
    assert led.add_found("downloads", "кот.sb3", 10, 2, "a" * 64) == "pending"
    assert led.add_found("downloads", "кот.sb3", 10, 2, "a" * 64) == "known"
    led.mark_confirmed_by_sha("a" * 64)
    # Тот же файл под другим именем — уже у учителя, повторно не отправляем
    assert led.add_found("desktop/Проекты", "копия кота.sb3", 10, 3, "a" * 64) == "confirmed"
    # Изменённая версия (другое время/хэш) — новая запись к отправке
    assert led.add_found("downloads", "кот.sb3", 12, 4, "b" * 64) == "pending"


def test_config_from_teacher_is_sanitized():
    cfg = validate_config({"folders": [{"base": "desktop", "sub": "..\\..\\Windows"}, {"base": "C:", "sub": ""},
                                       {"base": "documents", "sub": "Scratch\\Проекты"}], "max_file_mb": 10**9})
    assert cfg == {"folders": [{"base": "documents", "sub": "Scratch/Проекты"}], "max_file_mb": 500}


# ---------- Сквозные проверки ----------

def make_core(tmp_path, number):
    platform = FakePlatform(tmp_path / f"agent-{number}", hostname=f"PC-{number}")
    core = AgentCore(platform)
    core.collector = Collector(platform, tracker=ReadinessTracker(stable_seconds=0.2), use_watchdog=False,
                               full_scan_every=0.3)
    return platform, core


def write(path: Path, data: bytes, mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


async def test_new_file_collected_and_confirmed(teacher, tmp_path):
    platform, core = make_core(tmp_path, "1")
    downloads = platform.known_folder("downloads")
    write(downloads / "старый.pdf", b"old")               # лежал до установки агента
    _, client, task = await start_agent(teacher, tmp_path, "1", core=core, platform=platform)

    projects = platform.known_folder("desktop") / "Проекты"
    assert projects.is_dir()                               # агент создал «Рабочий стол\Проекты»
    data = os.urandom(300_000)
    write(projects / "Маша кот.sb3", data)

    await wait_for(lambda: teacher.db.find_received("1", hashlib.sha256(data).hexdigest()), timeout=10)
    row = teacher.db.find_received("1", hashlib.sha256(data).hexdigest())
    assert Path(row["inbox_path"]).read_bytes() == data
    assert "/Входящие/" in row["inbox_path"].replace("\\", "/") and "/Ноут-1/" in row["inbox_path"].replace("\\", "/")
    # Учитель сначала сохраняет файл, потом подтверждает — ждём подтверждения на ноутбуке
    await wait_for(lambda: core.collector.ledger.pending_count() == 0)
    # Старый файл не отправлялся
    assert len(teacher.db.q("SELECT * FROM received")) == 1
    await wait_for(lambda: teacher.state_snapshot()["laptops"][0]["pending_files"] == 0)
    await stop_agent(client, task)


async def test_queue_when_teacher_offline(tmp_path):
    from agent.client import AgentClient, Backoff
    from agent.state import AgentState
    from teacher.server import TeacherServer
    from teacher.storage import Storage

    storage = Storage(tmp_path / "teacher")
    storage.update_settings(projects_root=str(tmp_path / "projects"))
    server = TeacherServer(storage, host_ip_fn=lambda: "127.0.0.1")
    port = await server.start("127.0.0.1", 0)
    platform, core = make_core(tmp_path, "2")
    _, client, task = await start_agent(server, tmp_path, "2", core=core, platform=platform)
    await server.stop()                                      # Mac выключили
    await wait_for(lambda: not client.connected.is_set())

    write(platform.known_folder("downloads") / "работа.txt", b"hello")
    await wait_for(lambda: core.collector.ledger.pending_count() == 1)   # ждёт в очереди
    await asyncio.sleep(0.5)
    assert core.collector.ledger.pending_count() == 1

    server2 = TeacherServer(Storage(tmp_path / "teacher"), host_ip_fn=lambda: "127.0.0.1")
    await server2.start("127.0.0.1", port)
    try:
        await wait_for(lambda: server2.db.find_received("2", hashlib.sha256(b"hello").hexdigest()), timeout=10)
        await wait_for(lambda: core.collector.ledger.pending_count() == 0)
    finally:
        await stop_agent(client, task)
        await server2.stop()


async def test_resume_and_sha_check(teacher, tmp_path):
    """Докачка с места обрыва и отказ при неверной контрольной сумме."""
    platform, core = make_core(tmp_path, "3")
    _, client, task = await start_agent(teacher, tmp_path, "3", core=core, platform=platform)
    await stop_agent(client, task)
    token = client.state.token
    base = f"http://127.0.0.1:{teacher.port}"
    data = os.urandom(100_000)
    sha = hashlib.sha256(data).hexdigest()
    h = {"Authorization": f"Bearer {token}"}
    meta = {"sha256": sha, "size": len(data), "name": "игра.sb3", "rel_path": "игра.sb3", "folder_key": "downloads"}
    async with aiohttp.ClientSession(headers=h) as s:
        async with s.post(f"{base}/api/files/init", json=meta) as r:
            assert (await r.json()) == {"status": "upload", "offset": 0}
        async with s.put(f"{base}/api/files/{sha}", params={"offset": "0"}, data=data[:40_000]) as r:
            assert (await r.json())["offset"] == 40_000
        # «Обрыв связи». Начинаем заново — сервер помнит, сколько уже получено
        async with s.post(f"{base}/api/files/init", json=meta) as r:
            assert (await r.json())["offset"] == 40_000
        async with s.put(f"{base}/api/files/{sha}", params={"offset": "0"}, data=data) as r:
            assert r.status == 409 and (await r.json())["offset"] == 40_000    # неверное смещение
        async with s.put(f"{base}/api/files/{sha}", params={"offset": "40000"}, data=data[40_000:]) as r:
            assert (await r.json())["offset"] == len(data)
        async with s.post(f"{base}/api/files/{sha}/complete") as r:
            assert (await r.json())["status"] == "done"
        assert Path(teacher.db.find_received("3", sha)["inbox_path"]).read_bytes() == data

        # Испорченные данные: SHA-256 не совпадает → не подтверждаем
        bad = b"x" * 1000
        meta2 = {**meta, "sha256": "f" * 64, "size": len(bad), "name": "плохой.bin"}
        async with s.post(f"{base}/api/files/init", json=meta2) as r:
            assert (await r.json())["offset"] == 0
        async with s.put(f"{base}/api/files/{'f' * 64}", params={"offset": "0"}, data=bad):
            pass
        async with s.post(f"{base}/api/files/{'f' * 64}/complete") as r:
            assert r.status == 422
        assert teacher.db.find_received("3", "f" * 64) is None

    # Без токена — нельзя
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/api/files/init", json=meta) as r:
            assert r.status == 401


async def test_same_file_not_sent_twice_but_new_version_is(teacher, tmp_path):
    platform, core = make_core(tmp_path, "4")
    _, client, task = await start_agent(teacher, tmp_path, "4", core=core, platform=platform)
    d = platform.known_folder("downloads")
    write(d / "a.sb3", b"version1", mtime=1_700_000_000)
    await wait_for(lambda: len(teacher.db.q("SELECT * FROM received")) == 1, timeout=10)
    write(d / "копия a.sb3", b"version1", mtime=1_700_000_100)           # тот же файл под другим именем
    write(d / "a.sb3", b"version2!", mtime=1_700_000_200)                # изменённая версия
    await wait_for(lambda: len(teacher.db.q("SELECT * FROM received")) == 2, timeout=10)
    await asyncio.sleep(0.8)
    names = sorted(r["name"] for r in teacher.db.q("SELECT * FROM received"))
    assert names == ["a.sb3", "a.sb3"]                                   # копия не отправлялась
    await stop_agent(client, task)


async def test_cleanup_moves_only_confirmed(teacher, tmp_path):
    """«Конец урока»: в корзину уходят только подтверждённые учителем и неизменённые файлы."""
    platform, core = make_core(tmp_path, "5")
    d = platform.known_folder("downloads")
    write(d / "старый.pdf", b"baseline")
    _, client, task = await start_agent(teacher, tmp_path, "5", core=core, platform=platform)
    write(d / "готов.sb3", b"done", mtime=1_700_000_000)
    write(d / "Папка" / "тоже готов.txt", b"done2", mtime=1_700_000_000)
    await wait_for(lambda: len(teacher.db.q("SELECT * FROM received")) == 2, timeout=10)
    write(d / "изменили.sb3", b"v1", mtime=1_700_000_000)
    await wait_for(lambda: len(teacher.db.q("SELECT * FROM received")) == 3, timeout=10)
    await stop_agent(client, task)                    # связь пропала

    write(d / "изменили.sb3", "v2 после отправки".encode(), mtime=1_700_000_500)   # ребёнок продолжил работу
    write(d / "не отправлен.sb3", b"new")                                   # не успел уйти
    col = core.collector
    col.full_scan()
    await asyncio.sleep(0.3)
    await col.process_candidates()

    confirmed = set(teacher.db.confirmed_shas("5"))
    r = await col.cleanup(confirmed)
    assert r["moved"] == 2
    left = sorted(p.name for p in d.rglob("*") if p.is_file())
    assert left == ["изменили.sb3", "не отправлен.sb3", "старый.pdf"]
    assert not (d / "Папка").exists()                                       # пустая подпапка убрана
    trashed = sorted(p.name for p in col.trash_dir().rglob("*") if p.is_file())
    assert trashed == ["готов.sb3", "тоже готов.txt"]

    # Если учитель не подтвердил файл (например, его нет в списке) — не трогаем
    write(d / "x.sb3", b"x", mtime=1_700_000_000)
    col.full_scan()
    await asyncio.sleep(0.3)
    await col.process_candidates()
    row = [r for r in col.ledger.pending() if r.rel_path == "x.sb3"][0]
    col.ledger.set_status(row.id, "confirmed")       # в учёте агента «подтверждён»...
    r = await col.cleanup(set())                     # ...но учитель его не назвал
    assert r["moved"] == 0 and (d / "x.sb3").exists()


def test_trash_kept_7_days(tmp_path):
    platform = FakePlatform(tmp_path)
    col = Collector(platform, use_watchdog=False)
    for name in ["2020-01-01", "2999-01-01", "не дата"]:
        (col.trash_dir() / name).mkdir(parents=True)
    col.purge_trash()
    assert sorted(p.name for p in col.trash_dir().iterdir()) == ["2999-01-01", "не дата"]


# ---------- «Конец урока» целиком ----------

async def test_end_lesson_full_flow(teacher, tmp_path):
    from tests.test_integration import ui
    p1, core1 = make_core(tmp_path, "1")
    p2, core2 = make_core(tmp_path, "2")
    write(p1.known_folder("downloads") / "старьё.pdf", b"old")        # до установки — не трогаем
    _, c1, t1 = await start_agent(teacher, tmp_path, "1", core=core1, platform=p1)
    _, c2, t2 = await start_agent(teacher, tmp_path, "2", core=core2, platform=p2)

    d1 = p1.known_folder("desktop") / "Проекты"
    write(d1 / "кот.sb3", b"cat")
    write(d1 / "пёс.sb3", b"dog")                    # появился прямо перед концом урока
    d2 = p2.known_folder("downloads")
    big = d2 / "ещё качается.zip.crdownload"          # скачивается — игнорируется, не удаляется
    write(big, b"partial")

    status, _ = await ui(teacher, "POST", "/ui/api/end-lesson", json={"targets": "all"})
    assert status == 200
    await asyncio.sleep(0.1)
    await wait_for(lambda: not teacher.end_lesson.running, timeout=30)
    prog = teacher.end_lesson.progress
    assert prog["1"]["stage"] == "done" and "убрано в корзину 2" in prog["1"]["text"]
    assert prog["2"]["stage"] == "done"
    # Всё получено учителем
    assert sorted(r["name"] for r in teacher.db.q("SELECT name FROM received")) == ["кот.sb3", "пёс.sb3"]
    # На ноутбуке осталось только «старое» и недокачанное
    assert sorted(p.name for p in p1.known_folder("downloads").rglob("*") if p.is_file()) == ["старьё.pdf"]
    assert not any(d1.iterdir())
    assert big.exists()
    await stop_agent(c1, t1)
    await stop_agent(c2, t2)


async def test_end_lesson_skips_cleanup_if_not_everything_sent(teacher, tmp_path):
    """Если хоть один файл не подтверждён — на этом ноутбуке ничего не удаляется."""
    platform, core = make_core(tmp_path, "3")
    _, client, task = await start_agent(teacher, tmp_path, "3", core=core, platform=platform)
    d = platform.known_folder("downloads")
    write(d / "отправлен.sb3", b"ok")
    await wait_for(lambda: teacher.db.q("SELECT 1 FROM received"), timeout=10)

    # Сеть «пропала» при отправке: новый файл не сможет уйти учителю
    import teacher.endlesson as el
    old_attempts = el.ATTEMPTS
    el.ATTEMPTS = 1

    async def fail_upload(session, base, row):
        raise aiohttp.ClientConnectionError("сеть пропала")
    core.collector._upload_one = fail_upload
    write(d / "не отправлен.sb3", b"no")
    try:
        await teacher.end_lesson.run(["3"])
    finally:
        el.ATTEMPTS = old_attempts
    assert teacher.end_lesson.progress["3"]["stage"] == "warning"
    assert (d / "отправлен.sb3").exists() and (d / "не отправлен.sb3").exists()   # ничего не удалено
    await stop_agent(client, task)


async def test_end_lesson_offline_laptop(teacher, tmp_path):
    platform, core = make_core(tmp_path, "4")
    _, client, task = await start_agent(teacher, tmp_path, "4", core=core, platform=platform)
    await stop_agent(client, task)
    await wait_for(lambda: "4" not in teacher.agents)
    await teacher.end_lesson.run(["4"])
    assert teacher.end_lesson.progress["4"]["stage"] == "skip"
