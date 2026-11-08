"""
HTTP + WebSocket сервер учителя (aiohttp).

Маршруты для агентов (доступны из сети):
    POST /api/pair-request       — ноутбук просит подключить его
    GET  /api/pair-request/{id}  — ноутбук узнаёт, принял ли учитель (и забирает токен)
    GET  /ws/agent        — WebSocket агента (заголовок Authorization: Bearer <токен>)

Маршруты панели (только с самого Mac + токен панели):
    GET  /ui/                    — страница панели
    GET  /ui/api/state           — список ноутбуков
    POST /ui/api/pair-requests/{id}/approve — принять новый ноутбук (с номером)
    POST /ui/api/pair-requests/{id}/reject  — отклонить
    DELETE /ui/api/laptops/{n}   — отвязать ноутбук
    POST /ui/api/commands        — отправить команду
    GET  /ui/ws                  — живые обновления для панели
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import WSMsgType, web

from shared.protocol import (
    PROTOCOL_VERSION,
    Action,
    Message,
    MsgType,
    ProtocolError,
    validate_command,
    validate_process_name,
)
from shared.search import SEARCH_ENGINES, to_url
from teacher.netutil import is_loopback, primary_ipv4
from teacher.pairing import PairRequestManager
from teacher.udp_discovery import start_responder
from teacher.storage import Storage

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
HELLO_TIMEOUT = 10
LAPTOP_NUMBER_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё_-]{1,10}$")


@dataclass
class AgentConn:
    """Подключённый ноутбук."""
    number: str
    ws: web.WebSocketResponse
    ip: str
    username: str = ""
    hostname: str = ""
    agent_version: str = ""
    browsers: list[str] = field(default_factory=list)
    pending_files: int = 0
    screen: str | None = None      # что сейчас на экране: lock / message / countdown
    shutdown: str | None = None    # запланировано: shutdown / reboot
    connected_at: float = field(default_factory=time.time)


class TeacherServer:
    def __init__(
        self,
        storage: Storage,
        pairing: PairRequestManager | None = None,
        host_ip_fn: Callable[[], str] = primary_ipv4,
    ):
        self.storage = storage
        self.pairing = pairing or PairRequestManager()
        self.host_ip_fn = host_ip_fn
        self.ui_token = storage.get_ui_token()
        self.agents: dict[str, AgentConn] = {}
        self.ui_clients: set[web.WebSocketResponse] = set()
        self.port: int = storage.port  # реальный порт уточняется после запуска
        self._runner: web.AppRunner | None = None
        self._udp = None
        # Ноутбуки, на которых учитель включил «Внимание на учителя» (номер → текст).
        # Если ноутбук переподключился (перезагрузили, перелогинились) — включаем снова.
        self.locked: dict[str, str] = {}
        # Обработчики для следующих этапов (сбор файлов, конец урока)
        self.on_agent_connected: list = []
        self.on_agent_report: list = []
        self.on_settings_changed: list = []

    # ================= Запуск / остановка =================

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._ui_guard], client_max_size=1024 * 1024)
        app.add_routes([
            web.post("/api/pair-request", self.handle_pair_request),
            web.get("/api/pair-request/{id}", self.handle_pair_poll),
            web.get("/ws/agent", self.handle_agent_ws),
            web.get("/", self.handle_root),
            web.get("/ui/", self.handle_ui_index),
            web.get("/ui/api/state", self.handle_ui_state),
            web.post("/ui/api/pair-requests/{id}/approve", self.handle_ui_approve),
            web.post("/ui/api/pair-requests/{id}/reject", self.handle_ui_reject),
            web.delete("/ui/api/laptops/{number}", self.handle_ui_unpair),
            web.post("/ui/api/commands", self.handle_ui_command),
            web.put("/ui/api/settings", self.handle_ui_settings),
            web.get("/ui/ws", self.handle_ui_ws),
        ])
        app.router.add_static("/ui/static/", WEB_DIR)
        return app

    async def start(self, host: str = "0.0.0.0", port: int | None = None, udp_discovery: bool = True) -> int:
        """Запустить сервер. port=0 — случайный свободный (для тестов). Возвращает порт."""
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, self.storage.port if port is None else port)
        await site.start()
        self.port = self._runner.addresses[0][1]
        log.info("Сервер запущен на %s:%s", host, self.port)
        if udp_discovery:
            # Отвечаем на широковещательный поиск на том же номере порта (UDP)
            self._udp = await start_responder(self.storage.teacher_id, self.port)
        return self.port

    async def stop(self) -> None:
        for conn in list(self.agents.values()):
            await conn.ws.close()
        for ws in list(self.ui_clients):
            await ws.close()
        if self._udp:
            self._udp.close()
        if self._runner:
            await self._runner.cleanup()

    # ================= Защита панели =================

    @web.middleware
    async def _ui_guard(self, request: web.Request, handler):
        """Панель доступна только с самого Mac. API панели — ещё и только с токеном."""
        if request.path.startswith("/ui"):
            if not is_loopback(request.remote):
                raise web.HTTPForbidden(text="Панель доступна только на компьютере учителя")
            if request.path.startswith(("/ui/api", "/ui/ws")):
                token = request.headers.get("X-UI-Token") or request.query.get("t", "")
                if not secrets.compare_digest(token, self.ui_token):
                    raise web.HTTPUnauthorized(text="Нет доступа")
        return await handler(request)

    # ================= Агенты =================

    async def handle_pair_request(self, request: web.Request) -> web.Response:
        """Ноутбук просит подключить его. Токен он получит только после «Принять» в панели."""
        try:
            body = await request.json()
            secret = str(body["secret"])
            if len(secret) < 16:
                raise ValueError
        except Exception:
            return web.json_response({"error": "Некорректный запрос"}, status=400)
        try:
            req = self.pairing.create(
                hostname=str(body.get("hostname", "")),
                username=str(body.get("username", "")),
                secret=secret,
                ip=request.remote or "",
            )
        except OverflowError as e:
            return web.json_response({"error": str(e)}, status=429)
        log.info("Запрос на подключение: %s (%s), число %s", req.hostname, req.ip, req.match_code)
        await self.broadcast_state()
        return web.json_response({"request_id": req.id, "match_code": req.match_code,
                                  "protocol_version": PROTOCOL_VERSION})

    async def handle_pair_poll(self, request: web.Request) -> web.Response:
        req = self.pairing.poll(request.match_info["id"], request.headers.get("X-Pair-Secret", ""))
        if req is None:
            return web.json_response({"status": "unknown"}, status=404)
        if req.status == "approved":
            return web.json_response({
                "status": "approved",
                "token": req.token,
                "laptop_number": req.laptop_number,
                "teacher_id": self.storage.teacher_id,
                "protocol_version": PROTOCOL_VERSION,
            })
        return web.json_response({"status": req.status})

    def _auth_agent(self, request: web.Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return self.storage.find_laptop_by_token(auth[len("Bearer "):].strip())

    async def handle_agent_ws(self, request: web.Request) -> web.StreamResponse:
        number = self._auth_agent(request)
        if number is None:
            log.warning("Отклонено подключение без валидного токена с %s", request.remote)
            raise web.HTTPUnauthorized(text="Неверный токен")

        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
        await ws.prepare(request)

        # Первое сообщение обязано быть hello
        try:
            raw = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT)
            if raw.type != WSMsgType.TEXT:
                await ws.close()
                return ws
            hello = Message.from_json(raw.data)
            if hello.type != MsgType.HELLO:
                raise ProtocolError("Ожидалось сообщение hello")
        except ProtocolError as e:
            log.warning("Ноутбук %s: ошибка протокола: %s", number, e)
            await ws.send_str(Message(MsgType.ERROR, {"message": str(e)}).to_json())
            await ws.close(code=4002, message=b"protocol")
            return ws
        except asyncio.TimeoutError:
            await ws.close()
            return ws

        p = hello.payload
        conn = AgentConn(
            number=number,
            ws=ws,
            ip=request.remote or "",
            username=str(p.get("username", ""))[:100],
            hostname=str(p.get("hostname", ""))[:100],
            agent_version=str(p.get("agent_version", ""))[:20],
            browsers=[str(b) for b in p.get("browsers", [])][:10],
        )
        self._apply_status(conn, p)
        # Второе подключение того же ноутбука вытесняет старое
        old = self.agents.get(number)
        if old:
            await old.ws.close(code=4001, message=b"replaced")
        self.agents[number] = conn
        self.storage.update_laptop_info(number, username=conn.username, hostname=conn.hostname, last_seen=time.time())
        await ws.send_str(Message(MsgType.WELCOME, {"laptop_number": number, "server_time": time.time()}).to_json())
        log.info("Ноутбук %s на связи (%s, пользователь %s)", number, conn.ip, conn.username)
        if number in self.locked and conn.screen != "lock":
            await self._send_to(conn, Action.LOCK_SCREEN, {"text": self.locked[number]})
        for handler in self.on_agent_connected:
            await handler(conn)
        await self.broadcast_state()

        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = Message.from_json(raw.data)
                except ProtocolError as e:
                    log.warning("Ноутбук %s: плохое сообщение: %s", number, e)
                    continue
                await self._on_agent_message(conn, msg)
        finally:
            if self.agents.get(number) is conn:
                del self.agents[number]
                self.storage.update_laptop_info(number, last_seen=time.time())
                log.info("Ноутбук %s отключился", number)
                await self.broadcast_state()
        return ws

    @staticmethod
    def _apply_status(conn: AgentConn, p: dict) -> None:
        if "pending_files" in p:
            try:
                conn.pending_files = max(0, int(p.get("pending_files") or 0))
            except (TypeError, ValueError):
                pass
        if "screen" in p:
            conn.screen = p["screen"] if p["screen"] in ("lock", "message", "countdown") else None
        if "shutdown" in p:
            conn.shutdown = p["shutdown"] if p["shutdown"] in ("shutdown", "reboot") else None
        if "username" in p:
            conn.username = str(p["username"])[:100]

    async def _send_to(self, conn: AgentConn, action: Action, params: dict) -> None:
        msg = Message(MsgType.COMMAND, {"command_id": uuid.uuid4().hex, "action": action.value, "params": params})
        try:
            await conn.ws.send_str(msg.to_json())
        except ConnectionError:
            pass

    async def _on_agent_message(self, conn: AgentConn, msg: Message) -> None:
        if msg.type == MsgType.STATUS:
            self._apply_status(conn, msg.payload)
            await self.broadcast_state()
        elif msg.type == MsgType.REPORT:
            for handler in self.on_agent_report:
                await handler(conn, msg.payload)
        elif msg.type == MsgType.RESULT:
            p = msg.payload
            await self.broadcast_event({
                "kind": "result",
                "number": conn.number,
                "command_id": str(p.get("command_id", "")),
                "ok": bool(p.get("ok")),
                "message": str(p.get("message", ""))[:500],
            })

    async def send_command(self, targets: list[str] | str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Отправить команду ноутбукам. targets — список номеров или "all".
        Команды не копятся для офлайн-ноутбуков: открывать сайт через час не нужно.
        """
        if action == Action.OPEN_URLS.value and isinstance(params.get("urls"), list):
            # Строки учителя: адреса открываем как есть, остальное — поиск
            engine = params.get("engine") or self.storage.settings().get("search_engine", "yandex")
            params = {**params, "urls": [to_url(str(u), engine) for u in params["urls"] if str(u).strip()]}
            params.pop("engine", None)
        act, params = validate_command(action, params)
        if targets == "all":
            numbers = sorted(self.storage.laptops().keys(), key=_sort_key)
        else:
            numbers = [str(n) for n in targets]
        command_id = uuid.uuid4().hex
        msg = Message(MsgType.COMMAND, {"command_id": command_id, "action": act.value, "params": params})
        statuses: dict[str, str] = {}
        for n in numbers:
            conn = self.agents.get(n)
            if conn is None:
                statuses[n] = "offline"
                continue
            try:
                await conn.ws.send_str(msg.to_json())
                statuses[n] = "sent"
            except ConnectionError:
                statuses[n] = "offline"
        # Запоминаем, где включена заставка, чтобы вернуть её после переподключения
        for n in numbers:
            if act is Action.LOCK_SCREEN:
                self.locked[n] = params["text"]
            elif act is Action.UNLOCK_SCREEN:
                self.locked.pop(n, None)
        log.info("Команда %s %s → %s", act.value, params, statuses)
        return {"command_id": command_id, "statuses": statuses}

    # ================= Панель =================

    async def handle_root(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/ui/")

    async def handle_ui_index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    def state_snapshot(self) -> dict[str, Any]:
        laptops = []
        for number, info in sorted(self.storage.laptops().items(), key=lambda kv: _sort_key(kv[0])):
            conn = self.agents.get(number)
            laptops.append({
                "number": number,
                "online": conn is not None,
                "username": conn.username if conn else info.get("username", ""),
                "hostname": conn.hostname if conn else info.get("hostname", ""),
                "ip": conn.ip if conn else "",
                "agent_version": conn.agent_version if conn else "",
                "browsers": conn.browsers if conn else [],
                "pending_files": conn.pending_files if conn else None,
                "screen": conn.screen if conn else None,
                "locked": number in self.locked,
                "shutdown": conn.shutdown if conn else None,
                "last_seen": info.get("last_seen"),
            })
        return {
            "laptops": laptops,
            "pair_requests": [r.public() for r in self.pairing.pending()],
            "teacher": {"ip": self.host_ip_fn(), "port": self.port, "teacher_id": self.storage.teacher_id},
            "settings": self.public_settings(),
        }

    def public_settings(self) -> dict[str, Any]:
        st = self.storage.settings()
        return {k: st.get(k) for k in ("search_engine", "apps", "lesson_sets")} | {
            "search_engines": {k: v[0] for k, v in SEARCH_ENGINES.items()},
        }

    async def handle_ui_settings(self, request: web.Request) -> web.Response:
        """Сохранить настройки (только известные поля, с проверкой)."""
        body = await request.json()
        updates: dict[str, Any] = {}
        try:
            if "search_engine" in body:
                if body["search_engine"] not in SEARCH_ENGINES:
                    raise ValueError("Неизвестная поисковая система")
                updates["search_engine"] = body["search_engine"]
            if "apps" in body:
                apps = []
                for a in body["apps"][:30]:
                    title = str(a.get("title", "")).strip()[:60]
                    process = validate_process_name(str(a.get("process", "")))
                    apps.append({"title": title or process, "process": process})
                updates["apps"] = apps
            if "lesson_sets" in body:
                sets = []
                for ls in body["lesson_sets"][:100]:
                    name = str(ls.get("name", "")).strip()[:60]
                    items = [str(i).strip()[:500] for i in ls.get("items", []) if str(i).strip()][:20]
                    if not name or not items:
                        raise ValueError("У набора должно быть название и хотя бы один адрес")
                    browser = ls.get("browser", "default")
                    if browser not in ("default", "chrome", "yandex", "edge"):
                        raise ValueError("Неизвестный браузер")
                    sets.append({"id": str(ls.get("id") or uuid.uuid4().hex)[:40], "name": name,
                                 "items": items, "browser": browser})
                updates["lesson_sets"] = sets
            updates |= await self.validate_extra_settings(body)
        except (ValueError, ProtocolError, AttributeError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        self.storage.update_settings(**updates)
        await self.broadcast_state()
        for handler in self.on_settings_changed:
            await handler(updates)
        return web.json_response({"ok": True})

    async def validate_extra_settings(self, body: dict) -> dict:
        """Переопределяется/дополняется на следующих этапах."""
        return {}

    async def handle_ui_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.state_snapshot())

    async def handle_ui_approve(self, request: web.Request) -> web.Response:
        body = await request.json()
        number = str(body.get("number", "")).strip()
        if not LAPTOP_NUMBER_RE.match(number):
            return web.json_response({"error": "Номер: до 10 символов — цифры или буквы"}, status=400)
        token = secrets.token_urlsafe(32)
        req = self.pairing.approve(request.match_info["id"], number, token)
        if req is None:
            return web.json_response({"error": "Запрос уже не действует — перезапустите агент на ноутбуке"}, status=404)
        self.storage.add_laptop(number, token)
        self.storage.update_laptop_info(number, hostname=req.hostname, username=req.username)
        # Если под этим номером был другой ноутбук — его старый токен больше не действует
        old = self.agents.get(number)
        if old:
            await old.ws.close(code=4001, message=b"replaced")
        log.info("Ноутбук %s принят (%s, %s)", number, req.hostname, req.ip)
        await self.broadcast_state()
        return web.json_response({"ok": True, "number": number})

    async def handle_ui_reject(self, request: web.Request) -> web.Response:
        ok = self.pairing.reject(request.match_info["id"])
        await self.broadcast_state()
        return web.json_response({"ok": ok})

    async def handle_ui_unpair(self, request: web.Request) -> web.Response:
        number = request.match_info["number"]
        conn = self.agents.get(number)
        removed = self.storage.remove_laptop(number)
        if conn:
            await conn.ws.close(code=4003, message=b"unpaired")
        await self.broadcast_state()
        return web.json_response({"removed": removed})

    async def handle_ui_command(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            result = await self.send_command(body.get("targets", "all"), str(body.get("action", "")), body.get("params") or {})
        except ProtocolError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(result)

    async def handle_ui_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.ui_clients.add(ws)
        try:
            await ws.send_str(json.dumps({"kind": "state", **self.state_snapshot()}, ensure_ascii=False))
            async for _ in ws:
                pass
        finally:
            self.ui_clients.discard(ws)
        return ws

    async def broadcast_event(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False)
        for ws in list(self.ui_clients):
            try:
                await ws.send_str(data)
            except ConnectionError:
                self.ui_clients.discard(ws)

    async def broadcast_state(self) -> None:
        await self.broadcast_event({"kind": "state", **self.state_snapshot()})


def _sort_key(number: str):
    """Сортировка номеров «по-человечески»: 2 раньше 10."""
    return (0, int(number), "") if number.isdigit() else (1, 0, number)
