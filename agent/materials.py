"""
Приём материалов урока от учителя.

Команда RECEIVE_FILES → агент скачивает с Mac список файлов пакета и сами файлы:
  • несколько файлов параллельно (быстрее на множестве мелких файлов);
  • докачка с места обрыва (HTTP Range, временный файл «.part»);
  • проверка SHA-256 каждого файла;
  • если связь пропала — повторяет попытки до 30 минут;
  • файл с таким же содержимым повторно не пишется; изменённый на ноутбуке
    файл с тем же именем не перезаписывается — новый сохраняется с « (2)».
Папка материалов исключена из сбора проектов и из очистки «Конец урока».
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from agent.files import sha256_file
from agent.platform.base import Platform
from shared.materials import UnsafePath, safe_rel_path
from shared.protocol import MsgType

if TYPE_CHECKING:
    from agent.core import AgentCore

log = logging.getLogger(__name__)

DEFAULT_DEST = {"base": "desktop", "sub": "Материалы"}
PARALLEL = 4
RETRY_FOR = 30 * 60
REPORT_EVERY = 0.5


def unique_path(path: Path) -> Path:
    i = 2
    while True:
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
        i += 1


class MaterialsReceiver:
    def __init__(self, platform: Platform, core: "AgentCore", parallel: int = PARALLEL, retry_for: float = RETRY_FOR):
        self.platform = platform
        self.core = core
        self.parallel = parallel
        self.retry_for = retry_for
        self.dest = dict(DEFAULT_DEST)
        self._tasks: dict[str, asyncio.Task] = {}

    # ---------- Куда складывать ----------

    def set_dest(self, spec: dict | None) -> None:
        if not isinstance(spec, dict) or spec.get("base") not in ("desktop", "documents", "downloads"):
            return
        sub = str(spec.get("sub", "")).strip().strip("\\/").replace("\\", "/")
        try:
            sub = safe_rel_path(sub) if sub else ""
        except UnsafePath:
            return
        self.dest = {"base": spec["base"], "sub": sub}

    def root(self) -> Path:
        base = self.platform.known_folder(self.dest["base"])
        return base / self.dest["sub"] if self.dest["sub"] else base

    # ---------- Приём ----------

    def start(self, package_id: str, name: str) -> str:
        task = self._tasks.get(package_id)
        if task is not None and not task.done():
            return "уже загружается"
        self._tasks[package_id] = asyncio.create_task(self._run(package_id, name))
        return "загрузка начата"

    async def _report(self, package_id: str, state: str, done: int, total: int, message: str) -> None:
        client = self.core.client
        if client is not None:
            await client.send(MsgType.REPORT, {"kind": "materials", "package_id": package_id, "state": state,
                                               "done": done, "total": total, "message": message})

    async def _run(self, package_id: str, name: str) -> None:
        deadline = time.monotonic() + self.retry_for
        while True:
            try:
                await self._download(package_id, name)
                return
            except _Fatal as e:
                log.error("Материалы «%s»: %s", name, e)
                await self._report(package_id, "error", 0, 0, str(e))
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                if time.monotonic() > deadline:
                    await self._report(package_id, "error", 0, 0, f"не удалось скачать: {e}")
                    return
                log.info("Материалы «%s»: связь прервалась (%s), продолжу", name, e)
                await asyncio.sleep(5)

    async def _download(self, package_id: str, name: str) -> None:
        client = self.core.client
        if client is None or not client.base_url:
            raise aiohttp.ClientConnectionError("нет связи с учителем")
        base = f"{client.base_url}/api/packages/{package_id}"
        headers = {"Authorization": f"Bearer {client.state.token}"}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            async with s.get(f"{base}/manifest") as r:
                if r.status == 404:
                    raise _Fatal("пакет больше не доступен на Mac")
                r.raise_for_status()
                manifest = await r.json()

            root = self.root()
            files = []
            for f in manifest["files"]:
                try:
                    rel = safe_rel_path(f["path"])
                except UnsafePath as e:
                    raise _Fatal(f"небезопасный путь в пакете: {e}") from e
                dest = (root / rel)
                if root.resolve() not in dest.resolve().parents:
                    raise _Fatal(f"путь выходит за папку материалов: {rel}")
                files.append({**f, "dest": dest})
            total = sum(f["size"] for f in files)

            root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(root).free
            if free < total + 200 * 1024 * 1024:
                raise _Fatal(f"мало места на диске: нужно {total // 2**20} МБ, свободно {free // 2**20} МБ")

            progress = {"done": 0}
            last = {"t": 0.0}
            written = {"n": 0, "skipped": 0}

            async def report(force=False):
                if force or time.monotonic() - last["t"] >= REPORT_EVERY:
                    last["t"] = time.monotonic()
                    pct = int(progress["done"] * 100 / total) if total else 100
                    await self._report(package_id, "downloading", progress["done"], total, f"{pct}%")

            sem = asyncio.Semaphore(self.parallel)

            async def one(f):
                async with sem:
                    await self._fetch_file(s, base, f, progress, report, written)

            await report(force=True)
            await asyncio.gather(*(one(f) for f in files))

        mb = total / 2**20
        where = f"{'Рабочий стол' if self.dest['base'] == 'desktop' else self.dest['base']}\\{self.dest['sub']}".rstrip("\\")
        msg = f"получено файлов: {len(files)} ({mb:.1f} МБ) → {where}"
        if written["skipped"]:
            msg += f"; уже были на месте: {written['skipped']}"
        log.info("Материалы «%s»: %s", name, msg)
        await self._report(package_id, "done", total, total, msg)

    async def _fetch_file(self, s, base, f, progress, report, written) -> None:
        dest: Path = f["dest"]
        # Уже лежит такой же файл — не качаем
        if dest.exists() and dest.stat().st_size == f["size"] and await asyncio.to_thread(sha256_file, dest) == f["sha256"]:
            progress["done"] += f["size"]
            written["skipped"] += 1
            await report()
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + f".{f['sha256'][:8]}.part")
        offset = part.stat().st_size if part.exists() else 0
        if offset > f["size"]:
            part.unlink()
            offset = 0
        progress["done"] += offset
        req_headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with s.get(f"{base}/files/{f['i']}", headers=req_headers) as r:
            if r.status in (409, 410):
                raise _Fatal(await r.text())
            if offset and r.status == 200:      # сервер прислал файл целиком — начинаем сначала
                progress["done"] -= offset
                offset = 0
            elif r.status not in (200, 206):
                r.raise_for_status()
            with open(part, "ab" if offset else "wb") as out:
                async for chunk in r.content.iter_chunked(1024 * 1024):
                    out.write(chunk)
                    progress["done"] += len(chunk)
                    await report()
        if await asyncio.to_thread(sha256_file, part) != f["sha256"]:
            part.unlink(missing_ok=True)
            raise aiohttp.ClientPayloadError(f"контрольная сумма не совпала: {f['path']}")
        target = dest if not dest.exists() else unique_path(dest)
        os.replace(part, target)
        written["n"] += 1


class _Fatal(Exception):
    """Ошибка, которую бесполезно повторять (нет места, пакет удалён, небезопасный путь)."""
