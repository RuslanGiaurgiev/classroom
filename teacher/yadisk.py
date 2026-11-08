"""
Выгрузка разобранных проектов на Яндекс Диск (REST API).

  • OAuth-токен (право cloud_api:disk.write) хранится только в Связке ключей macOS (keyring);
  • структура на Диске повторяет локальную: <Школа>/<Группа>/<Имя>/<дата>/<файл>;
  • очередь в SQLite (disk_queue) с повторами: если пропал интернет, файлы ждут
    и отправляются позже (задержка растёт: 30 с, 1 мин, 2 мин … до 1 часа);
  • ничего не перезаписываем: если на Диске уже есть файл с таким именем, но другим
    содержимым — добавляем суффикс « (2)».

Документация API: https://yandex.ru/dev/disk-api/doc/ru/
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import aiohttp
from aiohttp import web

from teacher.inbox import safe_name

if TYPE_CHECKING:
    from teacher.server import TeacherServer

log = logging.getLogger(__name__)

API_BASE = "https://cloud-api.yandex.net/v1/disk"
KEYRING_SERVICE = "ClassroomTeacher"
KEYRING_USER = "yandex_disk_token"
MAX_BACKOFF = 3600


class DiskAuthError(Exception):
    """Токен неверный или без нужных прав."""


class KeyringTokenStore:
    """Токен в Связке ключей macOS."""

    def get(self) -> str | None:
        import keyring
        try:
            return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            log.exception("Не удалось прочитать токен из Связки ключей")
            return None

    def set(self, token: str) -> None:
        import keyring
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)

    def delete(self) -> None:
        import keyring
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


class MemoryTokenStore:
    """Для тестов."""

    def __init__(self, token: str | None = None):
        self.token = token

    def get(self):
        return self.token

    def set(self, token):
        self.token = token

    def delete(self):
        self.token = None


def _unique_remote(path: str, n: int) -> str:
    if n < 2:
        return path
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{folder}/{name} ({n})"
    return f"{folder}/{stem} ({n}).{ext}"


class YandexDisk:
    def __init__(self, server: "TeacherServer", token_store=None, api_base: str = API_BASE,
                 clock: Callable[[], float] = time.time):
        self.server = server
        self.db = server.db
        self.tokens = token_store or KeyringTokenStore()
        self.api_base = api_base
        self.clock = clock
        self.state = "idle"          # idle | working | offline | auth_error | no_token
        self.last_error = ""
        self._wake = asyncio.Event()
        self._created_dirs: set[str] = set()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()   # очередь обрабатывается строго по одному
        server.sorter.on_file_sorted.append(self.enqueue)

    # ================= Очередь =================

    def root(self) -> str:
        return safe_name(self.server.storage.settings().get("disk_root") or "Компьютерный класс")

    async def enqueue(self, file_id: int) -> None:
        f = self.db.one(
            "SELECT r.*, s.name AS student, g.name AS group_name FROM received r"
            " JOIN students s ON s.id = r.student_id JOIN groups g ON g.id = s.group_id WHERE r.id=?", (file_id,))
        if f is None or not f["project_path"]:
            return
        remote = "/".join([self.root(), f["group_name"], f["student"], f["lesson_date"], Path(f["project_path"]).name])
        # Если файл переназначили другому ученику — старую неотправленную задачу отменяем
        self.db.q("DELETE FROM disk_queue WHERE received_id=? AND status!='done'", (file_id,))
        self.db.q("INSERT OR IGNORE INTO disk_queue(received_id, local_path, remote_path, updated) VALUES (?,?,?,?)",
                  (file_id, f["project_path"], remote, self.clock()))
        self._wake.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.process_queue()
            except Exception:
                log.exception("Ошибка выгрузки на Яндекс Диск")

    async def process_queue(self) -> int:
        """Отправить всё, чему подошло время. Возвращает число выгруженных файлов."""
        async with self._lock:
            return await self._process_queue()

    async def _process_queue(self) -> int:
        token = self.tokens.get()
        if not token:
            self._set_state("no_token")
            return 0
        if self.state == "auth_error":
            return 0  # ждём, пока учитель введёт новый токен
        due = self.db.q("SELECT * FROM disk_queue WHERE status='pending' AND next_try<=? ORDER BY id",
                        (self.clock(),))
        if not due:
            if self.state not in ("offline",):
                self._set_state("idle")
            return 0
        done = 0
        self._set_state("working")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout, headers={"Authorization": f"OAuth {token}"}) as s, \
                aiohttp.ClientSession(timeout=timeout) as plain:
            for item in due:
                try:
                    await self._upload(s, plain, item)
                    done += 1
                except DiskAuthError as e:
                    self.last_error = str(e)
                    self._set_state("auth_error")
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError) as e:
                    self._retry_later(item, e)
                    if isinstance(e, (aiohttp.ClientConnectionError, asyncio.TimeoutError)):
                        self._set_state("offline")  # нет интернета — не мучаем остальные файлы
                        break
        if self.state == "working":
            self._set_state("idle")
        if done:
            await self.server.broadcast_event({"kind": "disk_changed"})
        return done

    def _retry_later(self, item: dict, err: Exception) -> None:
        attempts = item["attempts"] + 1
        delay = min(MAX_BACKOFF, 30 * 2 ** (attempts - 1))
        msg = str(err) or err.__class__.__name__
        self.db.q("UPDATE disk_queue SET attempts=?, next_try=?, last_error=?, updated=? WHERE id=?",
                  (attempts, self.clock() + delay, msg[:300], self.clock(), item["id"]))
        self.last_error = msg
        log.info("Яндекс Диск: %s — повтор через %s с (%s)", item["remote_path"], delay, msg)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            asyncio.ensure_future(self.server.broadcast_event({"kind": "disk_changed"}))

    # ================= API Яндекс Диска =================

    async def _check(self, r: aiohttp.ClientResponse, ok=(200, 201, 202)) -> dict:
        try:
            data = await r.json(content_type=None)
        except ValueError:
            data = {}
        if r.status in (401, 403):
            raise DiskAuthError("Токен Яндекс Диска недействителен или без права записи — получите новый")
        if r.status not in ok:
            raise RuntimeError(f"Яндекс Диск ответил {r.status}: {data.get('message') or data.get('description') or ''}")
        return data or {}

    async def _mkdirs(self, s: aiohttp.ClientSession, folder: str) -> None:
        parts = folder.split("/")
        for i in range(1, len(parts) + 1):
            path = "/".join(parts[:i])
            if path in self._created_dirs:
                continue
            async with s.put(f"{self.api_base}/resources", params={"path": f"disk:/{path}"}) as r:
                await self._check(r, ok=(201, 409))   # 409 — папка уже есть
            self._created_dirs.add(path)

    async def _remote_sha(self, s: aiohttp.ClientSession, remote: str) -> str | None:
        async with s.get(f"{self.api_base}/resources", params={"path": f"disk:/{remote}", "fields": "sha256"}) as r:
            if r.status == 404:
                return None
            return (await self._check(r)).get("sha256")

    async def _upload(self, s: aiohttp.ClientSession, plain: aiohttp.ClientSession, item: dict) -> None:
        """s — запросы к API с токеном; plain — загрузка по ссылке (токен туда не отправляем)."""
        local = Path(item["local_path"])
        if not local.exists():
            # Файл переложили — ищем актуальный путь
            f = self.db.one("SELECT project_path, inbox_path FROM received WHERE id=?", (item["received_id"],))
            local = Path(f["project_path"]) if f and f["project_path"] and Path(f["project_path"]).exists() \
                else Path(f["inbox_path"]) if f else local
            if not local.exists():
                raise RuntimeError(f"файл не найден на Mac: {local}")
        sha = self.db.one("SELECT sha256 FROM received WHERE id=?", (item["received_id"],))["sha256"]
        base_remote = item["remote_path"]
        await self._mkdirs(s, base_remote.rpartition("/")[0])

        for n in range(1, 50):
            remote = _unique_remote(base_remote, n)
            async with s.get(f"{self.api_base}/resources/upload",
                             params={"path": f"disk:/{remote}", "overwrite": "false"}) as r:
                if r.status == 409:
                    # Уже есть файл с таким именем. Тот же самый? Тогда готово, иначе — суффикс.
                    if await self._remote_sha(s, remote) == sha:
                        break
                    continue
                link = await self._check(r)
            with open(local, "rb") as fh:
                async with plain.put(link["href"], data=fh) as r:
                    await self._check(r, ok=(201, 202))
            break
        else:
            raise RuntimeError("слишком много файлов с одинаковым именем")

        self.db.q("UPDATE disk_queue SET status='done', remote_path=?, last_error='', updated=? WHERE id=?",
                  (remote, self.clock(), item["id"]))
        log.info("Яндекс Диск: выгружен %s", remote)

    async def check_connection(self) -> dict:
        """«Проверить подключение»: создаём (или находим) папку школы."""
        token = self.tokens.get()
        if not token:
            return {"ok": False, "message": "Токен не введён"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                             headers={"Authorization": f"OAuth {token}"}) as s:
                self._created_dirs.discard(self.root())
                await self._mkdirs(s, self.root())
                info = {}
                async with s.get(f"{self.api_base}/") as r:
                    if r.status == 200:
                        info = await r.json(content_type=None)
        except DiskAuthError as e:
            self._set_state("auth_error")
            return {"ok": False, "message": str(e)}
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
            return {"ok": False, "message": f"Нет связи с Яндекс Диском: {e}"}
        if self.state in ("auth_error", "no_token", "offline"):
            self._set_state("idle")
        self.wake()
        msg = f"Подключено. Папка на Диске: «{self.root()}»"
        if info.get("total_space"):
            free = (info["total_space"] - info.get("used_space", 0)) / 1024 ** 3
            msg += f". Свободно {free:.1f} ГБ"
        return {"ok": True, "message": msg}

    # ================= API панели =================

    def routes(self) -> list:
        return [
            web.get("/ui/api/disk", self.handle_status),
            web.post("/ui/api/disk/token", self.handle_set_token),
            web.delete("/ui/api/disk/token", self.handle_delete_token),
            web.post("/ui/api/disk/check", self.handle_check),
            web.post("/ui/api/disk/retry", self.handle_retry),
        ]

    async def handle_status(self, request: web.Request) -> web.Response:
        counts = {r["status"]: r["n"] for r in self.db.q("SELECT status, COUNT(*) AS n FROM disk_queue GROUP BY status")}
        items = self.db.q("SELECT id, remote_path, status, attempts, last_error, updated, next_try FROM disk_queue"
                          " ORDER BY (status='done'), id DESC LIMIT 200")
        st = self.server.storage.settings()
        return web.json_response({
            "has_token": bool(self.tokens.get()),
            "state": self.state,
            "last_error": self.last_error,
            "root": self.root(),
            "client_id": st.get("disk_client_id", ""),
            "counts": counts,
            "items": items,
        })

    async def handle_set_token(self, request: web.Request) -> web.Response:
        body = await request.json()
        token = str(body.get("token", "")).strip()
        if len(token) < 20 or " " in token:
            return web.json_response({"error": "Это не похоже на токен. Скопируйте его целиком."}, status=400)
        self.tokens.set(token)
        self.state = "idle"
        result = await self.check_connection()
        await self.server.broadcast_event({"kind": "disk_changed"})
        return web.json_response(result)

    async def handle_delete_token(self, request: web.Request) -> web.Response:
        self.tokens.delete()
        self._set_state("no_token")
        return web.json_response({"ok": True})

    async def handle_check(self, request: web.Request) -> web.Response:
        result = await self.check_connection()
        await self.server.broadcast_event({"kind": "disk_changed"})
        return web.json_response(result)

    async def handle_retry(self, request: web.Request) -> web.Response:
        self.db.q("UPDATE disk_queue SET status='pending', next_try=0 WHERE status IN ('pending','error')")
        if self.state in ("auth_error", "offline"):
            self.state = "idle"
        self.wake()
        return web.json_response({"ok": True})
