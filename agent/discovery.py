"""
Поиск компьютера учителя в сети через mDNS.

Агент знает teacher_id, поэтому спрашивает конкретное имя сервиса.
Используем «unicast»-режим zeroconf: запрос уходит с обычного случайного порта,
и ответ приходит на него же. Так агенту не нужно слушать порт 5353, и
брандмауэр Windows не показывает детям окно «Разрешить доступ?».
"""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from shared.protocol import SERVICE_TYPE, service_name

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
