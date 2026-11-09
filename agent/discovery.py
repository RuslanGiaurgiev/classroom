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

def _local_ips() -> list[str]:
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        ips = set()
    # Адрес, с которого реально уходит трафик (getaddrinfo на Windows иногда его не показывает)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def _broadcast_targets() -> list[str]:
    """
    Общий широковещательный адрес + широковещательные адреса своих сетей. Размер сети (маску)
    узнавать долго, поэтому шлём на адреса для масок от /24 до /30: дома и в школе сеть обычно /24
    (192.168.3.255), а у точки доступа iPhone — /28 (172.20.10.15). Лишние пакеты безвредны.
    """
    import ipaddress
    targets = ["255.255.255.255"]
    for ip in _local_ips():
        for prefix in range(24, 31):
            b = str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False).broadcast_address)
            if b not in targets and b != ip:
                targets.append(b)
    return targets


def sweep_targets(max_hosts: int = 254) -> list[str]:
    """Все адреса своих небольших частных сетей (/24) — для прямого опроса, если широковещание не проходит."""
    import ipaddress
    out: list[str] = []
    for ip in _local_ips():
        addr = ipaddress.ip_address(ip)
        if not addr.is_private:
            continue
        for host in ipaddress.ip_network(f"{ip}/24", strict=False).hosts():
            h = str(host)
            if h != ip and h not in out:
                out.append(h)
            if len(out) >= max_hosts * 2:
                return out
    return out


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


def _discover_sync(port: int, timeout: float, targets: list[str]) -> list[dict]:
    """
    Обычный сокет в отдельном потоке, а не asyncio: на Windows ответ «порт закрыт» от соседнего
    устройства (ICMP) приходит в сокет ошибкой WinError 10054, и asyncio-сокет после неё перестаёт
    принимать ответы. Здесь такие ошибки просто пропускаем.
    """
    import time
    collector = _Collector()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as e:
        log.debug("UDP-поиск: ошибка: %s", e)
        return []
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(0.1)
        for _ in range(2):  # дважды — на случай потери пакета в Wi-Fi
            for t in targets:
                try:
                    sock.sendto(DISCOVERY_REQUEST, (t, port))
                except OSError as e:
                    log.debug("UDP-поиск: не удалось отправить на %s: %s", t, e)
            end = time.monotonic() + timeout / 2
            while time.monotonic() < end:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    continue  # WinError 10054 и т.п. — ответ «порт закрыт» от другого устройства
                collector.datagram_received(data, addr)
    except OSError as e:
        log.debug("UDP-поиск: ошибка: %s", e)
    finally:
        sock.close()
    return collector.found


async def broadcast_discover(
    port: int = DEFAULT_PORT,
    timeout: float = 1.5,
    targets: list[str] | None = None,
) -> list[dict]:
    """
    Отправить в сеть «где учитель?» и собрать ответы за timeout секунд.
    Возвращает список {"ip", "port", "teacher_id", "v"}.
    targets — куда слать (по умолчанию широковещательные адреса; для прямого опроса — все адреса сети).
    """
    return await asyncio.to_thread(_discover_sync, port, timeout, targets or _broadcast_targets())
