"""
Связь агента с учителем: постоянное WebSocket-соединение
с автоматическим переподключением (экспоненциальная задержка).
"""

from __future__ import annotations

import asyncio
import logging
import random

import aiohttp

from agent import __version__
from agent.core import AgentCore
from agent.discovery import broadcast_discover, find_teacher, sweep_targets
from agent.platform.base import Platform
from agent.state import AgentState
from shared.protocol import (
    Action,
    Message,
    MsgType,
    ProtocolError,
    validate_command,
)

log = logging.getLogger(__name__)

# Коды закрытия соединения, которые присылает учитель
CLOSE_REPLACED = 4001   # тот же ноутбук подключился ещё раз
CLOSE_PROTOCOL = 4002   # несовместимая версия протокола
CLOSE_UNPAIRED = 4003   # учитель отвязал ноутбук


class Backoff:
    """Экспоненциальная задержка: 1, 2, 4, 8 … до max секунд, со случайным разбросом ±20%,
    чтобы 11 ноутбуков не ломились к учителю в одну и ту же секунду."""

    def __init__(self, initial: float = 1.0, maximum: float = 30.0, factor: float = 2.0, jitter: float = 0.2):
        self.initial, self.maximum, self.factor, self.jitter = initial, maximum, factor, jitter
        self.attempt = 0

    def next_delay(self) -> float:
        base = min(self.maximum, self.initial * (self.factor ** self.attempt))
        self.attempt += 1
        return base * random.uniform(1 - self.jitter, 1 + self.jitter)

    def reset(self) -> None:
        self.attempt = 0


