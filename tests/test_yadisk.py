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

    def app(self):
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.add_routes([
            web.put("/v1/disk/resources", self.mkdir),
            web.get("/v1/disk/resources", self.meta),
            web.get("/v1/disk/resources/upload", self.link),
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
    storage.update_settings(projects_root=str(tmp_path / "projects"), disk_root="Школа", disk_upload_enabled=True)
    tokens = MemoryTokenStore()
    server = TeacherServer(storage, host_ip_fn=lambda: "127.0.0.1", disk_token_store=tokens,
                           disk_api_base=f"http://127.0.0.1:{port}/v1/disk")
    await server.start("127.0.0.1", 0, udp_discovery=False)
    yield server, fake, tokens
    await server.stop()
    await runner.cleanup()


async def test_token_check_and_upload(env):
    server, fake, tokens = env
    status, r = await ui(server, "POST", "/ui/api/disk/token", json={"token": "short"})
    assert status == 400
    status, r = await ui(server, "POST", "/ui/api/disk/token", json={"token": TOKEN})
    assert r["ok"] and "Подключено" in r["message"] and not fake.dirs   # проверка ничего не создаёт
    assert tokens.token == TOKEN

    await add_group(server, "Пн", "Маша")
    await receive(server, "1", "masha.sb3", b"project")
    await receive(server, "1", "непонятно.sb3", b"x")            # неразобранное — на Диск не идёт
    await server.disk.process_queue()
    assert fake.files == {"Школа/Пн/Маша/2026-11-08/masha.sb3": b"project"}
    assert fake.upload_auth_headers == [None]                    # токен не уходит на адрес загрузки
    _, st = await ui(server, "GET", "/ui/api/disk")
    assert st["counts"] == {"done": 1}


async def test_offline_then_retry(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    await add_group(server, "Пн", "Маша")
    fake.online = False                                           # пропал интернет
    await receive(server, "1", "masha.sb3", b"p")
    await server.disk.process_queue()
    item = server.db.one("SELECT * FROM disk_queue")
    assert item["status"] == "pending" and item["attempts"] == 1 and item["next_try"] > 0
    fake.online = True
    await server.disk.process_queue()                             # ещё рано — ждём задержку
    assert fake.files == {}
    await ui(server, "POST", "/ui/api/disk/retry")
    await server.disk.process_queue()
    assert list(fake.files) == ["Школа/Пн/Маша/2026-11-08/masha.sb3"]


async def test_bad_token_pauses_queue(env):
    server, fake, tokens = env
    tokens.token = "y0_wrong_wrong_wrong_wrong_wrong"
    await add_group(server, "Пн", "Маша")
    await receive(server, "1", "masha.sb3", b"p")
    await server.disk.process_queue()
    assert server.disk.state == "auth_error" and "недействителен" in server.disk.last_error
    assert server.db.one("SELECT status FROM disk_queue")["status"] == "pending"   # файл не потерян


async def test_no_overwrite_on_disk(env):
    server, fake, tokens = env
    tokens.token = TOKEN
    await add_group(server, "Пн", "Маша")
    fake.dirs |= {"Школа", "Школа/Пн", "Школа/Пн/Маша", "Школа/Пн/Маша/2026-11-08"}
    fake.files["Школа/Пн/Маша/2026-11-08/masha.sb3"] = b"other content"
    await receive(server, "1", "masha.sb3", b"mine")
    await server.disk.process_queue()
    assert fake.files["Школа/Пн/Маша/2026-11-08/masha.sb3"] == b"other content"
    assert fake.files["Школа/Пн/Маша/2026-11-08/masha (2).sb3"] == b"mine"
    # Тот же самый файл ещё раз — не дублируется
    server.db.q("UPDATE disk_queue SET status='pending', remote_path='Школа/Пн/Маша/2026-11-08/masha (2).sb3'")
    await server.disk.process_queue()
    assert len(fake.files) == 2


async def test_upload_paused_by_default(env):
    """Пока выгрузка не включена, файлы ждут в очереди и на Диск не уходят."""
    server = env[0] if isinstance(env, tuple) else env.server
    server.storage.update_settings(disk_upload_enabled=False)
    assert await server.disk.process_queue() == 0
