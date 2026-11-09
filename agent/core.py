"""
Ядро агента: выполняет команды учителя. Не зависит от Windows —
всё системное делается через Platform, поэтому тестируется на Mac.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from agent.platform.base import OverlayHandle, Platform
from shared.protocol import Action, ProtocolError

log = logging.getLogger(__name__)


class OverlaySupervisor:
    """
    Следит за полноэкранным окном (сообщение, «Внимание на учителя», отсчёт выключения).
    Окно работает отдельным процессом. Если его закрыли (например, через Диспетчер
    задач), а оно ещё должно быть на экране — запускаем заново.
    Одновременно на экране только одно окно: новое заменяет старое.
    """

    def __init__(self, platform: Platform, check_interval: float = 1.0, clock=time.monotonic):
        self.platform = platform
        self.check_interval = check_interval
        self.clock = clock
        self.kind: str | None = None          # lock | message | countdown
        self.spec: dict | None = None
        self.deadline: float | None = None    # когда окно должно исчезнуть само
        self.handle: OverlayHandle | None = None
        self.restarts = 0

    def show(self, kind: str, spec: dict, seconds: float | None = None) -> bool:
        """Показать окно. Отсчёт выключения ничем не перекрывается (кроме нового отсчёта)."""
        if self.kind == "countdown" and kind != "countdown":
            return False
        self._kill()
        self.kind, self.spec = kind, {**spec, "mode": kind}
        self.deadline = self.clock() + seconds if seconds else None
        self.restarts = 0
        self.handle = self.platform.spawn_overlay(self.spec)
        log.info("Окно на экране: %s", kind)
        return True

    def clear(self, kind: str | None = None) -> bool:
        """Убрать окно. Если указан kind — только если сейчас показано именно оно."""
        if self.kind is None or (kind is not None and self.kind != kind):
            return False
        self._kill()
        log.info("Окно убрано: %s", self.kind)
        self.kind = self.spec = self.deadline = None
        return True

    def _kill(self) -> None:
        if self.handle is not None and self.handle.poll() is None:
            try:
                self.handle.terminate()
            except OSError:
                log.exception("Не удалось закрыть окно")
        self.handle = None

    def check(self) -> None:
        """Одна проверка (вызывается по таймеру)."""
        if self.kind is None:
            return
        if self.deadline is not None and self.clock() >= self.deadline:
            self.clear()
            return
        if self.handle is None or self.handle.poll() is not None:
            self.restarts += 1
            log.warning("Окно «%s» закрыли — показываю снова (раз %s)", self.kind, self.restarts)
            spec = dict(self.spec or {})
            if self.deadline is not None and "seconds" in spec:
                spec["seconds"] = max(1, int(self.deadline - self.clock()))  # отсчёт продолжается
            self.handle = self.platform.spawn_overlay(spec)

    async def run(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                log.exception("Ошибка проверки окна")
            await asyncio.sleep(self.check_interval)


class ShutdownController:
    """Выключение/перезагрузка с предупреждением и возможностью отмены."""

    def __init__(self, platform: Platform, overlay: OverlaySupervisor,
                 before_shutdown: Callable[[], Awaitable[None]] | None = None):
        self.platform = platform
        self.overlay = overlay
        self.before_shutdown = before_shutdown
        self._task: asyncio.Task | None = None
        self.pending: str | None = None  # "shutdown" | "reboot"

    def start(self, reboot: bool, seconds: int) -> None:
        self.cancel()
        self.pending = "reboot" if reboot else "shutdown"
        text = "Компьютер перезагрузится" if reboot else "Компьютер выключится"
        self.overlay.show("countdown", {"text": text, "seconds": seconds}, seconds=seconds + 5)
        self._task = asyncio.create_task(self._run(reboot, seconds))

    async def _run(self, reboot: bool, seconds: int) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        if self.before_shutdown:
            # Пока идёт отсчёт — досылаем файлы (не дольше самого отсчёта)
            try:
                await asyncio.wait_for(self.before_shutdown(), timeout=max(1, seconds - 2))
            except (asyncio.TimeoutError, Exception):
                log.warning("Не все файлы успели отправиться до выключения — отправятся при следующем запуске")
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        log.info("Выполняю: %s", self.pending)
        await asyncio.to_thread(self.platform.shutdown, reboot)

    def cancel(self) -> bool:
        was = self._task is not None and not self._task.done()
        if self._task:
            self._task.cancel()
        self._task = None
        self.pending = None
        self.overlay.clear("countdown")
        return was


class AgentCore:
    def __init__(self, platform: Platform):
        self.platform = platform
        self.overlay = OverlaySupervisor(platform)
        self.shutdown = ShutdownController(platform, self.overlay, before_shutdown=self._flush_files)
        self.collector = None  # сбор файлов (agent/collector.py), подключается в main
        from agent.materials import MaterialsReceiver
        from agent.updater import Updater
        self.updater = Updater(platform, self)
        self.materials = MaterialsReceiver(platform, self)
        self.client = None     # AgentClient — задаётся клиентом

    def start_background(self) -> list[asyncio.Task]:
        """Фоновые задачи ядра (запускает клиент)."""
        tasks = [asyncio.create_task(self.overlay.run())]
        if self.collector is not None:
            tasks += self.collector.start_background()
        return tasks

    def on_connected(self) -> None:
        if self.collector is not None:
            self.collector.wake()

    def apply_config(self, config: dict) -> None:
        self.materials.set_dest(config.get("materials"))
        if self.collector is not None:
            self.collector.excluded_roots = self.materials_roots
            self.collector.apply_config(config)

    def materials_roots(self) -> list:
        try:
            return [self.materials.root()]
        except OSError:
            return []

    def busy_reason(self) -> str | None:
        """Почему сейчас нельзя перезапускать агент (для обновления)."""
        if self.overlay.kind == "lock":
            return "включена заставка «Внимание на учителя»"
        if self.overlay.kind == "message":
            return "на экране сообщение"
        if self.shutdown.pending:
            return "запланировано выключение"
        if self.collector is not None and self.collector.is_uploading():
            return "отправляются файлы"
        if any(not t.done() for t in self.materials._tasks.values()):
            return "загружаются материалы урока"
        return None

    def status(self) -> dict:
        """Статус для учителя: что на экране, запланировано ли выключение, сколько файлов ждёт отправки."""
        return {
            "screen": self.overlay.kind,
            "shutdown": self.shutdown.pending,
            "pending_files": self.collector.pending_count() if self.collector is not None else 0,
            "update_error": self.updater.last_error,
        }

    async def _flush_files(self) -> None:
        if self.collector is not None:
            await self.collector.flush()

    async def execute(self, action: Action, params: dict) -> str | dict:
        """Выполнить команду. Результат — текст для журнала или словарь {"text": ..., данные}."""
        p = self.platform
        if action is Action.OPEN_URLS:
            return await asyncio.to_thread(p.open_urls, params["urls"], params["browser"])
        if action is Action.CLOSE_APPS:
            return await asyncio.to_thread(p.close_processes, params["process_names"])
        if action is Action.SHOW_MESSAGE:
            if not self.overlay.show("message", {"text": params["text"], "seconds": params["seconds"]},
                                     seconds=params["seconds"] or None):
                return "идёт отсчёт выключения — сообщение не показано"
            return "сообщение показано"
        if action is Action.HIDE_MESSAGE:
            return "сообщение убрано" if self.overlay.clear("message") else "сообщения не было"
        if action is Action.LOCK_SCREEN:
            if not self.overlay.show("lock", {"text": params["text"]}):
                return "идёт отсчёт выключения — заставка не показана"
            return "заставка включена"
        if action is Action.UNLOCK_SCREEN:
            return "заставка снята" if self.overlay.clear("lock") else "заставки не было"
        if action in (Action.SHUTDOWN, Action.REBOOT):
            self.shutdown.start(action is Action.REBOOT, params["seconds"])
            word = "перезагрузка" if action is Action.REBOOT else "выключение"
            return f"{word} через {params['seconds']} с"
        if action is Action.CANCEL_SHUTDOWN:
            return "выключение отменено" if self.shutdown.cancel() else "выключение не было запланировано"
        if action is Action.COLLECT_NOW:
            if self.collector is None:
                return "сбор файлов не настроен"
            r = await self.collector.collect_now()
            text = f"отправлено {r['sent']}, ждут отправки {r['pending']}"
            if r["not_ready"]:
                text += f", ещё не готовы: {', '.join(r['not_ready'][:5])}"
            return {"text": text, **r}
        if action is Action.CLEANUP:
            if self.collector is None:
                raise ProtocolError("сбор файлов не настроен")
            r = await self.collector.cleanup(set(params["confirmed_sha256"]))
            text = f"убрано в корзину {r['moved']}" + (f", оставлено {len(r['kept'])}" if r["kept"] else "")
            return {"text": text, **r}
        if action is Action.RECEIVE_FILES:
            return self.materials.start(params["package_id"], params["name"])
        if action in (Action.LIST_OPEN, Action.CLOSE_WINDOW, Action.CLOSE_TAB):
            try:
                if action is Action.LIST_OPEN:
                    data = await asyncio.to_thread(p.list_open)
                    n_tabs = sum(len(w.get("tabs") or []) for w in data["windows"])
                    return {"text": f"окон: {len(data['windows'])}, вкладок: {n_tabs}", **data}
                if action is Action.CLOSE_WINDOW:
                    return await asyncio.to_thread(p.close_window, params["window"], params["force"])
                return await asyncio.to_thread(p.close_tab, params["window"], params["title"], params["index"])
            except NotImplementedError as e:
                raise ProtocolError(str(e)) from e
        if action is Action.UPDATE_AGENT:
            from shared.update import UpdateError
            try:
                return await self.updater.update(params["version"])
            except UpdateError as e:
                raise ProtocolError(str(e)) from e
        raise ProtocolError(f"Действие не поддерживается: {action}")
