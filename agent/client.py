"""
Связь агента с учителем: постоянное WebSocket-соединение
с автоматическим переподключением (экспоненциальная задержка).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable

import aiohttp

from agent import __version__
from agent.discovery import broadcast_discover, find_teacher
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
        pending_files: Callable[[], int] = lambda: 0,
    ):
        self.platform = platform
        self.state = state
        self.use_mdns = use_mdns
        self.backoff = backoff or Backoff()
        self.pending_files = pending_files
        self.connected = asyncio.Event()
        self.unpaired = False   # учитель отвязал ноутбук — работать дальше нет смысла
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._stop = False

    # ---------- Основной цикл ----------

    async def run(self) -> None:
        """Работает, пока не вызвали stop() или учитель не отвязал ноутбук."""
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
            for addr in found:
                if addr not in result:
                    result.append(addr)
            if result and result[0] != (self.state.host, self.state.port):
                log.info("Учитель найден на %s", result)
        last = (self.state.host, self.state.port)
        if last not in result:
            result.append(last)
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
                if (host, port) != (self.state.host, self.state.port):
                    self.state.host, self.state.port = host, port
                    self.state.save(self.platform)
                log.info("На связи с учителем %s:%s как ноутбук %s", host, port, self.state.laptop_number)
                self.backoff.reset()
                self.connected.set()

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
                self._check_close_code(ws)
                return True
            finally:
                self.connected.clear()
                self._ws = None
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
            "pending_files": self.pending_files(),
        }

    async def send_status(self) -> None:
        """Сообщить учителю свежий статус (используется очередью файлов на этапе 3)."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_str(Message(MsgType.STATUS, {"pending_files": self.pending_files()}).to_json())

    # ---------- Команды ----------

    async def _handle_command(self, ws: aiohttp.ClientWebSocketResponse, msg: Message) -> None:
        command_id = str(msg.payload.get("command_id", ""))
        try:
            # Повторная проверка на стороне агента: выполняем только разрешённое
            action, params = validate_command(str(msg.payload.get("action", "")), msg.payload.get("params") or {})
            text = await self._execute(action, params)
            result = {"command_id": command_id, "ok": True, "message": text}
        except ProtocolError as e:
            log.warning("Отклонена команда: %s", e)
            result = {"command_id": command_id, "ok": False, "message": str(e)}
        except Exception as e:
            log.exception("Ошибка выполнения команды")
            result = {"command_id": command_id, "ok": False, "message": f"ошибка: {e}"}
        if not ws.closed:
            await ws.send_str(Message(MsgType.RESULT, result).to_json())

    async def _execute(self, action: Action, params: dict) -> str:
        # Действия ОС выполняем в отдельном потоке, чтобы не блокировать связь
        if action is Action.OPEN_URLS:
            return await asyncio.to_thread(self.platform.open_urls, params["urls"], params["browser"])
        raise ProtocolError(f"Действие не поддерживается: {action}")

