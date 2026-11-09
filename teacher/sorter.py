"""
Сортировка полученных файлов по ученикам.

  совпал ровно один ученик → Проекты/<Группа>/<Имя>/<ГГГГ-ММ-ДД>/<файл>
  ноль или несколько       → Неразобранное/<ГГГГ-ММ-ДД>/<файл>

Исходник всегда остаётся во «Входящих» (архив). В «Проекты» кладём жёсткую
ссылку (места на диске не занимает) или копию. Ничего не перезаписываем.

Если в настройках выбрана «текущая группа», ищем учеников только в ней —
так «Маша» из другой группы не мешает.
"""

from __future__ import annotations

import json
from datetime import date
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from shared.ignore import BUILTIN_PATTERNS, clean_patterns, ignored_by_patterns
from teacher.inbox import safe_name, unique_path
from teacher.names import DEFAULT_THRESHOLD, group_code, match_students, suggest_alias

if TYPE_CHECKING:
    from teacher.server import TeacherServer

log = logging.getLogger(__name__)


def place(src: Path, dest: Path) -> Path:
    """Положить файл в dest (с суффиксом при конфликте): жёсткая ссылка, иначе копия."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
    return dest


def _clean_name(value: str, what: str) -> str:
    v = " ".join(str(value).split())[:60].replace(":", ".")  # «Пн 10:00» → «Пн 10.00» (двоеточие нельзя в Finder)
    if not v:
        raise ValueError(f"Введите {what}")
    if safe_name(v) != v:
        raise ValueError(f"{what.capitalize()} не может содержать символы / \\ * ? \" < > |")
    return v


class Sorter:
    def __init__(self, server: "TeacherServer"):
        self.server = server
        self.db = server.db
        self.on_file_sorted: list = []   # обработчики (выгрузка на Яндекс Диск)
        server.files.on_file_received.append(self.sort_new)

    def routes(self) -> list:
        return [
            web.get("/ui/api/projects", self.handle_projects),
            web.post("/ui/api/groups", self.handle_group_create),
            web.put("/ui/api/groups/{id}", self.handle_group_update),
            web.delete("/ui/api/groups/{id}", self.handle_group_delete),
            web.post("/ui/api/students", self.handle_student_create),
            web.post("/ui/api/students/bulk", self.handle_students_bulk),
            web.put("/ui/api/students/{id}", self.handle_student_update),
            web.delete("/ui/api/students/{id}", self.handle_student_delete),
            web.post("/ui/api/files/{id}/assign", self.handle_assign),
            web.post("/ui/api/files/{id}/reveal", self.handle_reveal),
            web.post("/ui/api/files/{id}/open", self.handle_open_file),
            web.get("/ui/api/students/{id}/files", self.handle_student_files),
            web.post("/ui/api/files/{id}/delete", self.handle_delete),
            web.post("/ui/api/files/delete-unsorted-by-name", self.handle_delete_by_name),
            web.post("/ui/api/ignore", self.handle_ignore),
            web.post("/ui/api/open-folder", self.handle_open_folder),
        ]

    # ================= Сортировка =================

    def active_groups(self) -> list[int]:
        """Группы, которые сейчас занимаются (их может быть несколько — например, 6-1 и 6-1(2))."""
        st = self.server.storage.settings()
        ids = st.get("active_groups")
        if ids is None and st.get("active_group"):      # настройка старой версии
            ids = [st["active_group"]]
        return [int(i) for i in ids or []]

    def candidates(self, *texts: str) -> list[dict]:
        """
        Среди кого искать ученика:
          1) если в названии файла (или пути) есть номер группы («Ваня6-1(2) проект») и такая
             группа существует — только в ней (так различаются Вани из 6-1 и 6-1(2));
          2) иначе — среди групп, которые сейчас занимаются;
          3) если ни одна не выбрана — среди всех.
        """
        for t in texts:
            code = group_code(t or "")
            if code:
                same = [g["id"] for g in self.db.q("SELECT id, name FROM groups") if group_code(g["name"]) == code]
                if same:
                    return [s for s in self.db.students() if s["group_id"] in same]
        active = self.active_groups()
        if active:
            return [s for s in self.db.students() if s["group_id"] in active]
        return self.db.students()

    def project_dir(self, student: dict, lesson_date: str) -> Path:
        return self.server.projects_root() / "Проекты" / student["group_name"] / student["name"] / lesson_date

    async def sort_new(self, file_id: int) -> None:
        f = self.db.one("SELECT * FROM received WHERE id=?", (file_id,))
        if f is None or f["status"] != "new":
            return
        threshold = int(self.server.storage.settings().get("name_threshold", DEFAULT_THRESHOLD))
        found = match_students(f["name"], self.candidates(f["name"], f["rel_path"]), threshold)
        src = Path(f["inbox_path"])
        if len(found) == 1:
            student = self.db.student(found[0]["id"])
            dest = place(src, self.project_dir(student, f["lesson_date"]) / f["name"])
            self.db.q("UPDATE received SET status='sorted', student_id=?, project_path=? WHERE id=?",
                      (student["id"], str(dest), file_id))
            log.info("«%s» → %s / %s", f["name"], student["group_name"], student["name"])
            await self._sorted(file_id)
        else:
            dest = place(src, self.server.projects_root() / "Неразобранное" / f["lesson_date"] / f["name"])
            self.db.q("UPDATE received SET status='unsorted', project_path=? WHERE id=?", (str(dest), file_id))
            why = "несколько учеников: " + ", ".join(s["name"] for s in found) if found else "имя не найдено"
            log.info("«%s» → Неразобранное (%s)", f["name"], why)
        await self.server.broadcast_event({"kind": "projects_changed"})

    async def _sorted(self, file_id: int) -> None:
        for handler in self.on_file_sorted:
            try:
                await handler(file_id)
            except Exception:
                log.exception("Ошибка после сортировки")

    async def assign(self, file_id: int, student_id: int, remember_alias: str = "") -> dict:
        """Назначить файл ученику (из «Неразобранного» или переназначить)."""
        f = self.db.one("SELECT * FROM received WHERE id=?", (file_id,))
        student = self.db.student(student_id)
        if f is None or student is None:
            raise ValueError("Файл или ученик не найден")
        dest = unique_path(self.project_dir(student, f["lesson_date"]) / f["name"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        old = Path(f["project_path"]) if f["project_path"] else None
        if old and old.exists():
            os.replace(old, dest)      # переносим (в пределах одной папки проектов)
            self._remove_empty_dirs(old.parent)
        else:
            dest = place(Path(f["inbox_path"]), dest)
        self.db.q("UPDATE received SET status='sorted', student_id=?, project_path=? WHERE id=?",
                  (student_id, str(dest), file_id))
        alias = " ".join(remember_alias.split())[:60]
        if alias and alias.lower() not in [a.lower() for a in student["aliases"]] and alias.lower() != student["name"].lower():
            self.db.q("UPDATE students SET aliases=? WHERE id=?",
                      (json.dumps(student["aliases"] + [alias], ensure_ascii=False), student_id))
            student = self.db.student(student_id)
        log.info("«%s» назначен ученику %s / %s", f["name"], student["group_name"], student["name"])
        await self._sorted(file_id)
        await self.server.broadcast_event({"kind": "projects_changed"})
        # Если имя ученика не распознаётся в имени файла — предложим запомнить написание
        suggestion = ""
        if not match_students(f["name"], [student]):
            suggestion = suggest_alias(f["name"], student["name"])
        return {"path": str(dest), "suggest_alias": suggestion, "student_id": student_id, "student": student["name"]}

    def _remove_empty_dirs(self, d: Path) -> None:
        root = self.server.projects_root()
        while d != root and root in d.parents:
            try:
                d.rmdir()
            except OSError:
                return
            d = d.parent

    # ================= API панели =================

    async def handle_projects(self, request: web.Request) -> web.Response:
        def files(where: str, limit: int) -> list[dict]:
            rows = self.db.q(
                "SELECT r.id, r.laptop, r.name, r.size, r.received_at, r.lesson_date, r.status, r.project_path,"
                " s.name AS student, g.name AS group_name FROM received r"
                " LEFT JOIN students s ON s.id = r.student_id LEFT JOIN groups g ON g.id = s.group_id"
                f" WHERE {where} ORDER BY r.received_at DESC LIMIT {limit}")
            return rows
        unsorted = files("r.status='unsorted'", 500)
        st = self.server.storage.settings()
        return web.json_response({
            "groups": self.db.groups(date.today().isoformat()),
            "active_group": (self.active_groups() or [None])[0],
            "active_groups": self.active_groups(),
            "unsorted": unsorted,
            "recent": files("1=1", 100),
            "root": str(self.server.projects_root()),
        })

    async def handle_group_create(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            name = _clean_name(body.get("name", ""), "название группы")
            if self.db.one("SELECT 1 FROM groups WHERE name=?", (name,)):
                raise ValueError("Такая группа уже есть")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        gid = self.db.insert("INSERT INTO groups(name) VALUES (?)", (name,))
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"id": gid})

    async def handle_group_update(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            name = _clean_name(body.get("name", ""), "название группы")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        self.db.q("UPDATE groups SET name=? WHERE id=?", (name, int(request.match_info["id"])))
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"ok": True})

    async def handle_group_delete(self, request: web.Request) -> web.Response:
        gid = int(request.match_info["id"])
        self.db.q("UPDATE received SET student_id=NULL WHERE student_id IN (SELECT id FROM students WHERE group_id=?)", (gid,))
        self.db.q("DELETE FROM students WHERE group_id=?", (gid,))
        self.db.q("DELETE FROM groups WHERE id=?", (gid,))
        if gid in self.active_groups():
            self.server.storage.update_settings(active_groups=[g for g in self.active_groups() if g != gid], active_group=None)
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"ok": True})

    def _check_student(self, group_id: int, name: str, aliases, exclude_id: int | None = None) -> tuple[str, list[str]]:
        name = _clean_name(name, "имя ученика")
        if not self.db.one("SELECT 1 FROM groups WHERE id=?", (group_id,)):
            raise ValueError("Группа не найдена")
        dup = self.db.one("SELECT id FROM students WHERE group_id=? AND name=?", (group_id, name))
        if dup and dup["id"] != exclude_id:
            raise ValueError(f"В группе уже есть «{name}». Добавьте фамилию или букву: «{name} К.»")
        clean = []
        for a in aliases or []:
            a = " ".join(str(a).split())[:60]
            if a and a.lower() not in [c.lower() for c in clean]:
                clean.append(a)
        return name, clean[:30]

    async def handle_student_create(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            gid = int(body.get("group_id"))
            name, aliases = self._check_student(gid, body.get("name", ""), body.get("aliases", []))
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        sid = self.db.insert("INSERT INTO students(group_id, name, aliases) VALUES (?,?,?)",
                             (gid, name, json.dumps(aliases, ensure_ascii=False)))
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"id": sid})

    async def handle_students_bulk(self, request: web.Request) -> web.Response:
        """Список учеников текстом: каждая строка «Имя, вариант, вариант»."""
        body = await request.json()
        added, errors = 0, []
        try:
            gid = int(body.get("group_id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "Выберите группу"}, status=400)
        for line in str(body.get("text", "")).splitlines():
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if not parts:
                continue
            try:
                name, aliases = self._check_student(gid, parts[0], parts[1:])
            except ValueError as e:
                errors.append(str(e))
                continue
            self.db.insert("INSERT INTO students(group_id, name, aliases) VALUES (?,?,?)",
                           (gid, name, json.dumps(aliases, ensure_ascii=False)))
            added += 1
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"added": added, "errors": errors})

    async def handle_student_update(self, request: web.Request) -> web.Response:
        sid = int(request.match_info["id"])
        body = await request.json()
        cur = self.db.student(sid)
        if cur is None:
            return web.json_response({"error": "Ученик не найден"}, status=404)
        try:
            name, aliases = self._check_student(cur["group_id"], body.get("name", cur["name"]),
                                                body.get("aliases", cur["aliases"]), exclude_id=sid)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        self.db.q("UPDATE students SET name=?, aliases=? WHERE id=?", (name, json.dumps(aliases, ensure_ascii=False), sid))
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"ok": True})

    async def handle_student_delete(self, request: web.Request) -> web.Response:
        sid = int(request.match_info["id"])
        self.db.q("UPDATE received SET student_id=NULL WHERE student_id=?", (sid,))
        self.db.q("DELETE FROM students WHERE id=?", (sid,))
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"ok": True})

    async def handle_assign(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            r = await self.assign(int(request.match_info["id"]), int(body.get("student_id")),
                                  str(body.get("remember_alias", "")))
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(r)

    def trash_dir(self) -> Path:
        """Корзина Mac (для тестов можно подменить)."""
        return Path.home() / ".Trash"

    def delete_file(self, file_id: int) -> bool:
        """
        Убрать ненужный файл (мусор) в Корзину Mac. Запись остаётся со статусом deleted:
        учитель файл получил, поэтому «Конец урока» уберёт его и с ноутбука,
        а агент не будет присылать его снова.
        """
        f = self.db.one("SELECT * FROM received WHERE id=?", (file_id,))
        if f is None:
            return False
        trash = self.trash_dir()
        trash.mkdir(parents=True, exist_ok=True)
        for p in {f["project_path"], f["inbox_path"]}:
            if p and Path(p).exists():
                dest = unique_path(trash / Path(p).name)
                shutil.move(p, dest)
                self._remove_empty_dirs(Path(p).parent)
        self.db.q("UPDATE received SET status='deleted', project_path=NULL WHERE id=?", (file_id,))
        self.db.q("DELETE FROM disk_queue WHERE received_id=? AND status!='done'", (file_id,))
        return True

    async def handle_delete(self, request: web.Request) -> web.Response:
        ok = self.delete_file(int(request.match_info["id"]))
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"ok": ok})

    async def handle_delete_by_name(self, request: web.Request) -> web.Response:
        name = str((await request.json()).get("name", ""))
        rows = self.db.q("SELECT id FROM received WHERE status='unsorted' AND name=?", (name,))
        for r in rows:
            self.delete_file(r["id"])
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"deleted": len(rows)})

    def purge_ignored_unsorted(self) -> int:
        """Убрать из «Неразобранного» всё, что подходит под правила игнорирования (при запуске)."""
        patterns = BUILTIN_PATTERNS + self.server.storage.settings().get("ignore_patterns", [])
        n = 0
        for f in self.db.q("SELECT id, rel_path FROM received WHERE status='unsorted'"):
            if ignored_by_patterns(f["rel_path"], patterns):
                self.delete_file(f["id"])
                n += 1
        if n:
            log.info("Убрано служебных файлов из «Неразобранного»: %s", n)
        return n

    async def handle_ignore(self, request: web.Request) -> web.Response:
        """«Игнорировать такие файлы»: добавить шаблон и убрать уже пришедшие копии в Корзину."""
        pattern = clean_patterns([(await request.json()).get("pattern", "")])
        if not pattern:
            return web.json_response({"error": "Некорректный шаблон. Пример: *.dpapi или logs/"}, status=400)
        st = self.server.storage.settings()
        patterns = clean_patterns(st.get("ignore_patterns", []) + pattern)
        self.server.storage.update_settings(ignore_patterns=patterns)
        removed = 0
        for f in self.db.q("SELECT id, rel_path FROM received WHERE status='unsorted'"):
            if ignored_by_patterns(f["rel_path"], pattern):
                self.delete_file(f["id"])
                removed += 1
        for handler in self.server.on_settings_changed:   # отправить правило на ноутбуки
            await handler({"ignore_patterns": patterns})
        await self.server.broadcast_state()
        await self.server.broadcast_event({"kind": "projects_changed"})
        return web.json_response({"pattern": pattern[0], "removed": removed})

    async def handle_student_files(self, request: web.Request) -> web.Response:
        """Работы ученика (свежие сверху)."""
        rows = self.db.q("SELECT id, laptop, name, size, received_at, lesson_date, project_path FROM received"
                         " WHERE student_id=? AND status='sorted' ORDER BY received_at DESC LIMIT 500",
                         (int(request.match_info["id"]),))
        for r in rows:
            r["exists"] = bool(r["project_path"]) and Path(r["project_path"]).exists()
        return web.json_response({"files": rows})

    async def handle_open_file(self, request: web.Request) -> web.Response:
        """Открыть работу на Mac программой по умолчанию."""
        f = self.db.one("SELECT project_path, inbox_path FROM received WHERE id=?", (int(request.match_info["id"]),))
        if f is None:
            return web.json_response({"error": "Файл не найден"}, status=404)
        path = f["project_path"] if f["project_path"] and Path(f["project_path"]).exists() else f["inbox_path"]
        if not Path(path).exists():
            return web.json_response({"error": "Файл удалён с Mac"}, status=404)
        _open([path])
        return web.json_response({"ok": True})

    async def handle_reveal(self, request: web.Request) -> web.Response:
        """Показать файл в Finder."""
        f = self.db.one("SELECT project_path, inbox_path FROM received WHERE id=?", (int(request.match_info["id"]),))
        if f is None:
            return web.json_response({"error": "Файл не найден"}, status=404)
        path = f["project_path"] if f["project_path"] and Path(f["project_path"]).exists() else f["inbox_path"]
        _open(["-R", path])
        return web.json_response({"ok": True})

    async def handle_open_folder(self, request: web.Request) -> web.Response:
        body = await request.json()
        names = {"root": "", "projects": "Проекты", "unsorted": "Неразобранное", "inbox": "Входящие", "backups": "Резервные копии"}
        sub = names.get(body.get("which", "root"), "")
        path = self.server.projects_root() / sub
        path.mkdir(parents=True, exist_ok=True)
        _open([str(path)])
        return web.json_response({"ok": True})


def _open(args: list[str]) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", *args])
