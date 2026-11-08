"""
Первое подключение ноутбука к учителю — без ввода кодов.

Ход событий:
  1. Ищем учителя в сети (широковещательный UDP-запрос).
     Если не нашли — продолжаем искать; в окне есть запасное поле для адреса Mac.
  2. Отправляем учителю запрос на подключение → получаем «число для сверки».
  3. Ждём, пока учитель нажмёт «Принять» в панели → получаем постоянный токен.

Логика здесь не зависит от окон: окно (tkinter на Windows) лишь показывает
статусы через callback и может передать введённый вручную адрес.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Callable

import aiohttp

from agent.discovery import broadcast_discover
from agent.platform.base import Platform
from agent.state import AgentState
from shared.protocol import DEFAULT_PORT, PROTOCOL_VERSION

log = logging.getLogger(__name__)


@dataclass
class PairingStatus:
    """Что показать на экране ноутбука."""
    stage: str          # searching | waiting | rejected | error | done
    text: str
    match_code: str = ""


class PairingCancelled(Exception):
    pass


def parse_manual_address(text: str) -> tuple[str, int] | None:
    """
    Прощаем всё, что может ввести человек: «192.168.3.96», «192,168,3,96»,
    «http://192.168.3.96:8765/», пробелы. Возвращает (ip, порт) или None.
    """
    t = (text or "").strip().lower()
    t = re.sub(r"^https?://", "", t).rstrip("/").replace(",", ".").replace(" ", "")
    t = t.replace("ю", ".").replace("б", ".")  # русская раскладка: «.» на месте «ю»/«б»
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?", t)
    if not m:
        return None
    ip = m.group(1)
    if any(int(p) > 255 for p in ip.split(".")):
        return None
    port = int(m.group(2)) if m.group(2) else DEFAULT_PORT
    return (ip, port) if 0 < port < 65536 else None


class PairingFlow:
    def __init__(
        self,
        platform: Platform,
        on_status: Callable[[PairingStatus], None] = lambda s: None,
        discover_port: int = DEFAULT_PORT,
        discover_targets: list[str] | None = None,
        poll_interval: float = 1.5,
    ):
        self.platform = platform
        self.on_status = on_status
        self.discover_port = discover_port
        self.discover_targets = discover_targets
        self.poll_interval = poll_interval
        self.manual_address: tuple[str, int] | None = None  # может задать окно
        self.cancelled = False
        self._secret = secrets.token_urlsafe(24)

    def _status(self, stage: str, text: str, match_code: str = "") -> None:
        log.info("Подключение: %s — %s %s", stage, text, match_code)
        self.on_status(PairingStatus(stage, text, match_code))

    async def _find_teacher(self) -> tuple[str, int]:
        """Ищем, пока не найдём (или пока не введут адрес вручную)."""
        self._status("searching", "Ищу компьютер учителя в сети…")
        attempts = 0
        while True:
            if self.cancelled:
                raise PairingCancelled
            if self.manual_address:
                return self.manual_address
            found = await broadcast_discover(self.discover_port, targets=self.discover_targets)
            if found:
                return found[0]["ip"], int(found[0]["port"])
            attempts += 1
            if attempts == 3:
                self._status("searching", "Ищу компьютер учителя… Проверьте, что приложение учителя "
                                          "открыто и ноутбук подключён к той же сети Wi-Fi.")
            await asyncio.sleep(1)

    async def run(self) -> AgentState:
        while True:
            host, port = await self._find_teacher()
            try:
                return await self._request_and_wait(host, port)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                log.info("Связь с %s:%s потеряна: %s", host, port, e)
                if self.manual_address == (host, port):
                    self.manual_address = None
                    self._status("error", f"Не удалось связаться с {host}. Проверьте адрес.")
                    await asyncio.sleep(3)

    async def _request_and_wait(self, host: str, port: int) -> AgentState:
        base = f"http://{host}:{port}"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            body = {"secret": self._secret, "hostname": self.platform.hostname(), "username": self.platform.username()}
            async with s.post(f"{base}/api/pair-request", json=body) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise aiohttp.ClientError(data.get("error") or f"ошибка {r.status}")
            if data.get("protocol_version") != PROTOCOL_VERSION:
                self._status("error", "Версия агента не подходит к приложению учителя — нужен новый agent.exe")
                raise PairingCancelled
            request_id, match_code = data["request_id"], data["match_code"]
            self._status("waiting", "Учитель должен нажать «Принять» на своём компьютере.", match_code)

            while True:
                if self.cancelled:
                    raise PairingCancelled
                await asyncio.sleep(self.poll_interval)
                async with s.get(f"{base}/api/pair-request/{request_id}",
                                 headers={"X-Pair-Secret": self._secret}) as r:
                    data = await r.json(content_type=None)
                status = data.get("status")
                if status == "approved":
                    state = AgentState(
                        teacher_id=data["teacher_id"],
                        laptop_number=str(data["laptop_number"]),
                        host=host,
                        port=port,
                        token=data["token"],
                    )
                    state.save(self.platform)
                    self._status("done", f"Готово! Это ноутбук № {state.laptop_number}.")
                    return state
                if status == "rejected":
                    self._status("rejected", "Учитель отклонил подключение.")
                    raise PairingCancelled
                if status == "unknown":
                    # Запрос устарел (или учитель перезапустил приложение) — отправим новый
                    log.info("Запрос на подключение устарел, отправляю новый")
                    return await self._request_and_wait(host, port)
