"""
Резервные копии данных учителя (группы, ученики, история работ, настройки).

Копия делается при запуске приложения и каждые 3 часа в папку
<папка работ>/Резервные копии/<дата_время>/. Хранятся последние 30 копий.
Копирование SQLite — штатным способом (backup API), безопасно при работающем приложении.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from teacher.server import TeacherServer

log = logging.getLogger(__name__)

KEEP = 30
EVERY = 3 * 3600


def make_backup(data_dir: Path, dest_root: Path, keep: int = KEEP) -> Path:
    dest = dest_root / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest.mkdir(parents=True, exist_ok=True)
    db = data_dir / "teacher.sqlite3"
    if db.exists():
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(dest / "teacher.sqlite3")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for name in ("state.json",):
        if (data_dir / name).exists():
            shutil.copy2(data_dir / name, dest / name)
    # Старые копии — удаляем, оставляем последние keep
    copies = sorted(p for p in dest_root.iterdir() if p.is_dir())
    for old in copies[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    log.info("Резервная копия: %s", dest)
    return dest


class Backups:
    def __init__(self, server: "TeacherServer"):
        self.server = server
        self.last: str | None = None
        self._task: asyncio.Task | None = None

    def dest_root(self) -> Path:
        return self.server.projects_root() / "Резервные копии"

    async def backup_now(self) -> Path:
        path = await asyncio.to_thread(make_backup, self.server.storage.data_dir, self.dest_root())
        self.last = path.name
        return path

    def start(self) -> None:
        async def loop():
            while True:
                try:
                    await self.backup_now()
                except Exception:
                    log.exception("Не удалось сделать резервную копию")
                await asyncio.sleep(EVERY)
        self._task = asyncio.ensure_future(loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def routes(self) -> list:
        return [web.post("/ui/api/backup", self.handle_backup), web.get("/ui/api/backup", self.handle_info)]

    async def handle_backup(self, request: web.Request) -> web.Response:
        try:
            path = await self.backup_now()
        except Exception as e:
            return web.json_response({"error": f"Не удалось сделать копию: {e}"}, status=500)
        return web.json_response({"path": str(path)})

    async def handle_info(self, request: web.Request) -> web.Response:
        root = self.dest_root()
        copies = sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True) if root.exists() else []
        return web.json_response({"root": str(root), "copies": copies[:KEEP]})
