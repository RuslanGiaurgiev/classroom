"""Этап 5: выгрузка на Яндекс Диск (с поддельным API Диска)."""

import asyncio
import hashlib

import pytest
from aiohttp import web

from teacher.server import TeacherServer
from teacher.storage import Storage
from teacher.yadisk import MemoryTokenStore
from tests.test_integration import ui
from tests.test_names import add_group, receive

TOKEN = "y0_AgAAAAAtestTOKENtestTOKENtestTOKEN"


class FakeYandex:
    """Минимальная имитация REST API Яндекс Диска."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.online = True
        self.uploads: dict[str, str] = {}
        self.upload_auth_headers = []
        self.trash = []

    def app(self):
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.add_routes([
            web.put("/v1/disk/resources", self.mkdir),
            web.get("/v1/disk/resources", self.meta),
            web.get("/v1/disk/resources/upload", self.link),
            web.delete("/v1/disk/resources", self.delete),
            web.get("/v1/disk/resources/download", self.download),
            web.get("/v1/disk/", self.info),
            web.put("/upload/{id}", self.upload),
        ])
        return app

    def auth(self, request):
        if not self.online:
            raise web.HTTPServiceUnavailable()
        if request.headers.get("Authorization") != f"OAuth {TOKEN}":
            raise web.HTTPUnauthorized(text='{"message": "Не авторизован"}')

    @staticmethod
    def path(request):
        p = request.query["path"]
        assert p.startswith("disk:/")
        return p[len("disk:/"):]

    async def mkdir(self, request):
        self.auth(request)
        p = self.path(request)
        parent = p.rpartition("/")[0]
        if parent and parent not in self.dirs:
            return web.json_response({"message": "нет родителя"}, status=409 if False else 404)
        if p in self.dirs:
            return web.json_response({"message": "exists"}, status=409)
        self.dirs.add(p)
        return web.json_response({"href": "x"}, status=201)

    async def meta(self, request):
        self.auth(request)
        p = self.path(request)
        if p in self.dirs:
            pre = p + "/"
            names = {x[len(pre):].split("/")[0] for x in self.dirs | set(self.files) if x.startswith(pre)}
            items = [{"name": n, "type": "dir" if pre + n in self.dirs else "file"} for n in sorted(names)]
            return web.json_response({"_embedded": {"items": items, "total": len(items)}})
        if p not in self.files:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response({"sha256": hashlib.sha256(self.files[p]).hexdigest()})

    async def link(self, request):
        self.auth(request)
        p = self.path(request)
        assert request.query["overwrite"] == "false"
        if p in self.files:
            return web.json_response({"message": "exists"}, status=409)
        if p.rpartition("/")[0] not in self.dirs:
            return web.json_response({"message": "нет папки"}, status=409)
        uid = str(len(self.uploads))
        self.uploads[uid] = p
        return web.json_response({"href": f"http://127.0.0.1:{request.url.port}/upload/{uid}", "method": "PUT"})

    async def delete(self, request):
        self.auth(request)
        p = self.path(request)
        assert request.query["permanently"] == "false"            # только в Корзину
        if p not in self.dirs and p not in self.files:
            return web.json_response({"message": "not found"}, status=404)
        self.trash.extend(x for x in sorted(self.dirs | set(self.files)) if x == p or x.startswith(p + "/"))
        self.dirs = {d for d in self.dirs if d != p and not d.startswith(p + "/")}
        self.files = {f: v for f, v in self.files.items() if f != p and not f.startswith(p + "/")}
        return web.Response(status=204)

    async def download(self, request):
        self.auth(request)
        return web.json_response({"href": "https://downloader.example/" + self.path(request)})

    async def info(self, request):
        self.auth(request)
        return web.json_response({"total_space": 10 * 1024 ** 3, "used_space": 1024 ** 3})

    async def upload(self, request):
        self.upload_auth_headers.append(request.headers.get("Authorization"))
        self.files[self.uploads[request.match_info["id"]]] = await request.read()
        return web.Response(status=201)


@pytest.fixture
async def env(tmp_path):
    fake = FakeYandex()
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    storage = Storage(tmp_path / "teacher")
    storage.update_settings(projects_root=str(tmp_path / "projects"), disk_root="Школа / 2026")
    tokens = MemoryTokenStore()
    server = TeacherServer(storage, host_ip_fn=lambda: "127.0.0.1", disk_token_store=tokens,
                           disk_api_base=f"http://127.0.0.1:{port}/v1/disk")
    await server.start("127.0.0.1", 0, udp_discovery=False)
    yield server, fake, tokens
    await server.stop()
    await runner.cleanup()


def mkdirs(fake, *paths):
    for p in paths:
        parts = p.split("/")
        fake.dirs |= {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}


ROOT = "Школа/2026"


async def setup_lesson(server, fake):
    """Маша — папка есть (Make Code, уроки 1–2); Петя — папки нет."""
    mkdirs(fake, f"{ROOT}/Маша Иванова/Make Code/Урок 1", f"{ROOT}/Маша Иванова/Make Code/Урок 2 — Цикл (01.11.2026)",
           f"{ROOT}/Коля Орлов/Робототехника/Tinkercad/Урок 4")
    gid, _ = await add_group(server, "Пн", "Иванова Маша Петровна\nПетя Сидоров\nОрлов Коля")
    await receive(server, "1", "Маша проект.sb3", b"masha")
    await receive(server, "2", "Петя игра.sb3", b"petya")
    await receive(server, "3", "Коля модель.stl", b"kolya")
    return gid


async def test_token_check_creates_nothing(env):
    server, fake, tokens = env
    status, r = await ui(server, "POST", "/ui/api/disk/token", json={"token": "short"})
    assert status == 400
    status, r = await ui(server, "POST", "/ui/api/disk/token", json={"token": TOKEN})
    assert r["ok"] and "Подключено" in r["message"] and not fake.dirs   # проверка ничего не создаёт
    assert tokens.token == TOKEN


async def test_sorted_files_are_not_uploaded_automatically(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    await setup_lesson(server, fake)
    await server.disk.process_queue()
    assert fake.files == {} and server.db.q("SELECT * FROM disk_queue") == []


async def test_plan_and_send_into_kids_folders(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    gid = await setup_lesson(server, fake)
    status, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=MakeCode")
    assert status == 200, plan
    by = {x["name"]: x for x in plan["students"]}
    masha, petya = by["Иванова Маша Петровна"], by["Петя Сидоров"]
    assert masha["status"] == "found" and masha["folder"] == "Маша Иванова" and masha["program_path"] == "Make Code"
    assert sorted(masha["lessons"]) == ["1", "2"] and plan["lesson_suggest"] == 3
    assert petya["status"] == "missing" and petya["new_name"] == "Петя Сидоров"
    assert "Make Code" in plan["programs"]

    body = {"group_id": gid, "date": "2026-11-08", "program": "MakeCode", "folder_name": " Урок  3 ",
            "students": [{"student_id": masha["student_id"], "folder": "Маша Иванова"},
                         {"student_id": petya["student_id"], "folder": "Петя Сидоров"}]}
    status, r = await ui(server, "POST", "/ui/api/disk/send", json=body)
    assert status == 400 and "подтвердите" in r["error"]          # новую папку — только с подтверждением
    assert fake.files == {}
    body["students"][1]["create"] = True
    status, r = await ui(server, "POST", "/ui/api/disk/send", json=body)
    assert r == {"queued": 2, "skipped": 0, "created": ["Петя Сидоров"]}
    await server.disk.process_queue()
    assert set(fake.files) == {f"{ROOT}/Маша Иванова/Make Code/Урок 3/Маша проект.sb3",
                               f"{ROOT}/Петя Сидоров/Make Code/Урок 3/Петя игра.sb3"}   # как у других детей
    assert fake.upload_auth_headers == [None, None]               # токен не уходит на адрес загрузки

    # Второй раз — ничего не дублируется; выбор папок и программа запомнились
    status, r = await ui(server, "POST", "/ui/api/disk/send", json=body)
    assert r["queued"] == 0 and r["skipped"] == 2
    _, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08")
    assert {x["name"]: x["status"] for x in plan["students"]}["Петя Сидоров"] == "found"
    assert plan["group_program"] == "Make Code" and plan["topics"] == ["Урок 3"]
    assert all(f["sent"] for x in plan["students"] if x["name"] != "Орлов Коля" for f in x["files"])


async def test_existing_lesson_and_direction_folders_reused(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    gid = await setup_lesson(server, fake)
    _, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=tinkercad")
    kolya = next(x for x in plan["students"] if x["name"] == "Орлов Коля")
    assert kolya["folder"] == "Коля Орлов" and kolya["program_path"] == "Робототехника/Tinkercad"
    await ui(server, "POST", "/ui/api/disk/send", json={
        "group_id": gid, "date": "2026-11-08", "program": "Tinkercad", "folder_name": "урок 4",
        "students": [{"student_id": kolya["student_id"], "folder": "Коля Орлов"}]})
    await server.disk.process_queue()
    assert list(fake.files) == [f"{ROOT}/Коля Орлов/Робототехника/Tinkercad/Урок 4/Коля модель.stl"]


async def test_ambiguous_folder_and_missing_root(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    gid = await setup_lesson(server, fake)
    mkdirs(fake, f"{ROOT}/Иванова Маша")
    _, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=Make Code")
    masha = next(x for x in plan["students"] if x["name"].startswith("Иванова"))
    assert masha["status"] == "ambiguous" and sorted(masha["options"]) == ["Иванова Маша", "Маша Иванова"]
    server.storage.update_settings(disk_root="Нет такой")
    status, r = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08")
    assert status == 400 and "Нет такой" in r["error"]


async def test_offline_then_retry(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    await setup_lesson(server, fake)
    fid = server.db.one("SELECT id FROM received WHERE name LIKE 'Маша%'")["id"]
    fake.online = False                                           # пропал интернет
    server.disk.enqueue_remote(fid, f"{ROOT}/Маша Иванова/Make Code/Урок 3/a.sb3")
    await server.disk.process_queue()
    item = server.db.one("SELECT * FROM disk_queue")
    assert item["status"] == "pending" and item["attempts"] == 1 and item["next_try"] > 0
    fake.online = True
    await server.disk.process_queue()                             # ещё рано — ждём задержку
    assert fake.files == {}
    await ui(server, "POST", "/ui/api/disk/retry")
    await server.disk.process_queue()
    assert list(fake.files) == [f"{ROOT}/Маша Иванова/Make Code/Урок 3/a.sb3"]


async def test_bad_token_pauses_queue(env):
    server, fake, tokens = env
    tokens.token = "y0_wrong_wrong_wrong_wrong_wrong"
    await setup_lesson(server, fake)
    fid = server.db.one("SELECT id FROM received LIMIT 1")["id"]
    server.disk.enqueue_remote(fid, f"{ROOT}/x/a.sb3")
    await server.disk.process_queue()
    assert server.disk.state == "auth_error" and "недействителен" in server.disk.last_error
    assert server.db.one("SELECT status FROM disk_queue")["status"] == "pending"   # файл не потерян


async def test_no_overwrite_on_disk(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    await setup_lesson(server, fake)
    fid = server.db.one("SELECT id FROM received WHERE name LIKE 'Маша%'")["id"]
    folder = f"{ROOT}/Маша Иванова/Make Code/Урок 2 — Цикл (01.11.2026)"
    fake.files[f"{folder}/m.sb3"] = b"other content"
    server.disk.enqueue_remote(fid, f"{folder}/m.sb3")
    await server.disk.process_queue()
    assert fake.files[f"{folder}/m.sb3"] == b"other content"
    assert fake.files[f"{folder}/m (2).sb3"] == b"masha"
    # Тот же самый файл ещё раз — не дублируется
    server.db.q("UPDATE disk_queue SET status='pending', remote_path=?", (f"{folder}/m (2).sb3",))
    await server.disk.process_queue()
    assert len(fake.files) == 2


async def test_old_auto_queue_cancelled_once(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    await setup_lesson(server, fake)
    fid = server.db.one("SELECT id FROM received LIMIT 1")["id"]
    server.db.q("INSERT INTO disk_queue(received_id, local_path, remote_path, updated) VALUES (?,?,?,0)",
                (fid, "/x", "Компьютерный класс/Пн/Маша/2026-11-08/a.sb3"))
    server.storage.update_settings(disk_v2=False)
    server.disk._cancel_old_auto_queue()
    await server.disk.process_queue()
    assert fake.files == {} and server.db.one("SELECT status FROM disk_queue")["status"] == "cancelled"


async def test_wrong_root_is_flagged_and_program_spelling_kept(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    gid = await setup_lesson(server, fake)
    mkdirs(fake, f"{ROOT}/Петя Сидоров", "Пустая")
    _, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=TINKERCAD")
    assert not plan["root_suspicious"] and plan["program"] == "Tinkercad"   # написание как у детей на Диске
    petya = next(x for x in plan["students"] if x["name"] == "Петя Сидоров")
    await ui(server, "POST", "/ui/api/disk/send", json={
        "group_id": gid, "date": "2026-11-08", "program": "TinkerCAD", "folder_name": "Урок 1",
        "students": [{"student_id": petya["student_id"], "folder": "Петя Сидоров"}]})
    await server.disk.process_queue()
    assert list(fake.files) == [f"{ROOT}/Петя Сидоров/Tinkercad/Урок 1/Петя игра.sb3"]

    server.storage.update_settings(disk_root="Пустая")                    # не та папка: детей в ней нет
    _, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=Make Code")
    assert plan["root_suspicious"] and all(x["status"] == "missing" for x in plan["students"])


async def test_lesson_folder_reused_by_number(env):
    """«Урок 2» кладётся в уже существующую «Урок 2 — Цикл (01.11.2026)», а не рядом."""
    server, fake, tokens = env
    tokens.token = TOKEN
    gid = await setup_lesson(server, fake)
    masha = next(x for x in (await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=Make Code"))[1]["students"]
                 if x["name"].startswith("Иванова"))
    assert "Урок 2 — Цикл (01.11.2026)" in masha["lesson_dirs"]
    await ui(server, "POST", "/ui/api/disk/send", json={
        "group_id": gid, "date": "2026-11-08", "program": "Make Code", "folder_name": "Урок 2",
        "students": [{"student_id": masha["student_id"], "folder": "Маша Иванова"}]})
    await server.disk.process_queue()
    assert list(fake.files) == [f"{ROOT}/Маша Иванова/Make Code/Урок 2 — Цикл (01.11.2026)/Маша проект.sb3"]
    status, r = await ui(server, "POST", "/ui/api/disk/send", json={
        "group_id": gid, "date": "2026-11-08", "program": "Make Code", "folder_name": "Урок/3", "students": []})
    assert status == 400


async def test_browse_delete_and_resend(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    gid = await setup_lesson(server, fake)
    _, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=Make Code")
    masha = next(x for x in plan["students"] if x["name"].startswith("Иванова"))
    body = {"group_id": gid, "date": "2026-11-08", "program": "Make Code", "folder_name": "Урок 3",
            "students": [{"student_id": masha["student_id"], "folder": "Маша Иванова"}]}
    await ui(server, "POST", "/ui/api/disk/send", json=body)
    await server.disk.process_queue()

    status, ls = await ui(server, "GET", "/ui/api/disk/ls?path=Маша Иванова/Make Code")
    assert status == 200 and [i["name"] for i in ls["items"]] == ["Урок 1", "Урок 2 — Цикл (01.11.2026)", "Урок 3"]
    status, r = await ui(server, "GET", "/ui/api/disk/ls?path=../секрет")
    assert status == 400
    status, r = await ui(server, "POST", "/ui/api/disk/delete", json={"path": ""})
    assert status == 400 and not fake.trash                         # папку школы целиком — нельзя

    # Удалили урок на Диске — его можно отправить заново
    status, r = await ui(server, "POST", "/ui/api/disk/delete", json={"path": "Маша Иванова/Make Code/Урок 3"})
    assert status == 200 and f"{ROOT}/Маша Иванова/Make Code/Урок 3/Маша проект.sb3" in fake.trash
    _, plan = await ui(server, "GET", f"/ui/api/disk/plan?group_id={gid}&date=2026-11-08&program=Make Code")
    assert not next(x for x in plan["students"] if x["name"].startswith("Иванова"))["files"][0]["sent"]
    _, r = await ui(server, "POST", "/ui/api/disk/send", json=body)
    assert r["queued"] == 1
    await server.disk.process_queue()
    assert f"{ROOT}/Маша Иванова/Make Code/Урок 3/Маша проект.sb3" in fake.files

    status, r = await ui(server, "GET", "/ui/api/disk/link?path=Маша Иванова/Make Code/Урок 3/Маша проект.sb3")
    assert r["href"].startswith("https://downloader.example/")


async def test_manual_upload_and_mkdir(env):
    import aiohttp
    server, fake, tokens = env
    tokens.token = TOKEN
    mkdirs(fake, f"{ROOT}/Маша Иванова")
    status, _ = await ui(server, "POST", "/ui/api/disk/mkdir", json={"path": "Маша Иванова", "name": "Tinkercad"})
    assert status == 200 and f"{ROOT}/Маша Иванова/Tinkercad" in fake.dirs
    status, r = await ui(server, "POST", "/ui/api/disk/mkdir", json={"path": "Маша Иванова", "name": "../x"})
    assert status == 400
    for content in (b"model", b"other"):                             # второй — то же имя, другое содержимое
        form = aiohttp.FormData()
        form.add_field("file", content, filename="модель.stl")
        status, r = await ui(server, "POST", "/ui/api/disk/upload?path=Маша Иванова/Tinkercad", data=form)
        assert status == 200, r
    assert fake.files[f"{ROOT}/Маша Иванова/Tinkercad/модель.stl"] == b"model"
    assert fake.files[f"{ROOT}/Маша Иванова/Tinkercad/модель (2).stl"] == b"other"   # ничего не перезаписали
    assert not list(server.storage.data_dir.glob("disk-upload-*"))  # временные файлы убраны