class AgentClient:
    def __init__(
        self,
        platform: Platform,
        state: AgentState,
        *,
        use_mdns: bool = True,
        backoff: Backoff | None = None,
        core: AgentCore | None = None,
    ):
        self.platform = platform
        self.state = state
        self.use_mdns = use_mdns
        self.backoff = backoff or Backoff()
        self.core = core or AgentCore(platform)
        # Ядру (сбору файлов, материалам) нужен адрес учителя и способ слать статусы.
        # Если учителей несколько, это место занимает AgentHub — не перетираем его.
        if not getattr(self.core.client, "is_hub", False):
            self.core.client = self
        if self.core.collector is not None and not getattr(self.core.collector.client, "is_hub", False):
            self.core.collector.client = self
        self.hub = None   # AgentHub, если ноутбук привязан к нескольким учителям
        self.base_url: str | None = None  # http://ip:порт учителя, пока есть связь
        self.connected = asyncio.Event()
        self.unpaired = False   # учитель отвязал ноутбук — работать дальше нет смысла
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._stop = False

    # ---------- Основной цикл ----------

    async def run(self) -> None:
        """Работает, пока не вызвали stop() или учитель не отвязал ноутбук."""
        # Фоновые задачи ядра (сбор файлов, окна) запускает хаб — один раз на все соединения
        background = self.core.start_background() if self.hub is None else []
        try:
            await self._run_loop()
        finally:
            for t in background:
                t.cancel()

    async def _run_loop(self) -> None:
        while not self._stop and not self.unpaired:
            connected_ok = False
            for host, port in await self._candidates():
                if self._stop:
                    break
                try:
                    connected_ok = await self._session(host, port)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    log.info("Нет связи с %s:%s: %s", host, port, e)
                    continue
                except Exception:
                    log.exception("Непредвиденная ошибка соединения")
                if connected_ok or self.unpaired:
                    break
            if self._stop or self.unpaired:
                break
            if connected_ok:
                self.backoff.reset()  # соединение было рабочим — пробуем сразу
            delay = self.backoff.next_delay()
            log.info("Переподключение через %.1f с", delay)
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            await self._ws.close()

    async def _candidates(self) -> list[tuple[str, int]]:
        """Куда подключаться: адреса из mDNS и широковещательного поиска, потом последний известный."""
        result: list[tuple[str, int]] = []
        if self.use_mdns:
            found = await find_teacher(self.state.teacher_id)
            found += [(f["ip"], int(f["port"])) for f in await broadcast_discover(self.state.port)
                      if f.get("teacher_id") == self.state.teacher_id]
            if not found:
                # Широковещание и mDNS не прошли (точка доступа телефона, VPN) — спрашиваем каждый адрес
                # своей сети напрямую: обычные соединения в таких сетях работают
                found += [(f["ip"], int(f["port"])) for f in
                          await broadcast_discover(self.state.port, targets=sweep_targets())
                          if f.get("teacher_id") == self.state.teacher_id]
            for addr in found:
                if addr not in result:
                    result.append(addr)
            if result and result[0] != (self.state.host, self.state.port):
                log.info("Учитель найден на %s", result)
        # Известные адреса: последний и прежние (другие сети — роутер, точка доступа)
        for addr in [(self.state.host, self.state.port), *self.state.known_hosts]:
            if addr not in result:
                result.append(addr)
        return result

    async def _session(self, host: str, port: int) -> bool:
        """Одно соединение. Возвращает True, если оно было установлено (hello/welcome прошли)."""
        url = f"ws://{host}:{port}/ws/agent"
        headers = {"Authorization": f"Bearer {self.state.token}"}
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                ws = await session.ws_connect(url, headers=headers, heartbeat=20, max_msg_size=1024 * 1024)
            except aiohttp.WSServerHandshakeError as e:
                if e.status == 401:
                    log.warning("Учитель не принимает токен — ноутбук отвязан")
                    self.unpaired = True
                    return False
                raise
            self._ws = ws
            try:
                await ws.send_str(Message(MsgType.HELLO, self._hello_payload()).to_json())
                first = await asyncio.wait_for(ws.receive(), 10)
                if first.type != aiohttp.WSMsgType.TEXT:
                    self._check_close_code(ws)
                    return False
                welcome = Message.from_json(first.data)
                if welcome.type == MsgType.ERROR:
                    log.error("Учитель сообщил об ошибке: %s", welcome.payload.get("message"))
                    return False
                if welcome.type != MsgType.WELCOME:
                    return False

                # Запоминаем рабочий адрес — пригодится, если mDNS не сработает
                if (host, port) != (self.state.host, self.state.port) or \
                        (self.state.known_hosts[:1] != [(host, port)]):
                    self.state.remember(host, port)
                    self.state.save(self.platform)
                log.info("На связи с учителем %s:%s как ноутбук %s", host, port, self.state.laptop_number)
                self.backoff.reset()
                self.base_url = f"http://{host}:{port}"
                self.connected.set()
                if self.hub is not None:
                    self.hub.on_link_connected(self)
                self.core.on_connected()

                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        msg = Message.from_json(raw.data)
                    except ProtocolError as e:
                        log.warning("Плохое сообщение от учителя: %s", e)
                        continue
                    if msg.type == MsgType.COMMAND:
                        asyncio.create_task(self._handle_command(ws, msg))
                    elif msg.type == MsgType.CONFIG:
                        if self.hub is None or self.hub.is_active(self):
                            self.core.apply_config(msg.payload)   # настройки сбора — от ведущего урок
                self._check_close_code(ws)
                return True
            finally:
                self.connected.clear()
                self.base_url = None
                self._ws = None
                if self.hub is not None:
                    self.hub.on_link_disconnected(self)
                if not ws.closed:
                    await ws.close()

    def _check_close_code(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        if ws.close_code == CLOSE_UNPAIRED:
            log.warning("Учитель отвязал этот ноутбук")
            self.unpaired = True
        elif ws.close_code == CLOSE_PROTOCOL:
            log.error("Несовместимая версия протокола — обновите агент")

    def _hello_payload(self) -> dict:
        return {
            "laptop_number": self.state.laptop_number,
            "username": self.platform.username(),
            "hostname": self.platform.hostname(),
            "agent_version": __version__,
            "browsers": self.platform.detect_browsers(),
            **self.core.status(),
        }

    async def send(self, mtype: MsgType, payload: dict) -> bool:
        """Отправить сообщение учителю, если есть связь."""
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_str(Message(mtype, payload).to_json())
            return True
        except (ConnectionError, RuntimeError):
            return False

    async def send_status(self) -> None:
        """Сообщить учителю свежий статус (экран, выключение, файлы в очереди)."""
        await self.send(MsgType.STATUS, self.core.status())

    # ---------- Команды ----------

    async def _handle_command(self, ws: aiohttp.ClientWebSocketResponse, msg: Message) -> None:
        command_id = str(msg.payload.get("command_id", ""))
        if self.hub is not None:
            self.hub.mark_active(self)   # этот учитель сейчас ведёт урок — его работы и получают
        try:
            # Повторная проверка на стороне агента: выполняем только разрешённое
            action, params = validate_command(str(msg.payload.get("action", "")), msg.payload.get("params") or {})
            out = await self._execute(action, params)
            if isinstance(out, dict):
                result = {"command_id": command_id, "ok": True, "message": out.get("text", ""),
                          "data": {k: v for k, v in out.items() if k != "text"}}
            else:
                result = {"command_id": command_id, "ok": True, "message": out}
        except ProtocolError as e:
            log.warning("Отклонена команда: %s", e)
            result = {"command_id": command_id, "ok": False, "message": str(e)}
        except Exception as e:
            log.exception("Ошибка выполнения команды")
            result = {"command_id": command_id, "ok": False, "message": f"ошибка: {e}"}
        if not ws.closed:
            await ws.send_str(Message(MsgType.RESULT, result).to_json())
        await self.send_status()

    async def _execute(self, action: Action, params: dict) -> str | dict:
        return await self.core.execute(action, params)

