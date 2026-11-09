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
from shared.ignore import clean_patterns
from shared.search import SEARCH_ENGINES, to_url
from teacher.db import DB
from teacher.endlesson import EndLesson
from teacher.updates import UpdateManager
from teacher.backup import Backups
from teacher.importer import Importer
from teacher.inbox import FileReceiver
from teacher.materials import MaterialsManager
from teacher.sorter import Sorter
from teacher.yadisk import YandexDisk
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
    update_error: str = ""         # последняя ошибка самообновления
    connected_at: float = field(default_factory=time.time)


class TeacherServer:
    def __init__(
        self,
        storage: Storage,
        pairing: PairRequestManager | None = None,
        host_ip_fn: Callable[[], str] = primary_ipv4,
        disk_token_store=None,
        disk_api_base: str | None = None,
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
        self._waiting: dict[tuple[str, str], asyncio.Future] = {}   # (command_id, ноутбук) → ожидание результата
        self.db = DB(storage.data_dir / "teacher.sqlite3")
        self.files = FileReceiver(self)
        self.sorter = Sorter(self)
        self.importer = Importer(self)
        self.backups = Backups(self)
        self.end_lesson = EndLesson(self)
        self.updates = UpdateManager(self)
        self.materials = MaterialsManager(self)
        self.disk = YandexDisk(self, token_store=disk_token_store, **({"api_base": disk_api_base} if disk_api_base else {}))
        from teacher.disk_send import DiskSender
        self.disk_send = DiskSender(self)
        self.on_agent_connected.append(self._send_config)
        self.on_settings_changed.append(self._config_changed)

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
            web.post("/ui/api/laptops/{number}/rename", self.handle_ui_rename),
            web.get("/ui/api/laptops/{number}/open", self.handle_ui_list_open),
            web.post("/ui/api/laptops/{number}/close", self.handle_ui_close_open),
            web.post("/ui/api/commands", self.handle_ui_command),
            web.put("/ui/api/settings", self.handle_ui_settings),
            web.post("/ui/api/open-url", self.handle_ui_open_url),
            web.get("/ui/ws", self.handle_ui_ws),
        ])
        app.add_routes(self.files.routes())
        app.add_routes(self.sorter.routes())
        app.add_routes(self.importer.routes())
        app.add_routes(self.backups.routes())
        app.add_routes(self.disk.routes())
        app.add_routes(self.disk_send.routes())
        app.add_routes(self.end_lesson.routes())
        app.add_routes(self.updates.routes())
        app.add_routes(self.materials.routes())
        app.router.add_static("/ui/static/", WEB_DIR)
        return app

    async def start(self, host: str = "0.0.0.0", port: int | None = None, udp_discovery: bool = True,
                    check_updates: bool = True) -> int:
        """Запустить сервер. port=0 — случайный свободный (для тестов). Возвращает порт."""
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, self.storage.port if port is None else port)
        await site.start()
        self.port = self._runner.addresses[0][1]
        log.info("Сервер запущен на %s:%s", host, self.port)
        self.sorter.purge_ignored_unsorted()
        self.disk.start()
        if check_updates:          # в тестах выключено — как и проверка обновлений
            self.backups.start()
            self.updates.start()
        if udp_discovery:
            # Отвечаем на широковещательный поиск на том же номере порта (UDP)
            self._udp = await start_responder(self.storage.teacher_id, self.port)
        return self.port

    async def stop(self) -> None:
        for conn in list(self.agents.values()):
            await conn.ws.close()
        for ws in list(self.ui_clients):
            await ws.close()
        await self.disk.stop()
        await self.updates.stop()
        await self.backups.stop()
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
            resp = await handler(request)
            # Панель всегда берёт свежие файлы (после обновления не нужен «сброс кэша»)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
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
            # conn.number, а не number: номер могли сменить, пока ноутбук был на связи
            if self.agents.get(conn.number) is conn:
                del self.agents[conn.number]
                self.storage.update_laptop_info(conn.number, last_seen=time.time())
                log.info("Ноутбук %s отключился", conn.number)
                await self.broadcast_state()
        return ws

    def _apply_status(self, conn: AgentConn, p: dict) -> None:
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
        if "update_error" in p:
            err = str(p["update_error"] or "")[:300]
            if err and err != conn.update_error:
                # Агент сообщил о неудачном обновлении (и сам откатился) — учитываем попытку
                self.updates.on_agent_failed(conn.number, err)
            conn.update_error = err

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
            fut = self._waiting.pop((str(p.get("command_id", "")), conn.number), None)
            if fut is not None and not fut.done():
                fut.set_result(p)
                return  # результат ждёт сценарий (например, «Конец урока») — он сам напишет в журнал
            await self.broadcast_event({
                "kind": "result",
                "number": conn.number,
                "command_id": str(p.get("command_id", "")),
                "ok": bool(p.get("ok")),
                "message": str(p.get("message", ""))[:500],
            })

    async def command_and_wait(self, number: str, action: Action, params: dict, timeout: float) -> dict | None:
        """
        Отправить команду одному ноутбуку и дождаться результата.
        None — ноутбук не на связи или не ответил за timeout секунд.
        """
        conn = self.agents.get(number)
        if conn is None:
            return None
        act, params = validate_command(action.value, params)
        command_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._waiting[(command_id, number)] = fut
        try:
            await conn.ws.send_str(Message(MsgType.COMMAND, {"command_id": command_id, "action": act.value,
                                                             "params": params}).to_json())
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionError):
            return None
        finally:
            self._waiting.pop((command_id, number), None)

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
                "update_status": self.updates.status.get(number, "") or (conn.update_error if conn else ""),
                "outdated": bool(conn and self.updates.needs_update(conn)),
                "last_seen": info.get("last_seen"),
            })
        return {
            "laptops": laptops,
            "pair_requests": self._pair_requests_public(),
            "teacher": {"ip": self.host_ip_fn(), "port": self.port, "teacher_id": self.storage.teacher_id},
            "settings": self.public_settings(),
            "updates": self.updates.public(),
            "materials": self.materials.public(),
        }

    def _pair_requests_public(self) -> list[dict]:
        """Стучащиеся ноутбуки + номер, который у них был раньше (если свободен)."""
        former = self.storage.settings().get("former_laptops") or {}
        taken = set(self.storage.laptops())
        out = []
        for r in self.pairing.pending():
            d = r.public()
            n = former.get(r.hostname)
            d["former_number"] = n if n and n not in taken else ""
            out.append(d)
        return out

    def public_settings(self) -> dict[str, Any]:
        st = self.storage.settings()
        keys = ("search_engine", "apps", "lesson_sets", "watch_folders", "max_file_mb", "active_groups",
                "name_threshold", "disk_root", "disk_client_id", "ignore_patterns", "materials_dest", "lock_text")
        return {k: st.get(k) for k in keys} | {
            "disk_clean_local": st.get("disk_clean_local", True),
            "search_engines": {k: v[0] for k, v in SEARCH_ENGINES.items()},
            "projects_root": str(self.projects_root()),
        }

    async def handle_ui_open_url(self, request: web.Request) -> web.Response:
        """Открыть страницу в браузере по умолчанию. Только страницы Яндекса для получения токена."""
        url = str((await request.json()).get("url", ""))
        if not url.startswith(("https://oauth.yandex.ru/", "https://yandex.ru/dev/disk")):
            return web.json_response({"error": "Можно открывать только страницы oauth.yandex.ru"}, status=400)
        from teacher.osbridge import open_url
        open_url(url)
        return web.json_response({"ok": True})

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
        """Настройки сбора файлов и папки проектов."""
        out: dict[str, Any] = {}
        if "watch_folders" in body:
            folders = []
            for f in body["watch_folders"][:10]:
                base = f.get("base")
                sub = str(f.get("sub", "")).strip().strip("\\/")
                if base not in ("downloads", "desktop", "documents"):
                    raise ValueError("Папка должна начинаться с «Загрузки», «Рабочий стол» или «Документы»")
                if ".." in sub.replace("\\", "/").split("/") or ":" in sub:
                    raise ValueError(f"Некорректная папка: {sub}")
                folders.append({"base": base, "sub": sub})
            if not folders:
                raise ValueError("Нужна хотя бы одна папка")
            out["watch_folders"] = folders
        if "materials_dest" in body:
            m = body["materials_dest"] or {}
            sub = str(m.get("sub", "")).strip().strip("\\/")
            if m.get("base") not in ("desktop", "documents", "downloads"):
                raise ValueError("Папка материалов: «Рабочий стол», «Документы» или «Загрузки»")
            if ".." in sub.replace("\\", "/").split("/") or ":" in sub:
                raise ValueError("Некорректная папка материалов")
            # Папка материалов не должна содержать отслеживаемые папки (иначе проекты перестанут собираться)
            watched = out.get("watch_folders") or self.storage.settings()["watch_folders"]
            ms = sub.replace("\\", "/").strip("/").lower()
            for w in watched:
                ws = w["sub"].replace("\\", "/").strip("/").lower()
                if w["base"] == m["base"] and (ms == "" or ms == ws or ws.startswith(ms + "/")):
                    raise ValueError("Папка материалов не может совпадать с папкой сбора проектов или содержать её")
            out["materials_dest"] = {"base": m["base"], "sub": sub}
        if "lock_text" in body:
            t = " ".join(str(body["lock_text"]).split())[:200]
            out["lock_text"] = t or "Внимание на учителя!"
        if "auto_update" in body:
            out["auto_update"] = bool(body["auto_update"])
        if "disk_clean_local" in body:
            out["disk_clean_local"] = bool(body["disk_clean_local"])
        if "ignore_patterns" in body:
            out["ignore_patterns"] = clean_patterns(body["ignore_patterns"])
        if "max_file_mb" in body:
            mb = body["max_file_mb"]
            if not isinstance(mb, int) or not 1 <= mb <= 4096:
                raise ValueError("Лимит размера: от 1 до 4096 МБ")
            out["max_file_mb"] = mb
        if "active_group" in body:   # старый формат (одна группа)
            g = body["active_group"]
            body = {**body, "active_groups": [] if g in (None, "", 0) else [g]}
        if "active_groups" in body:
            ids = []
            for g in body["active_groups"] or []:
                if not self.db.one("SELECT 1 FROM groups WHERE id=?", (int(g),)):
                    raise ValueError("Группа не найдена")
                if int(g) not in ids:
                    ids.append(int(g))
            out["active_groups"] = ids
            out["active_group"] = None
        if "name_threshold" in body:
            t = body["name_threshold"]
            if not isinstance(t, int) or not 60 <= t <= 100:
                raise ValueError("Порог похожести имён: от 60 до 100")
            out["name_threshold"] = t
        if "disk_root" in body:
            from teacher.yadisk import root_path
            name = root_path(" ".join(str(body["disk_root"]).split())[:150])
            if not name:
                raise ValueError("Укажите папку на Диске, например: Марьино/2026-2027")
            out["disk_root"] = name
        if "disk_client_id" in body:
            cid = str(body["disk_client_id"]).strip()
            if cid and not re.fullmatch(r"[0-9a-fA-F]{32}", cid):
                raise ValueError("ClientID — это 32 символа (цифры и буквы a–f)")
            out["disk_client_id"] = cid
        if "projects_root" in body:
            root = str(body["projects_root"]).strip()
            if root and not Path(root).expanduser().is_absolute():
                raise ValueError("Папка проектов: нужен полный путь")
            out["projects_root"] = root
        return out

    def projects_root(self) -> Path:
        root = self.storage.settings().get("projects_root") or ""
        return Path(root).expanduser() if root else Path.home() / "Documents" / "Компьютерный класс"

    def collect_config(self) -> dict:
        st = self.storage.settings()
        return {"folders": st["watch_folders"], "max_file_mb": st["max_file_mb"], "ignore": st.get("ignore_patterns", []),
                "materials": st.get("materials_dest") or {"base": "desktop", "sub": "Материалы"}}

    async def _send_config(self, conn: AgentConn) -> None:
        try:
            await conn.ws.send_str(Message(MsgType.CONFIG, self.collect_config()).to_json())
        except ConnectionError:
            pass

    async def _config_changed(self, updates: dict) -> None:
        if {"watch_folders", "max_file_mb", "ignore_patterns", "materials_dest"} & set(updates):
            for conn in list(self.agents.values()):
                await self._send_config(conn)

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

    def rename_laptop(self, old: str, new: str, swap: bool = False) -> None:
        """
        Сменить номер ноутбука (или поменять два номера местами). Вместе с номером
        переносится всё, что к нему привязано: история полученных файлов (по ней
        «Конец урока» решает, что можно убрать), недокачанные файлы, отправленные
        материалы, заставка, статусы обновления. Ноутбуку ничего сообщать не нужно —
        учитель узнаёт его по токену.
        """
        self.storage.rename_laptop(old, new, swap=swap)
        tmp = f"__swap__{uuid.uuid4().hex}"

        def move(a: str, b: str) -> None:
            self.db.q("UPDATE received SET laptop=? WHERE laptop=?", (b, a))
            pa = self.files.partial_root / f"laptop-{a}"
            if pa.exists():
                pa.rename(self.files.partial_root / f"laptop-{b}")
            for pkg in self.materials.packages.values():
                if a in pkg["targets"]:
                    pkg["targets"][b] = pkg["targets"].pop(a)
            for d in (self.locked, self.updates.status):
                if a in d:
                    d[b] = d.pop(a)

        move(old, tmp)
        move(new, old)  # если new был занят — его данные переезжают на old (обмен)
        move(tmp, new)
        for pkg in self.materials.packages.values():
            self.materials._save(pkg)
        # Подключения
        a, b = self.agents.pop(old, None), self.agents.pop(new, None)
        if a is not None:
            a.number = new
            self.agents[new] = a
        if b is not None:
            b.number = old
            self.agents[old] = b
        log.info("Номер ноутбука %s → %s%s", old, new, " (обмен номерами)" if swap else "")

    async def handle_ui_rename(self, request: web.Request) -> web.Response:
        old = request.match_info["number"]
        body = await request.json()
        new = str(body.get("new_number", "")).strip()
        if not LAPTOP_NUMBER_RE.match(new):
            return web.json_response({"error": "Номер: до 10 символов — цифры или буквы"}, status=400)
        if new == old:
            return web.json_response({"ok": True})
        try:
            self.rename_laptop(old, new, swap=bool(body.get("swap")))
        except KeyError:
            return web.json_response({"error": "Ноутбук не найден"}, status=404)
        except ValueError:
            return web.json_response({"error": f"Номер {new} уже занят", "busy": True}, status=409)
        await self.broadcast_state()
        return web.json_response({"ok": True})

    async def handle_ui_unpair(self, request: web.Request) -> web.Response:
        number = request.match_info["number"]
        conn = self.agents.get(number)
        info = self.storage.laptops().get(number) or {}
        host = (conn.hostname if conn else "") or info.get("hostname", "")
        if host:
            # Если этот ноутбук снова постучится — предложим ему тот же номер
            former = dict(self.storage.settings().get("former_laptops") or {})
            former[host] = number
            self.storage.update_settings(former_laptops=dict(list(former.items())[-100:]))
        removed = self.storage.remove_laptop(number)
        if conn:
            await conn.ws.close(code=4003, message=b"unpaired")
        await self.broadcast_state()
        return web.json_response({"removed": removed})

    # ---------- Что открыто на ноутбуке ----------

    async def _ask_agent(self, number: str, action: Action, params: dict, timeout: float) -> dict:
        """Команда одному ноутбуку с ожиданием ответа; ошибки — сразу HTTP-ответом для панели."""
        if number not in self.agents:
            raise web.HTTPConflict(text=json.dumps({"error": f"Ноутбук {number} не на связи"}, ensure_ascii=False),
                                   content_type="application/json")
        try:
            r = await self.command_and_wait(number, action, params, timeout)
        except ProtocolError as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False),
                                     content_type="application/json") from e
        if r is None:
            raise web.HTTPGatewayTimeout(text=json.dumps({"error": f"Ноутбук {number} не ответил"}, ensure_ascii=False),
                                         content_type="application/json")
        if not r.get("ok"):
            msg = str(r.get("message", ""))[:300] or "ошибка"
            if "Неизвестное действие" in msg:
                msg = "На ноутбуке старая версия агента — обновите его (вкладка «Настройки» → обновления)"
            raise web.HTTPBadGateway(text=json.dumps({"error": msg}, ensure_ascii=False), content_type="application/json")
        return r

    async def handle_ui_list_open(self, request: web.Request) -> web.Response:
        number = request.match_info["number"]
        r = await self._ask_agent(number, Action.LIST_OPEN, {}, 30)
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        return web.json_response({"number": number, "windows": _clean_windows(data.get("windows")),
                                  "tabs_error": str(data.get("tabs_error") or "")[:200], "time": time.time()})

    async def handle_ui_close_open(self, request: web.Request) -> web.Response:
        number = request.match_info["number"]
        body = await request.json()
        if body.get("kind") == "tab":
            action, params = Action.CLOSE_TAB, {"window": body.get("window"), "title": body.get("title", ""),
                                                "index": body.get("index", 0)}
        else:
            action, params = Action.CLOSE_WINDOW, {"window": body.get("window"), "force": body.get("force") is True}
        r = await self._ask_agent(number, action, params, 25)
        log.info("Ноутбук %s: %s → %s", number, action.value, r.get("message"))
        return web.json_response({"ok": True, "message": str(r.get("message", ""))[:300]})

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


def _clean_windows(raw) -> list[dict]:
    """Список окон от агента — только ожидаемые поля и разумной длины."""
    out = []
    for w in raw if isinstance(raw, list) else []:
        if not isinstance(w, dict) or not isinstance(w.get("id"), int) or isinstance(w.get("id"), bool):
            continue
        tabs = []
        for t in w.get("tabs") if isinstance(w.get("tabs"), list) else []:
            if isinstance(t, dict) and isinstance(t.get("index"), int):
                tabs.append({"title": str(t.get("title") or "")[:300], "index": t["index"],
                             "active": t.get("active") is True, "url": str(t.get("url") or "")[:500]})
        out.append({"id": w["id"], "title": str(w.get("title") or "")[:300], "app": str(w.get("app") or "")[:80],
                    "process": str(w.get("process") or "")[:80], "browser": w.get("browser") is True,
                    "minimized": w.get("minimized") is True, "tabs": tabs[:200]})
    return out[:100]


def _sort_key(number: str):
    """Сортировка номеров «по-человечески»: 2 раньше 10."""
    return (0, int(number), "") if number.isdigit() else (1, 0, number)
