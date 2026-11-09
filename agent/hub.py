"""
Несколько учителей у одного ноутбука (например, два препода на одной площадке).

AgentHub держит отдельное постоянное соединение с КАЖДЫМ учителем, к которому привязан
ноутбук, и всё время ищет в сети других учителей:
  • привязка к новому учителю НЕ отвязывает от прежних — ноутбук на связи у всех;
  • новый учитель, найденный в сети, видит ноутбук в «Ноутбуки рядом» и подключает его
    кнопкой «Подключить» (на ноутбуке ничего делать не нужно);
  • если учитель отвязал ноутбук — пропадает только этот учитель, остальные остаются;
  • если учитель нажал «Отклонить» — 12 часов больше не просимся к нему.

Работы учеников, отчёты и загрузки идут «ведущему» учителю — тому, кто последним
прислал команду (открыл сайт, отправил материалы, нажал «Конец урока»…); пока команд
не было — тому, кто подключился. Статус (сколько файлов ждёт) видят все учителя.

Для сбора файлов, материалов и обновлений хаб выглядит как обычный клиент:
у него есть base_url, state.token, send() и send_status() — ведущего учителя.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import aiohttp

from agent.client import AgentClient
from agent.core import AgentCore
from agent.discovery import broadcast_discover, sweep_targets
from agent.pairing import PairingCancelled, PairingFlow
from agent.platform.base import Platform
from agent.state import AgentConfig, AgentState
from shared.protocol import DEFAULT_PORT, MsgType

log = logging.getLogger(__name__)

WATCH_INTERVAL = 10.0          # как часто искать в сети новых учителей, секунд
DECLINE_FOR = 12 * 3600        # после «Отклонить» не просимся к этому учителю столько секунд


class _NoTeacher:
    """Заглушка state, пока ни с кем нет связи (base_url в это время None — загрузки не идут)."""
    token = ""
    laptop_number = ""
    teacher_id = ""


class AgentHub:
    is_hub = True

    def __init__(
        self,
        platform: Platform,
        core: AgentCore,
        config: AgentConfig,
        *,
        make_client: Callable[[AgentState], AgentClient] | None = None,
        discover_port: int = DEFAULT_PORT,
        discover_targets: list[str] | None = None,
        watch_interval: float = WATCH_INTERVAL,
        poll_interval: float = 1.5,
        watch: bool = True,
    ):
        self.platform = platform
        self.core = core
        self.config = config
        self.make_client = make_client or (lambda st: AgentClient(platform, st, core=core))
        self.discover_port = discover_port
        self.discover_targets = discover_targets
        self.watch_interval = watch_interval
        self.poll_interval = poll_interval
        self.watch = watch
        self.links: dict[str, AgentClient] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._offers: dict[str, asyncio.Task] = {}
        self._active: AgentClient | None = None
        self._stop: asyncio.Event | None = None   # создаётся в run() — внутри своего цикла событий
        # Ядро, сбор файлов и материалы общаются с учителем через хаб
        core.client = self
        if core.collector is not None:
            core.collector.client = self

    # ================= «Как обычный клиент» — ведущий учитель =================

    def _current(self) -> AgentClient | None:
        if self._active is not None and self._active.connected.is_set():
            return self._active
        return next((c for c in self.links.values() if c.connected.is_set()), None)

    @property
    def base_url(self) -> str | None:
        c = self._current()
        return c.base_url if c else None

    @property
    def state(self):
        c = self._current()
        return c.state if c else _NoTeacher()

    @property
    def connected_teachers(self) -> list[str]:
        return [tid for tid, c in self.links.items() if c.connected.is_set()]

    async def send(self, mtype: MsgType, payload: dict) -> bool:
        c = self._current()
        return await c.send(mtype, payload) if c else False

    async def send_status(self) -> None:
        """Статус (экран, файлы в очереди) — всем учителям на связи."""
        for c in list(self.links.values()):
            if c.connected.is_set():
                await c.send_status()

    # ================= Кто ведёт урок =================

    def mark_active(self, link: AgentClient) -> None:
        if self._active is not link:
            log.info("Ведущий учитель: %s (ноутбук № %s)", link.state.teacher_id, link.state.laptop_number)
        self._active = link

    def is_active(self, link: AgentClient) -> bool:
        return self._current() is link

    def on_link_connected(self, link: AgentClient) -> None:
        if self._active is None or not self._active.connected.is_set():
            self._active = link

    def on_link_disconnected(self, link: AgentClient) -> None:
        if self._active is link:
            self._active = None

    # ================= Работа =================

    def start_link(self, account: AgentState) -> None:
        tid = account.teacher_id
        if tid in self.links:
            return
        client = self.make_client(account)
        client.hub = self
        self.links[tid] = client
        self._tasks[tid] = asyncio.create_task(self._run_link(tid, client))
        log.info("Связь с учителем %s (ноутбук № %s)", tid, account.laptop_number)

    async def _run_link(self, tid: str, client: AgentClient) -> None:
        try:
            await client.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка связи с учителем %s", tid)
        if client.unpaired:
            # Этот учитель отвязал ноутбук — забываем только его, остальные остаются на связи
            log.warning("Учитель %s отвязал ноутбук — остальные учителя остаются", tid)
            self.config.remove(tid)
            self.config.save(self.platform)
        self.links.pop(tid, None)
        self._tasks.pop(tid, None)
        if self._active is client:
            self._active = None

    async def run(self) -> None:
        self._stop = asyncio.Event()
        background = self.core.start_background()   # сбор файлов, окна — один раз на весь ноутбук
        watcher = asyncio.create_task(self._watch()) if self.watch else None
        try:
            for account in list(self.config.accounts):
                self.start_link(account)
            await self._stop.wait()
        finally:
            for t in [*self._tasks.values(), *self._offers.values(), *background, *([watcher] if watcher else [])]:
                t.cancel()
            for c in list(self.links.values()):
                await c.stop()

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    # ================= Поиск новых учителей =================

    def _declined_path(self):
        return self.platform.data_dir() / "declined_teachers.json"

    def _declined(self) -> dict[str, float]:
        try:
            data = json.loads(self._declined_path().read_text(encoding="utf-8"))
            now = time.time()
            return {k: float(v) for k, v in data.items() if now - float(v) < DECLINE_FOR}
        except (OSError, ValueError, AttributeError):
            return {}

    def _decline(self, tid: str) -> None:
        d = self._declined()
        d[tid] = time.time()
        try:
            self._declined_path().write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass

    async def _watch(self) -> None:
        rounds = 0
        while True:
            try:
                found = await broadcast_discover(self.discover_port, targets=self.discover_targets)
                if not found and self.discover_targets is None and rounds % 3 == 2:
                    found = await broadcast_discover(self.discover_port, targets=sweep_targets())
                declined = self._declined()
                for f in found:
                    tid = f.get("teacher_id")
                    if not tid or tid in self.links or tid in self._offers or tid in declined:
                        continue
                    self._offers[tid] = asyncio.create_task(self._offer(tid, f["ip"], int(f["port"])))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка поиска учителей")
            rounds += 1
            await asyncio.sleep(self.watch_interval)

    async def _offer(self, tid: str, ip: str, port: int) -> None:
        """Попроситься к учителю: в его панели ноутбук появится в «Ноутбуки рядом»."""
        flow = PairingFlow(self.platform, discover_port=port, discover_targets=[ip], poll_interval=self.poll_interval)
        try:
            account = await flow._request_and_wait(ip, port)
        except PairingCancelled:
            log.info("Учитель %s отклонил подключение — не просимся 12 часов", tid)
            self._decline(tid)
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, KeyError, ValueError) as e:
            log.info("Не удалось попроситься к учителю %s: %s", tid, e)
            return
        finally:
            self._offers.pop(tid, None)
        self.config.add(account)
        self.config.save(self.platform)
        log.info("Подключён к новому учителю %s как ноутбук № %s", account.teacher_id, account.laptop_number)
        self.start_link(account)
