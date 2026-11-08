"""
«Конец урока».

Для каждого ноутбука:
  1. «Собрать всё» (COLLECT_NOW): агент находит оставшиеся файлы, ждёт, пока они
     станут готовыми, и отправляет. Отвечает, сколько ещё ждёт отправки.
  2. Если неотправленных нет — «Очистить» (CLEANUP) со списком SHA-256 всего, что
     учитель получил от этого ноутбука. Агент переносит в свою корзину (7 дней)
     ТОЛЬКО эти файлы и только если они не менялись после отправки.
  3. Если что-то не отправилось — очистка на этом ноутбуке НЕ выполняется.
     Ни один неотправленный проект не потеряется.

Ноутбуки обрабатываются параллельно; ход показывается в панели.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from shared.protocol import Action

if TYPE_CHECKING:
    from teacher.server import TeacherServer

log = logging.getLogger(__name__)

COLLECT_TIMEOUT = 180     # сколько ждать ответа на «Собрать всё», секунд
CLEANUP_TIMEOUT = 120
ATTEMPTS = 2


class EndLesson:
    def __init__(self, server: "TeacherServer"):
        self.server = server
        self.progress: dict[str, dict] = {}
        self.running = False
        self._task: asyncio.Task | None = None

    def routes(self) -> list:
        return [
            web.post("/ui/api/end-lesson", self.handle_start),
            web.get("/ui/api/end-lesson", self.handle_status),
        ]

    async def _set(self, number: str, stage: str, text: str) -> None:
        self.progress[number] = {"stage": stage, "text": text}
        await self.server.broadcast_event({"kind": "end_lesson", "running": self.running, "progress": self.progress})

    async def run(self, numbers: list[str]) -> dict[str, dict]:
        self.running = True
        self.progress = {}
        try:
            for n in numbers:
                await self._set(n, "wait", "ожидание…")
            await asyncio.gather(*(self._one(n) for n in numbers))
        finally:
            self.running = False
            await self.server.broadcast_event({"kind": "end_lesson", "running": False, "progress": self.progress})
            await self.server.broadcast_state()
        return self.progress

    async def _one(self, number: str) -> None:
        srv = self.server
        if number not in srv.agents:
            await self._set(number, "skip", "не на связи — файлы останутся на ноутбуке, очистка не выполнена")
            return
        result = None
        for attempt in range(1, ATTEMPTS + 1):
            await self._set(number, "collect", "собираю файлы…" if attempt == 1 else "собираю ещё раз…")
            result = await srv.command_and_wait(number, Action.COLLECT_NOW, {}, COLLECT_TIMEOUT)
            if result is None:
                await self._set(number, "error", "ноутбук не ответил — очистка не выполнена")
                return
            data = result.get("data") or {}
            if result.get("ok") and data.get("pending") == 0 and not data.get("not_ready"):
                break
            await asyncio.sleep(3)
        data = (result or {}).get("data") or {}
        if not result.get("ok") or data.get("pending") != 0 or data.get("not_ready"):
            parts = []
            if data.get("pending"):
                parts.append(f"не отправлено: {data['pending']}")
            if data.get("not_ready"):
                parts.append("ещё открыты/скачиваются: " + ", ".join(data["not_ready"][:5]))
            if not result.get("ok"):
                parts.append(result.get("message", "ошибка"))
            await self._set(number, "warning", "; ".join(parts) + " — очистка НЕ выполнена, нажмите «Конец урока» ещё раз")
            return

        await self._set(number, "cleanup", "все файлы получены, очищаю папки…")
        shas = srv.db.confirmed_shas(number)
        res = await srv.command_and_wait(number, Action.CLEANUP, {"confirmed_sha256": shas}, CLEANUP_TIMEOUT)
        if res is None or not res.get("ok"):
            await self._set(number, "error", "очистка не удалась: " + ((res or {}).get("message") or "нет ответа"))
            return
        d = res.get("data") or {}
        text = f"готово: получено файлов за всё время {len(shas)}, убрано в корзину {d.get('moved', 0)}"
        if d.get("kept"):
            text += f"; оставлено на месте {len(d['kept'])} (изменены после отправки)"
        await self._set(number, "done", text)
        log.info("Конец урока, ноутбук %s: %s", number, text)

    async def handle_start(self, request: web.Request) -> web.Response:
        if self.running:
            return web.json_response({"error": "«Конец урока» уже выполняется"}, status=409)
        body = await request.json()
        targets = body.get("targets", "all")
        numbers = sorted(self.server.storage.laptops()) if targets == "all" else [str(n) for n in targets]
        if not numbers:
            return web.json_response({"error": "Нет ноутбуков"}, status=400)
        self._task = asyncio.create_task(self.run(numbers))
        return web.json_response({"started": numbers})

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({"running": self.running, "progress": self.progress})
