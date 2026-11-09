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

import asyncio
import json
import os
import re
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
        groups[key].update(_names_in(list(filenames) + list(dirnames)))
    return _merge(groups, order)


def _merge(groups: dict[str, Counter], order: list[str]) -> list[dict]:
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


def _names_in(items: list[str]) -> Counter:
    """Имена детей из названий файлов и папок внутри папки группы."""
    c: Counter = Counter()
    for item in items:
        if item.startswith(".") or ignored_by_patterns(item, BUILTIN_PATTERNS) or looks_like_group_folder(item):
            continue
        name = name_from_filename(item)
        if name:
            c[name] += 1
    return c


# ================= Яндекс Диск: ссылка вместо папки =================

_PUBLIC_RE = re.compile(r"^https?://(disk\.yandex\.[a-z]{2,3}|disk\.360\.yandex\.[a-z]{2,3}|yadi\.sk)/(d|i)/[^/?#\s]+", re.I)
_CLIENT_RE = re.compile(r"^https?://disk\.(?:360\.)?yandex\.[a-z]{2,3}/client/disk/?([^?#]*)", re.I)
# Папка ребёнка: «Иван Петров», «Анна-Мария Иванова» — только буквы, 1–4 слова
_KID_FOLDER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:[ -][A-Za-zА-Яа-яЁё]+){0,3}")


def parse_disk_source(text: str) -> tuple[str, str]:
    """
    Что вставил учитель → ("public", публичная ссылка) или ("private", путь на своём Диске).
      https://disk.yandex.ru/d/AbC…                         — публичная ссылка («Поделиться»)
      https://disk.yandex.ru/client/disk/Марьино/2026-2027  — адрес из браузера на своём Диске
      Марьино/2026-2027 или disk:/Марьино/2026-2027          — путь
    """
    from urllib.parse import unquote
    t = " ".join(str(text or "").split())
    if not t:
        raise ValueError("Вставьте ссылку на папку Яндекс Диска")
    m = _PUBLIC_RE.match(t)
    if m:
        return "public", m.group(0)
    m = _CLIENT_RE.match(t)
    if m:
        path = unquote(m.group(1)).strip("/")
        if not path:
            raise ValueError("В ссылке нет папки — откройте нужную папку на Диске и скопируйте адрес")
        return "private", path
    if re.match(r"^[a-z]+://", t, re.I):
        raise ValueError("Это не ссылка на Яндекс Диск")
    path = t[len("disk:"):] if t.lower().startswith("disk:") else t
    path = "/".join(x.strip() for x in path.replace("\\", "/").split("/") if x.strip())
    if not path or ".." in path.split("/"):
        raise ValueError("Некорректный путь на Диске")
    return "private", path


# Папки программ и направлений — не дети
_NOT_KID_WORDS = {"make", "makecode", "tinkercad", "scratchjr", "scratch", "capcut", "animaker", "apitor", "kodu",
                  "minecraft", "roblox", "python", "робототехника", "программирование", "робот", "лагерь", "материалы",
                  "проекты", "работы", "уроки", "архив", "группа", "группы"}


def kid_names_from_folders(names: list[str], extra_not: set[str] = frozenset()) -> list[str]:
    """Папки детей среди папок: «Иван Петров» — да, «Лагерь 14-08-26», «Make Code» (программа) — нет."""
    from teacher.names import _NOT_NAMES
    out = []
    for n in names:
        n = " ".join(n.split())
        words = {w.lower() for w in n.replace("-", " ").split()}
        if not _KID_FOLDER_RE.fullmatch(n) or n in out or n.lower() in extra_not:
            continue
        if words & (_NOT_NAMES | _NOT_KID_WORDS):
            continue
        out.append(n)
    return out


