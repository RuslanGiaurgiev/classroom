"""
Самообновление агента.

1. Учитель присылает команду UPDATE_AGENT с номером версии.
2. Агент проверяет, что сейчас безопасно (нет заставки, отсчёта, отправки файлов).
3. Скачивает у учителя manifest + подпись → проверяет подпись (Ed25519, ключ
   зашит в агент) → скачивает agent.exe → проверяет SHA-256 и размер.
4. Заменяет %LOCALAPPDATA%\\ClassroomAgent\\bin\\agent.exe (работающий файл в Windows
   можно переименовать, но не удалить): agent.exe → agent-old.exe, новый → agent.exe.
5. Отпускает «замок» единственного экземпляра и запускает новую версию.
   Новая версия захватывает замок и создаёт файл-сигнал «готов».
6. Дождавшись сигнала (до 30 с), старая версия завершается.
   Не дождавшись — ОТКАТ: новая версия останавливается, старый файл возвращается,
   агент продолжает работать, учитель видит ошибку в панели.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import aiohttp

from agent import __version__
from agent.platform.base import Platform
from shared.update import UpdateError, is_newer, verify_manifest

if TYPE_CHECKING:
    from agent.core import AgentCore

log = logging.getLogger(__name__)

READY_TIMEOUT = 30.0


def installed_exe_path(platform: Platform) -> Path:
    return platform.data_dir() / "bin" / "agent.exe"


class Updater:
    def __init__(self, platform: Platform, core: "AgentCore", ready_timeout: float = READY_TIMEOUT,
                 exit_fn: Callable[[int], None] = os._exit, current_version: str = __version__):
        self.platform = platform
        self.core = core
        self.ready_timeout = ready_timeout
        self.exit_fn = exit_fn
        self.current_version = current_version
        self.in_progress = False
        self.last_error = ""

    @property
    def update_dir(self) -> Path:
        d = self.platform.data_dir() / "update"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def update(self, version: str) -> dict:
        if not self.platform.can_self_update():
            return {"text": "обновление работает только у собранного agent.exe", "updated": False}
        if not is_newer(version, self.current_version):
            return {"text": f"уже установлена версия {self.current_version}", "updated": False}
        if self.in_progress:
            return {"text": "обновление уже идёт", "updated": False, "deferred": True}
        busy = self.core.busy_reason()
        if busy:
            return {"text": f"обновление отложено: {busy}", "updated": False, "deferred": True}
        client = self.core.client
        if client is None or not client.base_url:
            return {"text": "нет связи с учителем", "updated": False, "deferred": True}

        self.in_progress = True
        self.last_error = ""
        try:
            new_exe = await self._download(client.base_url, client.state.token, version)
        except (UpdateError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            self.in_progress = False
            self.last_error = str(e)
            log.error("Обновление до %s не удалось: %s", version, e)
            raise UpdateError(f"обновление не удалось: {e}") from e

        # Даём отправить учителю ответ, затем меняем файлы в отдельном потоке
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, lambda: threading.Thread(target=self._apply, args=(new_exe, version, loop),
                                                      daemon=True).start())
        return {"text": f"версия {version} скачана и проверена, перезапускаюсь", "updated": True}

    async def _download(self, base_url: str, token: str, version: str) -> Path:
        headers = {"Authorization": f"Bearer {token}"}
        timeout = aiohttp.ClientTimeout(total=600, sock_connect=10)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            async with s.get(f"{base_url}/api/update/manifest") as r:
                r.raise_for_status()
                data = await r.json()
            manifest = verify_manifest(base64.b64decode(data["manifest"]), data["signature"])
            if manifest["version"] != version:
                raise UpdateError(f"учитель предлагает {manifest['version']}, а команда — {version}")
            if not is_newer(manifest["version"], self.current_version):
                raise UpdateError("версия не новее текущей (откат запрещён)")
            part = self.update_dir / f"agent-{version}.part"
            h = hashlib.sha256()
            size = 0
            async with s.get(f"{base_url}/api/update/agent.exe") as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        size += len(chunk)
                        if size > manifest["size"]:
                            raise UpdateError("файл обновления больше заявленного")
                        h.update(chunk)
                        f.write(chunk)
        if size != manifest["size"] or h.hexdigest() != manifest["sha256"]:
            part.unlink(missing_ok=True)
            raise UpdateError("контрольная сумма файла обновления не совпала")
        final = self.update_dir / f"agent-{version}.exe"
        os.replace(part, final)
        log.info("Обновление %s скачано и проверено", version)
        return final

    def _apply(self, new_exe: Path, version: str, loop: asyncio.AbstractEventLoop) -> None:
        target = installed_exe_path(self.platform)
        old = target.with_name("agent-old.exe")
        bad = target.with_name("agent-bad.exe")
        ready = self.update_dir / f"ready-{uuid.uuid4().hex}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            old.unlink(missing_ok=True)
            if target.exists():
                os.replace(target, old)          # работающий exe переименовывать можно
            shutil.copy2(new_exe, target)
        except OSError as e:
            log.exception("Не удалось подменить файл агента")
            if old.exists() and not target.exists():
                os.replace(old, target)
            self._failed(f"не удалось заменить файл: {e}", loop)
            return

        self.platform.install_autorun(target)
        self.platform.release_single_instance()
        try:
            proc = self.platform.spawn_agent(target, ["--after-update", str(ready)])
        except OSError as e:
            proc = None
            log.exception("Не удалось запустить новую версию")
        deadline = time.monotonic() + self.ready_timeout
        while proc is not None and time.monotonic() < deadline:
            if ready.exists():
                log.info("Новая версия %s запущена — старая завершает работу", version)
                self.exit_fn(0)
                return
            if proc.poll() is not None:
                break
            time.sleep(0.3)

        # ---- Откат ----
        log.error("Новая версия %s не запустилась — возвращаю старую", version)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            time.sleep(1)
        try:
            bad.unlink(missing_ok=True)
            os.replace(target, bad)
            if old.exists():
                os.replace(old, target)
            self.platform.install_autorun(target)
        except OSError:
            log.exception("Ошибка отката файлов")
        for _ in range(50):
            if self.platform.acquire_single_instance():
                break
            time.sleep(0.2)
        self._failed(f"версия {version} не запустилась, оставлена {self.current_version}", loop)

    def _failed(self, message: str, loop: asyncio.AbstractEventLoop) -> None:
        self.last_error = message
        self.in_progress = False
        client = self.core.client
        if client is not None:
            asyncio.run_coroutine_threadsafe(client.send_status(), loop)


def after_update_started(platform: Platform, ready_file: str) -> None:
    """Новая версия запустилась после обновления: сообщаем старой, что всё хорошо."""
    try:
        Path(ready_file).write_text("ok", encoding="utf-8")
    except OSError:
        log.exception("Не удалось создать сигнал готовности")


def cleanup_after_update(platform: Platform) -> None:
    """Убрать старый exe и скачанные файлы (вызывается в фоне через минуту после старта)."""
    target = installed_exe_path(platform)
    for p in (target.with_name("agent-old.exe"), target.with_name("agent-bad.exe")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass  # ещё занят завершающейся старой версией — уберём при следующем запуске
    d = platform.data_dir() / "update"
    if d.is_dir():
        for p in d.iterdir():
            try:
                p.unlink()
            except OSError:
                pass
