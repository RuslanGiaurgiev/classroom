"""
Приём файлов от ноутбуков (HTTP, с докачкой).

    POST /api/files/init            {sha256, size, name, rel_path, folder_key}
         → {"status": "done"}                 — такой файл уже получен (повторно не нужен)
         → {"status": "upload", "offset": N}  — присылай, начиная с байта N (докачка)
    PUT  /api/files/{sha256}?offset=N  — очередной кусок файла
    POST /api/files/{sha256}/complete  — учитель проверяет SHA-256 и только тогда
                                         подтверждает получение ({"status": "done"})

Недокачанные файлы лежат в <данные>/partial/<ноутбук>/ и переживают перезапуск.
Готовые — во «Входящих»: <Проекты>/Входящие/<ГГГГ-ММ-ДД>/Ноут-<номер>/<файл>.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from teacher.server import TeacherServer

log = logging.getLogger(__name__)

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILE = 4 * 1024 * 1024 * 1024
MAX_CHUNK = 16 * 1024 * 1024


def safe_name(name: str) -> str:
    """Имя файла без путей и запрещённых символов. Кириллица сохраняется."""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f:*?"<>|]', "_", name).strip(" .")
    return name[:200] or "файл"


def unique_path(path: Path) -> Path:
    """Никогда не перезаписываем: «кот.sb3» → «кот (2).sb3»."""
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
        i += 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


class FileReceiver:
    def __init__(self, server: "TeacherServer"):
        self.server = server
        self.partial_root = server.storage.data_dir / "partial"
        self.on_file_received: list = []   # обработчики (сортировка по именам)

    def routes(self) -> list:
        return [
            web.post("/api/files/init", self.handle_init),
            web.put("/api/files/{sha}", self.handle_put),
            web.post("/api/files/{sha}/complete", self.handle_complete),
        ]

    def _laptop(self, request: web.Request) -> str:
        number = self.server._auth_agent(request)
        if number is None:
            raise web.HTTPUnauthorized(text="Неверный токен")
        return number

    def _paths(self, laptop: str, sha: str) -> tuple[Path, Path]:
        if not SHA_RE.match(sha):
            raise web.HTTPBadRequest(text="Некорректный SHA-256")
        d = self.partial_root / safe_name(f"laptop-{laptop}")
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{sha}.part", d / f"{sha}.json"

    async def handle_init(self, request: web.Request) -> web.Response:
        laptop = self._laptop(request)
        try:
            body = await request.json()
            sha = str(body["sha256"])
            size = int(body["size"])
            name = safe_name(str(body["name"]))
            rel_path = str(body.get("rel_path", name))[:500]
            folder_key = str(body.get("folder_key", ""))[:100]
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="Некорректный запрос")
        if not 0 < size <= MAX_FILE:
            raise web.HTTPBadRequest(text="Некорректный размер")
        if self.server.db.find_received(laptop, sha):
            return web.json_response({"status": "done"})
        part, meta_path = self._paths(laptop, sha)
        meta = {"sha256": sha, "size": size, "name": name, "rel_path": rel_path, "folder_key": folder_key}
        old = json.loads(meta_path.read_text()) if meta_path.exists() else None
        if old is None or old.get("size") != size:
            part.unlink(missing_ok=True)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False))
        offset = part.stat().st_size if part.exists() else 0
        return web.json_response({"status": "upload", "offset": offset})

    async def handle_put(self, request: web.Request) -> web.Response:
        laptop = self._laptop(request)
        part, meta_path = self._paths(laptop, request.match_info["sha"])
        if not meta_path.exists():
            raise web.HTTPNotFound(text="Сначала /api/files/init")
        meta = json.loads(meta_path.read_text())
        current = part.stat().st_size if part.exists() else 0
        try:
            offset = int(request.query.get("offset", "-1"))
        except ValueError:
            offset = -1
        if offset != current:
            return web.json_response({"offset": current}, status=409)
        written = 0
        with open(part, "ab") as f:
            async for chunk in request.content.iter_chunked(256 * 1024):
                written += len(chunk)
                if written > MAX_CHUNK or current + written > meta["size"]:
                    f.truncate(current)
                    raise web.HTTPBadRequest(text="Слишком много данных")
                f.write(chunk)
        return web.json_response({"offset": current + written})

    async def handle_complete(self, request: web.Request) -> web.Response:
        laptop = self._laptop(request)
        sha = request.match_info["sha"]
        part, meta_path = self._paths(laptop, sha)
        if self.server.db.find_received(laptop, sha):
            return web.json_response({"status": "done"})
        if not meta_path.exists() or not part.exists():
            raise web.HTTPNotFound(text="Нет такой загрузки")
        meta = json.loads(meta_path.read_text())
        if part.stat().st_size != meta["size"]:
            return web.json_response({"error": "файл получен не полностью"}, status=422)
        actual = await asyncio.to_thread(sha256_file, part)
        if actual != sha:
            # Повреждение при передаче — выбрасываем и просим прислать заново
            part.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            log.warning("Ноутбук %s: SHA-256 не совпал для %s", laptop, meta["name"])
            return web.json_response({"error": "контрольная сумма не совпала"}, status=422)

        today = date.today().isoformat()
        dest_dir = self.server.projects_root() / "Входящие" / today / f"Ноут-{safe_name(laptop)}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(dest_dir / meta["name"])
        shutil.move(str(part), str(dest))
        meta_path.unlink(missing_ok=True)
        file_id = self.server.db.add_received(
            laptop=laptop, sha256=sha, size=meta["size"], name=meta["name"], rel_path=meta["rel_path"],
            folder_key=meta["folder_key"], lesson_date=today, inbox_path=str(dest))
        log.info("Получен файл от ноутбука %s: %s (%s байт)", laptop, dest.name, meta["size"])
        for handler in self.on_file_received:
            try:
                await handler(file_id)
            except Exception:
                log.exception("Ошибка обработки полученного файла")
        await self.server.broadcast_event({"kind": "file", "number": laptop, "name": meta["name"], "id": file_id})
        return web.json_response({"status": "done"})
