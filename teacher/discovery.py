"""
Объявление учителя в локальной сети через mDNS (zeroconf).

Агенты знают teacher_id (получили при привязке) и спрашивают в сети
«где Classroom-<teacher_id>?». Так они находят Mac даже после смены IP.
"""

from __future__ import annotations

import asyncio
import logging
import socket

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from shared.protocol import PROTOCOL_VERSION, SERVICE_TYPE, service_name
from teacher.netutil import all_ipv4

log = logging.getLogger(__name__)


class Advertiser:
    def __init__(self, teacher_id: str, port: int, recheck_seconds: float = 30):
        self.teacher_id = teacher_id
        self.port = port
        self.recheck_seconds = recheck_seconds
        self._zc: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None
        self._ips: list[str] = []
        self._task: asyncio.Task | None = None

    def _make_info(self, ips: list[str]) -> AsyncServiceInfo:
        return AsyncServiceInfo(
            SERVICE_TYPE,
            service_name(self.teacher_id),
            addresses=[socket.inet_aton(ip) for ip in ips],
            port=self.port,
            properties={"id": self.teacher_id, "v": str(PROTOCOL_VERSION)},
            server=f"classroom-{self.teacher_id}.local.",
        )

    async def start(self) -> None:
        try:
            self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            self._ips = all_ipv4()
            self._info = self._make_info(self._ips)
            await self._zc.async_register_service(self._info, allow_name_change=False)
            log.info("mDNS: объявлен %s на %s:%s", service_name(self.teacher_id), self._ips, self.port)
            self._task = asyncio.create_task(self._watch_ip())
        except Exception:  # mDNS — не критично, есть запасной адрес из кода
            log.exception("Не удалось запустить mDNS-объявление")

    async def _watch_ip(self) -> None:
        """Если IP Mac сменился (переподключение к Wi-Fi) — обновляем объявление."""
        while True:
            await asyncio.sleep(self.recheck_seconds)
            try:
                ips = all_ipv4()
                if ips != self._ips and self._zc and self._info:
                    log.info("mDNS: IP изменился %s → %s", self._ips, ips)
                    self._ips = ips
                    self._info = self._make_info(ips)
                    await self._zc.async_update_service(self._info)
            except Exception:
                log.exception("mDNS: ошибка обновления")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._zc:
            try:
                if self._info:
                    await self._zc.async_unregister_service(self._info)
                await self._zc.async_close()
            except Exception:
                log.exception("mDNS: ошибка остановки")
