"""
Импорт групп и учеников из папки с работами (без ручного ввода).

Ожидаемая структура (как в «Проектах Детей» или на Яндекс Диске):
    <папка>/М6-1(2)/Ваня6-1(2) проект.pptx
    <папка>/М6-1(2)/Дамир Проект Codu.Kodu2
    <папка>/М6-3/Вася6-3 проект.pptx
Папки с названием-номером группы становятся группами, имена детей берутся из
названий файлов и папок внутри. Учитель видит предложение и правит его перед сохранением.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from shared.ignore import BUILTIN_PATTERNS, ignored_by_patterns
from teacher.names import looks_like_group_folder, name_from_filename, normalize

if TYPE_CHECKING:
    from teacher.server import TeacherServer

MAX_DEPTH = 4


def scan_folder(root: Path) -> list[dict]:
    """Найти папки-группы и имена детей в них. [{"name", "students": [{"name", "files"}]}]"""
    groups: dict[str, Counter] = {}
    order: list[str] = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        depth = len(d.relative_to(root).parts)
        dirnames[:] = [x for x in dirnames if not x.startswith(".")] if depth < MAX_DEPTH else []
        if not looks_like_group_folder(d.name) or d == root and not looks_like_group_folder(root.name):
            continue
        key = d.name.strip()
        if key not in groups:
            groups[key] = Counter()
            order.append(key)
        # Имена — из файлов и папок прямо внутри папки группы
        for item in list(filenames) + list(dirnames):
            if item.startswith(".") or ignored_by_patterns(item, BUILTIN_PATTERNS) or looks_like_group_folder(item):
                continue
            name = name_from_filename(item)
            if name:
                groups[key][name] += 1
    out = []
    for key in order:
        # Объединяем варианты одного имени («Кирилл» и «КИРИЛЛ» уже приведены)
        merged: dict[str, dict] = {}
        for name, cnt in groups[key].most_common():
            k = normalize(name)
            if k in merged:
                merged[k]["files"] += cnt
            else:
                merged[k] = {"name": name, "files": cnt}
        out.append({"name": key, "students": sorted(merged.values(), key=lambda s: s["name"])})
    return out


class Importer:
    def __init__(self, server: "TeacherServer"):
        self.server = server

    def routes(self) -> list:
        return [
            web.post("/ui/api/import/pick", self.handle_pick),
            web.post("/ui/api/import/scan", self.handle_scan),
            web.post("/ui/api/import/apply", self.handle_apply),
        ]

    async def handle_pick(self, request: web.Request) -> web.Response:
        """Окно выбора папки macOS → сразу сканируем."""
        import asyncio

        from teacher.materials import pick_dialog
        paths = await asyncio.to_thread(pick_dialog, "folders")
        if not paths:
            return web.json_response({"path": None, "groups": []})
        groups = await asyncio.to_thread(scan_folder, Path(paths[0]))
        return web.json_response({"path": paths[0], "groups": self._mark_existing(groups)})

    async def handle_scan(self, request: web.Request) -> web.Response:
        import asyncio
        path = Path(str((await request.json()).get("path", ""))).expanduser()
        if not path.is_dir():
            return web.json_response({"error": "Папка не найдена"}, status=400)
        groups = await asyncio.to_thread(scan_folder, path)
        return web.json_response({"path": str(path), "groups": self._mark_existing(groups)})

    def _mark_existing(self, groups: list[dict]) -> list[dict]:
        db = self.server.db
        existing = {g["name"].lower(): g for g in db.groups()}
        for g in groups:
            eg = existing.get(g["name"].replace(":", ".").lower())
            g["exists"] = bool(eg)
            have = {normalize(s["name"]) for s in eg["students"]} if eg else set()
            for s in g["students"]:
                s["exists"] = normalize(s["name"]) in have
        return groups

    async def handle_apply(self, request: web.Request) -> web.Response:
        """Создать группы и учеников (существующие не трогаем, дубликаты пропускаем)."""
        body = await request.json()
        db, sorter = self.server.db, self.server.sorter
        made_groups = made_students = 0
        errors = []
        for g in body.get("groups", [])[:200]:
            try:
                from teacher.sorter import _clean_name
                gname = _clean_name(g.get("name", ""), "название группы")
            except ValueError as e:
                errors.append(str(e))
                continue
            row = db.one("SELECT id FROM groups WHERE name=?", (gname,))
            gid = row["id"] if row else db.insert("INSERT INTO groups(name) VALUES (?)", (gname,))
            made_groups += 0 if row else 1
            have = {normalize(s["name"]) for s in db.students(gid)}
            for s in g.get("students", [])[:200]:
                if normalize(str(s.get("name", ""))) in have:
                    continue   # такой ученик уже есть — молча пропускаем
                try:
                    name, aliases = sorter._check_student(gid, str(s.get("name", "")), s.get("aliases", []))
                except ValueError as e:
                    errors.append(str(e))
                    continue
                if normalize(name) in have:
                    continue
                db.insert("INSERT INTO students(group_id, name, aliases) VALUES (?,?,?)",
                          (gid, name, json.dumps(aliases, ensure_ascii=False)))
                have.add(normalize(name))
                made_students += 1
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"groups": made_groups, "students": made_students, "errors": errors[:20]})
