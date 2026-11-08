"""
Поиск компьютера учителя в сети через mDNS.

Агент знает teacher_id, поэтому спрашивает конкретное имя сервиса.
Используем «unicast»-режим zeroconf: запрос уходит с обычного случайного порта,
и ответ приходит на него же. Так агенту не нужно слушать порт 5353, и
брандмауэр Windows не показывает детям окно «Разрешить доступ?».
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from shared.protocol import DEFAULT_PORT, DISCOVERY_REQUEST, SERVICE_TYPE, service_name

log = logging.getLogger(__name__)


async def find_teacher(teacher_id: str, timeout: float = 3.0, unicast: bool = True) -> list[tuple[str, int]]:
    """Вернуть список адресов (ip, порт) учителя; пустой — если не нашли за timeout секунд.
    Адресов может быть несколько (например, Wi-Fi и кабель)."""
    azc = None
    try:
        azc = AsyncZeroconf(ip_version=IPVersion.V4Only, unicast=unicast)
        info = AsyncServiceInfo(SERVICE_TYPE, service_name(teacher_id))
        found = await info.async_request(azc.zeroconf, int(timeout * 1000))
        if not found or not info.port:
            return []
        return [(socket.inet_ntoa(a), int(info.port)) for a in info.addresses if len(a) == 4]
    except Exception as e:
        log.debug("mDNS: ошибка поиска: %s", e)
        return []
    finally:
        if azc is not None:
            try:
                await azc.async_close()
            except Exception:
                pass


# ---------- Широковещательный поиск (UDP) ----------

def _broadcast_targets() -> list[str]:
    """
    Общий широковещательный адрес + адреса вида 192.168.3.255 для каждой своей сети
    (если на ноутбуке несколько адаптеров, 255.255.255.255 уходит только в один).
    """
    targets = ["255.255.255.255"]
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        ips = set()
    for ip in sorted(ips):
        if not ip.startswith(("127.", "169.254.")):
            targets.append(ip.rsplit(".", 1)[0] + ".255")
    return targets


class _Collector(asyncio.DatagramProtocol):
    def __init__(self):
        self.found: list[dict] = []

    def datagram_received(self, data: bytes, addr):
        try:
            info = json.loads(data)
            if info.get("service") == "classroom" and isinstance(info.get("port"), int):
                info["ip"] = addr[0]
                if not any(f["ip"] == info["ip"] and f["teacher_id"] == info.get("teacher_id") for f in self.found):
                    self.found.append(info)
        except (ValueError, AttributeError):
            pass


async def broadcast_discover(
    port: int = DEFAULT_PORT,
    timeout: float = 1.5,
    targets: list[str] | None = None,
) -> list[dict]:
    """
    Отправить в сеть «где учитель?» и собрать ответы за timeout секунд.
    Возвращает список {"ip", "port", "teacher_id", "v"}.
    targets — куда слать (по умолчанию широковещательный адрес; в тестах — 127.0.0.1).
    """
    loop = asyncio.get_running_loop()
    collector = _Collector()
    transport = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        transport, _ = await loop.create_datagram_endpoint(lambda: collector, sock=sock)
        for _ in range(2):  # дважды — на случай потери пакета в Wi-Fi
            for t in targets or _broadcast_targets():
                try:
                    transport.sendto(DISCOVERY_REQUEST, (t, port))
                except OSError as e:
                    log.debug("UDP-поиск: не удалось отправить на %s: %s", t, e)
            await asyncio.sleep(timeout / 2)
    except OSError as e:
        log.debug("UDP-поиск: ошибка: %s", e)
    finally:
        if transport:
            transport.close()
    return collector.found
