"""
Отправка материалов урока на ноутбуки (файлы и папки).

  • Учитель выбирает файлы/папки в обычном окне macOS (osascript «choose file/folder»).
  • Mac собирает «пакет»: список файлов с размерами и SHA-256. Файлы НЕ копируются —
    раздаются прямо с диска (быстро и без лишнего места).
  • Ноутбуки получают команду RECEIVE_FILES и сами скачивают файлы параллельно,
    с докачкой (HTTP Range) и проверкой SHA-256. Ход загрузки — в панели.
  • Ноутбук, который был выключен, получит пакет при подключении (в течение суток).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from shared.materials import MAX_PACKAGE_BYTES, MAX_PACKAGE_FILES, PACKAGE_ID_RE, UnsafePath, safe_rel_path
from shared.protocol import Action, Message, MsgType

if TYPE_CHECKING:
    from teacher.server import AgentConn, TeacherServer

log = logging.getLogger(__name__)

DELIVERY_WINDOW = 24 * 3600     # сколько ждать выключенные ноутбуки
KEEP_PACKAGES = 30


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _rel_name(name: str) -> str:
    """Имя файла/папки с Mac → допустимое в Windows."""
    bad = '<>:"|?*\\'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in name).rstrip(" .")
    return out[:150] or "файл"


def collect_items(paths: list[str]) -> list[dict]:
    """Развернуть выбранные файлы и папки в список файлов с относительными путями."""
    items: list[dict] = []
    used: set[str] = set()

    def top_name(name: str) -> str:
        base, n = _rel_name(name), 2
        cand = base
        while cand.lower() in used:
            stem, dot, ext = base.rpartition(".")
            cand = f"{stem} ({n}).{ext}" if dot and stem else f"{base} ({n})"
            n += 1
        used.add(cand.lower())
        return cand

    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            items.append({"src": str(p), "rel": top_name(p.name)})
        elif p.is_dir():
            root_name = top_name(p.name)
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for fn in sorted(filenames):
                    if fn.startswith(".") or fn in ("Thumbs.db", "desktop.ini"):
                        continue
                    full = Path(dirpath) / fn
                    rel_parts = [root_name] + [_rel_name(x) for x in full.relative_to(p).parts]
                    items.append({"src": str(full), "rel": "/".join(rel_parts)})
                    if len(items) > MAX_PACKAGE_FILES:
                        raise ValueError(f"Слишком много файлов (больше {MAX_PACKAGE_FILES})")
    return items


def pick_dialog(kind: str) -> list[str]:
    """Окно выбора файлов/папок (Mac или Windows). kind: files | folders. Отмена → []."""
    from teacher.osbridge import pick_dialog as _pick
    return _pick(kind)


class MaterialsManager:
    def __init__(self, server: "TeacherServer"):
        self.server = server
        self.dir = server.storage.data_dir / "packages"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.packages: dict[str, dict] = {}
        self._load()
        self._last_broadcast = 0.0
        server.on_agent_connected.append(self._on_connected)
        server.on_agent_report.append(self._on_report)

    # ================= Хранение =================

    def _load(self) -> None:
        for f in sorted(self.dir.glob("*.json")):
            try:
                pkg = json.loads(f.read_text(encoding="utf-8"))
                self.packages[pkg["id"]] = pkg
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, pkg: dict) -> None:
        tmp = self.dir / f"{pkg['id']}.tmp"
        tmp.write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / f"{pkg['id']}.json")
        # Храним только последние пакеты
        old = sorted(self.packages.values(), key=lambda p: p["created"])[:-KEEP_PACKAGES]
        for p in old:
            self.packages.pop(p["id"], None)
            (self.dir / f"{p['id']}.json").unlink(missing_ok=True)

    def public(self) -> list[dict]:
        out = []
        for p in sorted(self.packages.values(), key=lambda p: p["created"], reverse=True)[:10]:
            out.append({k: p[k] for k in ("id", "name", "created", "count", "total_size", "state", "targets")})
        return out

    # ================= Создание и отправка =================

    async def create(self, paths: list[str]) -> dict:
        items = await asyncio.to_thread(collect_items, paths)
        if not items:
            raise ValueError("Не выбрано ни одного файла")
        pkg_id = uuid.uuid4().hex
        names = [Path(p).name for p in paths]
        name = names[0] if len(names) == 1 else f"{names[0]} и ещё {len(names) - 1}"
        pkg = {"id": pkg_id, "name": name[:120], "created": time.time(), "state": "preparing",
               "count": len(items), "total_size": 0, "files": [], "targets": {}}
        self.packages[pkg_id] = pkg
        await self.server.broadcast_event({"kind": "materials", "packages": self.public()})

        def hash_all():
            total = 0
            files = []
            for it in items:
                src = Path(it["src"])
                st = src.stat()
                total += st.st_size
                if total > MAX_PACKAGE_BYTES:
                    raise ValueError("Слишком большой объём (больше 50 ГБ)")
                files.append({"path": safe_rel_path(it["rel"]), "src": it["src"], "size": st.st_size,
                              "mtime_ns": st.st_mtime_ns, "sha256": _sha256(src)})
            return files, total

        try:
            files, total = await asyncio.to_thread(hash_all)
        except (OSError, ValueError, UnsafePath) as e:
            self.packages.pop(pkg_id, None)
            await self.server.broadcast_event({"kind": "materials", "packages": self.public()})
            raise ValueError(str(e)) from e
        pkg.update(files=files, total_size=total, state="ready")
        self._save(pkg)
        log.info("Пакет «%s»: %s файлов, %s байт", pkg["name"], len(files), total)
        return pkg

    async def send(self, pkg_id: str, numbers: list[str]) -> dict:
        pkg = self.packages[pkg_id]
        for n in numbers:
            pkg["targets"][n] = {"state": "pending", "done": 0, "message": "ждёт подключения ноутбука", "at": time.time()}
        self._save(pkg)
        for n in numbers:
            conn = self.server.agents.get(n)
            if conn is not None:
                await self._deliver(pkg, conn)
        await self._broadcast(force=True)
        return {n: pkg["targets"][n]["state"] for n in numbers}

    async def _deliver(self, pkg: dict, conn: "AgentConn") -> None:
        t = pkg["targets"][conn.number]
        msg = Message(MsgType.COMMAND, {"command_id": uuid.uuid4().hex, "action": Action.RECEIVE_FILES.value,
                                        "params": {"package_id": pkg["id"], "name": pkg["name"]}})
        try:
            await conn.ws.send_str(msg.to_json())
            t.update(state="sent", message="ноутбук начинает загрузку")
        except ConnectionError:
            pass

    async def _on_connected(self, conn: "AgentConn") -> None:
        """Ноутбук подключился: досылаем пакеты, которые он ещё не получил."""
        now = time.time()
        for pkg in list(self.packages.values()):
            t = pkg["targets"].get(conn.number)
            if t and t["state"] in ("pending", "sent", "downloading") and now - pkg["created"] < DELIVERY_WINDOW:
                await self._deliver(pkg, conn)

    async def _on_report(self, conn: "AgentConn", payload: dict) -> None:
        if payload.get("kind") != "materials":
            return
        pkg = self.packages.get(str(payload.get("package_id")))
        if pkg is None or conn.number not in pkg["targets"]:
            return
        t = pkg["targets"][conn.number]
        state = payload.get("state")
        if state in ("downloading", "done", "error"):
            t["state"] = state
        t["done"] = int(payload.get("done") or 0)
        t["message"] = str(payload.get("message", ""))[:300]
        if state in ("done", "error"):
            self._save(pkg)
            log.info("Материалы «%s» на ноутбуке %s: %s", pkg["name"], conn.number, t["message"])
        await self._broadcast(force=state in ("done", "error"))

    async def _broadcast(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_broadcast < 0.5:
            return
        self._last_broadcast = time.monotonic()
        await self.server.broadcast_event({"kind": "materials", "packages": self.public()})

    # ================= Раздача файлов ноутбукам =================

    def _pkg_for_agent(self, request: web.Request) -> dict:
        number = self.server._auth_agent(request)
        if number is None:
            raise web.HTTPUnauthorized(text="Неверный токен")
        pid = request.match_info["id"]
        pkg = self.packages.get(pid) if PACKAGE_ID_RE.match(pid) else None
        if pkg is None or pkg["state"] != "ready" or number not in pkg["targets"]:
            raise web.HTTPNotFound(text="Пакет не найден")
        return pkg

    async def handle_manifest(self, request: web.Request) -> web.Response:
        pkg = self._pkg_for_agent(request)
        return web.json_response({"id": pkg["id"], "name": pkg["name"], "total_size": pkg["total_size"],
                                  "files": [{"i": i, "path": f["path"], "size": f["size"], "sha256": f["sha256"]}
                                            for i, f in enumerate(pkg["files"])]})

    async def handle_file(self, request: web.Request) -> web.StreamResponse:
        pkg = self._pkg_for_agent(request)
        try:
            f = pkg["files"][int(request.match_info["i"])]
        except (ValueError, IndexError):
            raise web.HTTPNotFound()
        src = Path(f["src"])
        try:
            st = src.stat()
        except OSError:
            raise web.HTTPGone(text=f"Файл удалён с Mac: {f['path']}")
        if (st.st_size, st.st_mtime_ns) != (f["size"], f["mtime_ns"]):
            raise web.HTTPConflict(text=f"Файл изменился на Mac после отправки: {f['path']}")
        # FileResponse сам поддерживает докачку (Range) и отдаёт файл максимально быстро (sendfile)
        return web.FileResponse(src, headers={"Content-Type": "application/octet-stream"})

    # ================= API панели =================

    def routes(self) -> list:
        return [
            web.get("/api/packages/{id}/manifest", self.handle_manifest),
            web.get("/api/packages/{id}/files/{i}", self.handle_file),
            web.post("/ui/api/materials/pick", self.handle_pick),
            web.post("/ui/api/materials/send", self.handle_send),
            web.post("/ui/api/materials/{id}/resend", self.handle_resend),
            web.get("/ui/api/materials", self.handle_list),
        ]

    async def handle_pick(self, request: web.Request) -> web.Response:
        kind = (await request.json()).get("kind", "files")
        paths = await asyncio.to_thread(pick_dialog, "folders" if kind == "folders" else "files")
        result = []
        for p in paths:
            pp = Path(p)
            try:
                size = pp.stat().st_size if pp.is_file() else sum(
                    f.stat().st_size for f in pp.rglob("*") if f.is_file() and not f.name.startswith("."))
            except OSError:
                size = 0
            result.append({"path": p, "name": pp.name, "is_dir": pp.is_dir(), "size": size})
        return web.json_response({"items": result})

    def _targets(self, targets) -> list[str]:
        laptops = self.server.storage.laptops()
        nums = sorted(laptops) if targets == "all" else [str(n) for n in targets if str(n) in laptops]
        if not nums:
            raise ValueError("Нет ноутбуков для отправки")
        return nums

    async def handle_send(self, request: web.Request) -> web.Response:
        body = await request.json()
        paths = [str(p) for p in body.get("paths", [])][:200]
        try:
            numbers = self._targets(body.get("targets", "all"))
            pkg = await self.create(paths)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        statuses = await self.send(pkg["id"], numbers)
        return web.json_response({"package_id": pkg["id"], "name": pkg["name"], "count": pkg["count"],
                                  "total_size": pkg["total_size"], "statuses": statuses})

    async def handle_resend(self, request: web.Request) -> web.Response:
        pid = request.match_info["id"]
        pkg = self.packages.get(pid)
        if pkg is None:
            return web.json_response({"error": "Пакет не найден"}, status=404)
        body = await request.json()
        try:
            # Только тем, кто ещё не получил (или указанным)
            targets = body.get("targets") or [n for n, t in pkg["targets"].items() if t["state"] != "done"]
            numbers = self._targets(targets)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        pkg["created"] = time.time()  # продлеваем срок доставки
        return web.json_response({"statuses": await self.send(pid, numbers)})

    async def handle_list(self, request: web.Request) -> web.Response:
        return web.json_response({"packages": self.public()})
