"""
Ответчик на широковещательный поиск учителя.

Агент отправляет в сеть UDP-пакет «CLASSROOM_DISCOVER_V1» на порт учителя,
учитель отвечает своим teacher_id и портом. Так новый ноутбук находит Mac
без ввода адреса. Работает в обычной домашней/школьной сети, если роутер
не изолирует устройства друг от друга.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

from shared.protocol import DISCOVERY_REQUEST, PROTOCOL_VERSION

log = logging.getLogger(__name__)


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, teacher_id: str, port_fn):
        self.teacher_id = teacher_id
        self.port_fn = port_fn
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if data.strip() != DISCOVERY_REQUEST:
            return
        reply = json.dumps({
            "service": "classroom",
            "teacher_id": self.teacher_id,
            "port": self.port_fn(),
            "v": PROTOCOL_VERSION,
        }).encode()
        if self.transport:
            self.transport.sendto(reply, addr)


async def start_responder(teacher_id: str, port: int, http_port_fn=None) -> asyncio.DatagramTransport | None:
    """Слушаем UDP на port. Возвращает транспорт (для закрытия) или None, если не вышло."""
    loop = asyncio.get_running_loop()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Responder(teacher_id, http_port_fn or (lambda: port)), sock=sock
        )
        log.info("UDP-поиск: слушаю порт %s", sock.getsockname()[1])
        return transport
    except OSError:
        log.exception("Не удалось запустить UDP-ответчик")
        return None