class Importer:
    def __init__(self, server: "TeacherServer"):
        self.server = server

    def routes(self) -> list:
        return [
            web.post("/ui/api/import/pick", self.handle_pick),
            web.post("/ui/api/import/scan", self.handle_scan),
            web.post("/ui/api/import/disk", self.handle_disk),
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

    async def handle_disk(self, request: web.Request) -> web.Response:
        """Импорт по ссылке на папку Яндекс Диска (публичной или своего Диска)."""
        import aiohttp

        from teacher.yadisk import DiskAuthError
        text = str((await request.json()).get("link", ""))
        try:
            kind, src = parse_disk_source(text)
            result = await self._scan_disk(kind, src)
        except DiskAuthError as e:
            return web.json_response({"error": str(e)}, status=401)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
            return web.json_response({"error": f"Нет связи с Яндекс Диском: {e}"}, status=502)
        return web.json_response(result)

    async def _scan_disk(self, kind: str, src: str) -> dict:
        import aiohttp
        disk = self.server.disk
        sem = asyncio.Semaphore(8)
        if kind == "private":
            session = disk.session(total=120)

            async def ls(rel: str):
                async with sem:
                    return await disk.list_dir(session, "/".join(x for x in (src, rel) if x))
            root_name = src.rsplit("/", 1)[-1]
            shown = src
        else:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

            async def ls(rel: str):
                out: list[dict] = []
                async with sem:
                    while True:
                        params = {"public_key": src, "path": "/" + rel, "limit": "1000", "offset": str(len(out)),
                                  "fields": "name,_embedded.items.name,_embedded.items.type,_embedded.total"}
                        async with session.get(f"{disk.api_base}/public/resources", params=params) as r:
                            if r.status == 404:
                                return None
                            data = (await disk._check(r)).get("_embedded") or {}
                        items = data.get("items") or []
                        out += [{"name": str(i.get("name", "")), "type": i.get("type")} for i in items]
                        if not items or len(out) >= int(data.get("total") or 0):
                            return out
            root_name, shown = "", src
        async with session:
            items = await ls("")
            if items is None:
                raise ValueError("Папка не найдена — проверьте ссылку (у публичной ссылки должен быть открыт доступ)")
            dirs = [i["name"] for i in items if i["type"] == "dir"]
            group_dirs = [d for d in dirs if looks_like_group_folder(d)]
            if looks_like_group_folder(root_name):
                # Ссылка прямо на папку группы
                counter = _names_in([i["name"] for i in items])
                return {"source": shown, "mode": "groups",
                        "groups": self._mark_existing(_merge({root_name: counter}, [root_name]))}
            if group_dirs:
                inner = await asyncio.gather(*(ls(d) for d in group_dirs))
                groups = {d: _names_in([i["name"] for i in (lst or [])]) for d, lst in zip(group_dirs, inner)}
                return {"source": shown, "mode": "groups", "groups": self._mark_existing(_merge(groups, group_dirs))}
        st = self.server.storage.settings()
        programs = {x.lower() for x in [*(st.get("disk_known_programs") or []), *(st.get("disk_programs") or [])]}
        kids = kid_names_from_folders(dirs, programs)
        if not kids:
            raise ValueError("В папке не нашлось ни папок групп (например, «6-1»), ни папок детей («Имя Фамилия»)")
        # Чтобы не было дублей: дети, которые уже есть в какой-нибудь группе
        from teacher.disk_send import folder_matches
        students = self.server.db.students()
        links = {r["folder"]: r["student_id"] for r in self.server.db.q("SELECT * FROM disk_links")}
        out = []
        for k in kids:
            same = [st for st in students if st["id"] == links.get(k)] or \
                   [st for st in students if folder_matches(st["name"], k)]
            out.append({"name": k, "exists_in": same[0]["group_name"] if same else "",
                        "exists_as": same[0]["name"] if same else ""})
        # Если это и есть папка детей из настроек Диска — запомним папку каждого нового ребёнка
        is_root = kind == "private" and src.strip("/") == disk.root()
        return {"source": shown, "mode": "kids", "kids": out, "link_folders": is_root}

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
                sid = db.insert("INSERT INTO students(group_id, name, aliases) VALUES (?,?,?)",
                                (gid, name, json.dumps(aliases, ensure_ascii=False)))
                folder = " ".join(str(s.get("disk_folder") or "").split())
                if folder and "/" not in folder and len(folder) <= 200:
                    db.q("INSERT OR REPLACE INTO disk_links(student_id, folder) VALUES (?,?)", (sid, folder))
                have.add(normalize(name))
                made_students += 1
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"groups": made_groups, "students": made_students, "errors": errors[:20]})
