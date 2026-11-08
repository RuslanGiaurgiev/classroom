"""
Обновления агентов через Mac учителя.

  • Раз в час (и при запуске) смотрим последний релиз на GitHub (tag «agent-vX.Y.Z»)
    по прямым ссылкам на файлы — без API, у которого есть лимит запросов.
  • Найдя версию новее — скачиваем manifest.json, manifest.sig и agent.exe,
    проверяем подпись и SHA-256 (как будет проверять и сам агент).
  • Раздаём ноутбукам по локальной сети (с токеном ноутбука):
        GET /api/update/manifest   GET /api/update/agent.exe
  • Автообновление (настройка, по умолчанию включено): каждому подключённому
    ноутбуку со старой версией отправляем UPDATE_AGENT. Агент сам откажется,
    если сейчас неудобно (заставка, отправка файлов…) — повторим через 10 минут.
    Пока идёт «Конец урока», обновления не отправляются.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from shared.protocol import Action
from shared.update import UpdateError, is_newer, verify_manifest

if TYPE_CHECKING:
    from teacher.server import AgentConn, TeacherServer

log = logging.getLogger(__name__)

DEFAULT_REPO = "RuslanGiaurgiev/classroom"
CHECK_EVERY = 3600          # проверка GitHub, секунд
PUSH_EVERY = 600            # повторная попытка обновить ноутбуки, секунд
MAX_FAILS_PER_VERSION = 3   # после стольких неудач ноутбук не трогаем, пока не выйдет новая версия


class UpdateManager:
    def __init__(self, server: "TeacherServer", github_base: str = "https://github.com"):
        self.server = server
        self.github_base = github_base
        self.dir = server.storage.data_dir / "updates"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.latest: dict | None = self._load_local()   # {"version", "manifest_b64", "signature", "path"}
        self.last_check: float | None = None
        self.last_error = ""
        self.status: dict[str, str] = {}               # ноутбук → что происходит с обновлением
        self._fails: dict[tuple[str, str], int] = {}   # (ноутбук, версия) → неудачных попыток
        self._last_try: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._check_lock = asyncio.Lock()
        server.on_agent_connected.append(self._on_connected)

    # ================= Хранение скачанной версии =================

    def _load_local(self) -> dict | None:
        """Последняя проверенная версия, скачанная раньше (переживает перезапуск Mac)."""
        meta = self.dir / "latest.json"
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            manifest = verify_manifest(base64.b64decode(info["manifest_b64"]), info["signature"])
            path = Path(info["path"])
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
                raise UpdateError("файл повреждён")
            return {**info, "version": manifest["version"]}
        except (OSError, ValueError, KeyError, UpdateError):
            return None

    def settings(self) -> dict:
        st = self.server.storage.settings()
        return {"auto": st.get("auto_update", True), "repo": st.get("update_repo") or DEFAULT_REPO}

    # ================= Проверка GitHub =================

    async def check(self) -> dict:
        async with self._check_lock:
            try:
                found = await self._check_github()
                self.last_error = ""
            except (aiohttp.ClientError, asyncio.TimeoutError, UpdateError, OSError, KeyError, ValueError) as e:
                self.last_error = f"проверка обновлений не удалась: {e}"
                log.warning(self.last_error)
                found = None
            self.last_check = time.time()
            await self.server.broadcast_state()
            if found:
                asyncio.ensure_future(self.push_all())
            return self.public()

    async def _check_github(self) -> str | None:
        """
        Без API GitHub (у него лимит 60 запросов в час на адрес): берём файлы по прямым
        ссылкам последнего релиза. Сначала маленький подписанный manifest.json —
        если версия не новее, большой agent.exe даже не скачиваем.
        """
        repo = self.settings()["repo"]
        base = f"{self.github_base}/{repo}/releases"
        timeout = aiohttp.ClientTimeout(total=300, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "ClassroomTeacher"}) as s:
            async with s.get(f"{base}/latest/download/manifest.json") as r:
                if r.status == 404:
                    return None  # релизов ещё нет
                r.raise_for_status()
                manifest_bytes = await r.read()
            async with s.get(f"{base}/latest/download/manifest.sig") as r:
                r.raise_for_status()
                signature = (await r.text()).strip()
            manifest = verify_manifest(manifest_bytes, signature)
            version = manifest["version"]
            if self.latest and not is_newer(version, self.latest["version"]):
                return None
            path = self.dir / f"agent-{version}.exe"
            h = hashlib.sha256()
            size = 0
            # Сам файл — строго из релиза этой версии
            async with s.get(f"{base}/download/agent-v{version}/agent.exe") as r:
                r.raise_for_status()
                with open(path.with_suffix(".part"), "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        size += len(chunk)
                        if size > manifest["size"]:
                            raise UpdateError("файл больше заявленного")
                        h.update(chunk)
                        f.write(chunk)
            if size != manifest["size"] or h.hexdigest() != manifest["sha256"]:
                path.with_suffix(".part").unlink(missing_ok=True)
                raise UpdateError("контрольная сумма agent.exe не совпала")
            path.with_suffix(".part").replace(path)
        self._set_latest(version, manifest_bytes, signature, path)
        log.info("Доступна новая версия агента %s", version)
        return version

    def _set_latest(self, version: str, manifest_bytes: bytes, signature: str, path: Path) -> None:
        info = {"version": version, "manifest_b64": base64.b64encode(manifest_bytes).decode(),
                "signature": signature, "path": str(path)}
        (self.dir / "latest.json").write_text(json.dumps(info), encoding="utf-8")
        # Старые скачанные версии больше не нужны
        for p in self.dir.glob("agent-*.exe"):
            if p != path:
                p.unlink(missing_ok=True)
        self.latest = info

    # ================= Раздача ноутбукам =================

    def needs_update(self, conn: "AgentConn") -> bool:
        return bool(self.latest) and is_newer(self.latest["version"], conn.agent_version or "0.0.0")

    async def push(self, number: str, force: bool = False) -> str:
        srv = self.server
        conn = srv.agents.get(number)
        if conn is None or not self.latest or not self.needs_update(conn):
            return "не нужно"
        version = self.latest["version"]
        if srv.end_lesson.running:
            return "отложено: идёт «Конец урока»"
        if not force and self._fails.get((number, version), 0) >= MAX_FAILS_PER_VERSION:
            return "пропущено: несколько неудачных попыток"
        if conn.agent_version and not is_newer(conn.agent_version, "0.2.0"):
            # 0.2.0 и старше ещё не умеют обновляться сами
            self.status[number] = "старая версия — обновите один раз вручную"
            return self.status[number]
        self._last_try[number] = time.time()
        self.status[number] = f"обновляется до {version}…"
        await srv.broadcast_state()
        res = await srv.command_and_wait(number, Action.UPDATE_AGENT, {"version": version}, timeout=300)
        data = (res or {}).get("data") or {}
        if res and res.get("ok") and data.get("updated"):
            self.status[number] = f"перезапускается с версией {version}"
        elif res and res.get("ok") and data.get("deferred"):
            self.status[number] = res.get("message", "отложено")
        else:
            self._fails[(number, version)] = self._fails.get((number, version), 0) + 1
            self.status[number] = "ошибка: " + ((res or {}).get("message") or "нет ответа")
        await srv.broadcast_state()
        return self.status[number]

    def on_agent_failed(self, number: str, error: str) -> None:
        """Агент не смог обновиться и вернул старую версию."""
        if self.latest:
            key = (number, self.latest["version"])
            self._fails[key] = self._fails.get(key, 0) + 1
        self.status[number] = "ошибка: " + error

    async def push_all(self, force: bool = False) -> None:
        if not self.latest:
            return
        async def one(number):
            try:
                await self.push(number, force=force)
            except Exception:
                log.exception("Ошибка обновления ноутбука %s", number)
        await asyncio.gather(*(one(n) for n, c in list(self.server.agents.items()) if self.needs_update(c)))

    async def _on_connected(self, conn: "AgentConn") -> None:
        # Версия уже свежая — убираем прошлый статус
        if not self.needs_update(conn):
            if self.status.pop(conn.number, None) is not None and self.latest:
                log.info("Ноутбук %s обновлён до %s", conn.number, conn.agent_version)
            return
        if self.settings()["auto"]:
            # Даём ноутбуку минуту освоиться (дослать файлы) и обновляем
            async def later():
                await asyncio.sleep(60)
                await self.push(conn.number)
            self._tasks.append(asyncio.ensure_future(later()))

    # ================= Фон =================

    def start(self) -> None:
        self._tasks.append(asyncio.ensure_future(self._loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        last_check = 0.0
        while True:
            try:
                if time.time() - last_check >= CHECK_EVERY:
                    last_check = time.time()
                    await self.check()
                if self.settings()["auto"]:
                    await self.push_all()
            except Exception:
                log.exception("Ошибка фоновых обновлений")
            await asyncio.sleep(PUSH_EVERY)

    # ================= API =================

    def public(self) -> dict:
        return {
            "latest": self.latest["version"] if self.latest else None,
            "auto": self.settings()["auto"],
            "last_check": self.last_check,
            "error": self.last_error,
            "status": self.status,
        }

    def routes(self) -> list:
        return [
            web.get("/api/update/manifest", self.handle_manifest),
            web.get("/api/update/agent.exe", self.handle_exe),
            web.post("/ui/api/updates/check", self.handle_check),
            web.post("/ui/api/updates/push", self.handle_push),
        ]

    def _require_agent(self, request: web.Request) -> str:
        number = self.server._auth_agent(request)
        if number is None:
            raise web.HTTPUnauthorized(text="Неверный токен")
        if not self.latest:
            raise web.HTTPNotFound(text="Обновления нет")
        return number

    async def handle_manifest(self, request: web.Request) -> web.Response:
        self._require_agent(request)
        return web.json_response({"manifest": self.latest["manifest_b64"], "signature": self.latest["signature"]})

    async def handle_exe(self, request: web.Request) -> web.StreamResponse:
        self._require_agent(request)
        return web.FileResponse(self.latest["path"], headers={"Content-Type": "application/octet-stream"})

    async def handle_check(self, request: web.Request) -> web.Response:
        return web.json_response(await self.check())

    async def handle_push(self, request: web.Request) -> web.Response:
        body = await request.json()
        targets = body.get("targets", "all")
        numbers = list(self.server.agents) if targets == "all" else [str(n) for n in targets]
        results = {}
        for n in numbers:
            results[n] = await self.push(n, force=True)
        return web.json_response({"results": results})
