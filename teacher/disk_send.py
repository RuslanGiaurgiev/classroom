"""
Отправка работ урока на Яндекс Диск — в папки детей (по кнопке учителя).

Структура на Диске (как уже заведено в школе):
    <Папка школы>/<Имя Фамилия>/<Программа>/<Урок N — тема (ДД.ММ.ГГГГ)>/<файлы>

  • папку ребёнка ищем среди существующих по имени (порядок слов, ё/е, опечатки не важны);
    если подходит несколько — выбирает учитель; если нет ни одной — учитель подтверждает создание;
  • выбор запоминается (таблица disk_links) — второй раз не спрашиваем;
  • папку программы ищем и прямо в папке ребёнка, и внутри «Программирование»/«Робототехника»;
  • если у ребёнка уже есть папка «Урок N» с тем же номером — кладём в неё;
  • на Диске ничего не удаляется и не перезаписывается (см. yadisk.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import date as Date
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from teacher.inbox import safe_name
from teacher.names import _PATRONYMIC, _word_match, tokens
from teacher.yadisk import DiskAuthError

if TYPE_CHECKING:
    from teacher.server import TeacherServer

log = logging.getLogger(__name__)

DIRECTION_FOLDERS = ("Программирование", "Робототехника")
LESSON_RE = re.compile(r"^\s*урок\s*(\d{1,3})(?!\d)", re.IGNORECASE)
NAME_THRESHOLD = 85
MAX_TOPICS = 30


def _key(name: str) -> str:
    """Для сравнения названий программ: «Make Code» = «MakeCode» = «make-code»."""
    return re.sub(r"[^0-9a-zа-яё]", "", name.lower().replace("ё", "е"))


def name_words(name: str) -> list[str]:
    """Слова имени без отчества."""
    return [w for w in tokens(name) if not _PATRONYMIC.search(w)]


def folder_matches(student_name: str, folder: str, threshold: int = NAME_THRESHOLD) -> bool:
    """Папка «Иван Петров» подходит ученику «Петров Иван Сергеевич» (и наоборот, с опечатками)."""
    sw, fw = name_words(student_name)[:2], tokens(folder)
    if not sw or not fw:
        return False
    return all(any(_word_match(w, f, threshold) for f in fw) for w in sw)


def new_folder_name(student_name: str) -> str:
    """Имя новой папки: первые два слова без отчества — как записан ученик."""
    words = [w for w in student_name.split() if not _PATRONYMIC.search(w.lower())]
    return safe_name(" ".join(words[:2]) or student_name)


def lesson_folder_name(n: int, topic: str, day: str) -> str:
    d = Date.fromisoformat(day).strftime("%d.%m.%Y")
    topic = " ".join(topic.split())
    return safe_name(f"Урок {n} — {topic} ({d})" if topic else f"Урок {n} ({d})")


def lesson_numbers(items: list[dict]) -> dict[int, str]:
    """{номер: имя папки} для папок «Урок N…» (если с одним номером несколько — первая по имени)."""
    out: dict[int, str] = {}
    for i in sorted(items, key=lambda x: x["name"]):
        m = LESSON_RE.match(i["name"]) if i["type"] == "dir" else None
        if m and int(m.group(1)) not in out:
            out[int(m.group(1))] = i["name"]
    return out


class DiskSender:
    def __init__(self, server: "TeacherServer"):
        self.server = server
        self.db = server.db
        self.disk = server.disk

    def routes(self) -> list:
        return [
            web.get("/ui/api/disk/plan", self.handle_plan),
            web.post("/ui/api/disk/send", self.handle_send),
        ]

    # ================= Что и куда =================

    def _files(self, group_id: int, day: str) -> dict[int, list[dict]]:
        rows = self.db.q(
            "SELECT r.id, r.name, r.project_path, r.student_id,"
            " EXISTS(SELECT 1 FROM disk_queue q WHERE q.received_id=r.id AND q.status IN ('done','pending')) AS sent"
            " FROM received r JOIN students s ON s.id=r.student_id"
            " WHERE r.status='sorted' AND s.group_id=? AND r.lesson_date=? ORDER BY r.name COLLATE NOCASE",
            (group_id, day))
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(r["student_id"], []).append(
                {"id": r["id"], "name": Path(r["project_path"]).name if r["project_path"] else r["name"],
                 "sent": bool(r["sent"])})
        return out

    def _link(self, student_id: int) -> str | None:
        r = self.db.one("SELECT folder FROM disk_links WHERE student_id=?", (student_id,))
        return r["folder"] if r else None

    async def _program_place(self, s, kid_path: str, kid_items: list[dict], program: str) -> tuple[str, list[dict]]:
        """Где у ребёнка папка программы: (путь относительно папки ребёнка, её содержимое)."""
        want = _key(program)
        for i in kid_items:
            if i["type"] == "dir" and _key(i["name"]) == want:
                return i["name"], await self.disk.list_dir(s, f"{kid_path}/{i['name']}") or []
        for i in kid_items:
            if i["type"] == "dir" and i["name"] in DIRECTION_FOLDERS:
                inner = await self.disk.list_dir(s, f"{kid_path}/{i['name']}") or []
                for j in inner:
                    if j["type"] == "dir" and _key(j["name"]) == want:
                        rel = f"{i['name']}/{j['name']}"
                        return rel, await self.disk.list_dir(s, f"{kid_path}/{rel}") or []
        return safe_name(program), []    # нет — создадим прямо в папке ребёнка

    async def build_plan(self, group_id: int, day: str, program: str) -> dict:
        root = self.disk.root()
        if not root:
            raise ValueError("Укажите папку на Диске («Настройки» → «Яндекс Диск»), например: Марьино/2026-2027")
        group = self.db.one("SELECT * FROM groups WHERE id=?", (group_id,))
        if group is None:
            raise ValueError("Группа не найдена")
        files = self._files(group_id, day)
        students = {s["id"]: s for s in self.db.students(group_id)}
        sem = asyncio.Semaphore(6)
        async with self.disk.session() as s:
            top = await self.disk.list_dir(s, root)
            if top is None:
                raise ValueError(f"На Диске нет папки «{root}» — проверьте название в настройках")
            kid_folders = sorted(i["name"] for i in top if i["type"] == "dir")
            programs: Counter = Counter()

            async def one(sid: int) -> dict:
                st = students[sid]
                item = {"student_id": sid, "name": st["name"], "files": files[sid], "status": "missing",
                        "folder": "", "options": [], "new_name": new_folder_name(st["name"]),
                        "program_path": "", "lessons": {}}
                linked = self._link(sid)
                if linked and linked in kid_folders:
                    item.update(status="found", folder=linked)
                else:
                    opts = [f for f in kid_folders if folder_matches(st["name"], f)]
                    if len(opts) == 1:
                        item.update(status="found", folder=opts[0])
                    elif opts:
                        item.update(status="ambiguous", options=opts)
                if item["folder"]:
                    async with sem:
                        kid_path = f"{root}/{item['folder']}"
                        kid_items = await self.disk.list_dir(s, kid_path) or []
                        for i in kid_items:
                            if i["type"] != "dir":
                                continue
                            if i["name"] not in DIRECTION_FOLDERS:
                                programs[i["name"]] += 1
                            else:   # программы внутри «Программирование»/«Робототехника»
                                for j in await self.disk.list_dir(s, f"{kid_path}/{i['name']}") or []:
                                    if j["type"] == "dir":
                                        programs[j["name"]] += 1
                        if program:
                            rel, inside = await self._program_place(s, kid_path, kid_items, program)
                            item["program_path"] = rel
                            item["lessons"] = {str(k): v for k, v in lesson_numbers(inside).items()}
                elif program:
                    item["program_path"] = safe_name(program)
                return item

            result = await asyncio.gather(*(one(sid) for sid in files if sid in students))
        result.sort(key=lambda x: x["name"].lower())
        # Номер урока: следующий за самым частым последним уроком в группе
        last = Counter(max(map(int, r["lessons"])) for r in result if r["lessons"])
        suggest = last.most_common(1)[0][0] + 1 if last else 1
        st = self.server.storage.settings()
        prefs = (st.get("disk_group_prefs") or {}).get(str(group_id), {})
        known = list(dict.fromkeys([*(n for n, _ in programs.most_common()), *(st.get("disk_known_programs") or []),
                                    *(st.get("disk_programs") or [])]))
        if programs:
            seen = list(dict.fromkeys([*(n for n, _ in programs.most_common()), *(st.get("disk_known_programs") or [])]))
            self.server.storage.update_settings(disk_known_programs=seen[:50])
        program = self.canonical_program(program, known)
        found = sum(1 for r in result if r["status"] == "found")
        return {"root": root, "date": day, "group": {"id": group_id, "name": group["name"]},
                "program": program, "programs": known, "group_program": prefs.get("program", ""),
                "lesson_suggest": suggest, "topics": st.get("disk_topics") or [], "students": result,
                "folders": kid_folders,
                # Почти никого не нашли — скорее всего, в настройках не та папка
                "root_suspicious": len(result) >= 2 and found * 2 < len(result)}

    # ================= Отправка =================

    def canonical_program(self, program: str, known: list[str] | None = None) -> str:
        """«TinkerCad» → «Tinkercad», если у детей на Диске программа уже так называется."""
        if not program:
            return program
        st = self.server.storage.settings()
        known = known if known is not None else [*(st.get("disk_known_programs") or []), *(st.get("disk_programs") or [])]
        return next((k for k in known if _key(k) == _key(program)), program)

    async def send(self, group_id: int, day: str, program: str, lesson: int, topic: str, choices: list[dict]) -> dict:
        root = self.disk.root()
        program = safe_name(self.canonical_program(" ".join(program.split())))
        topic = " ".join(topic.split())[:100]
        files = self._files(group_id, day)
        students = {s["id"]: s for s in self.db.students(group_id)}
        queued = skipped = 0
        created: list[str] = []
        async with self.disk.session() as s:
            top = await self.disk.list_dir(s, root)
            if top is None:
                raise ValueError(f"На Диске нет папки «{root}»")
            existing = {i["name"] for i in top if i["type"] == "dir"}
            # Сначала проверяем всех: если что-то не так — ничего не отправляем
            plan: list[tuple[int, str]] = []
            for c in choices:
                sid = c.get("student_id")
                if sid not in students or sid not in files:
                    continue
                folder = safe_name(" ".join(str(c.get("folder") or "").split()))
                if not folder or folder == "файл":
                    continue
                if folder not in existing:
                    if not c.get("create"):
                        raise ValueError(f"Папки «{folder}» на Диске нет — подтвердите её создание")
                    created.append(folder)
                plan.append((sid, folder))
            for sid, folder in plan:
                self.db.q("INSERT OR REPLACE INTO disk_links(student_id, folder) VALUES (?,?)", (sid, folder))
                kid_path = f"{root}/{folder}"
                kid_items = (await self.disk.list_dir(s, kid_path) or []) if folder in existing else []
                rel, inside = await self._program_place(s, kid_path, kid_items, program)
                lesson_dir = lesson_numbers(inside).get(lesson) or lesson_folder_name(lesson, topic, day)
                for f in files[sid]:
                    if f["sent"]:
                        skipped += 1
                        continue
                    remote = f"{kid_path}/{rel}/{lesson_dir}/{safe_name(f['name'])}"
                    if self.disk.enqueue_remote(f["id"], remote):
                        queued += 1
                    else:
                        skipped += 1
        # Запоминаем выбор группы и тему (подсказки в следующий раз)
        st = self.server.storage.settings()
        prefs = dict(st.get("disk_group_prefs") or {})
        prefs[str(group_id)] = {"program": program}
        programs = list(dict.fromkeys([program, *(st.get("disk_programs") or [])]))[:30]
        topics = list(dict.fromkeys([t for t in [topic, *(st.get("disk_topics") or [])] if t]))[:MAX_TOPICS]
        self.server.storage.update_settings(disk_group_prefs=prefs, disk_programs=programs, disk_topics=topics)
        log.info("На Диск: группа %s, %s, урок %s — в очереди %s, пропущено %s, новых папок %s",
                 group_id, program, lesson, queued, skipped, len(created))
        self.disk.wake()
        await self.server.broadcast_event({"kind": "disk_changed"})
        return {"queued": queued, "skipped": skipped, "created": created}

    # ================= API панели =================

    @staticmethod
    def _day(value) -> str:
        day = str(value or "") or Date.today().isoformat()
        try:
            Date.fromisoformat(day)
        except ValueError as e:
            raise ValueError("Некорректная дата") from e
        return day

    async def _guard(self, coro):
        try:
            return web.json_response(await coro)
        except DiskAuthError as e:
            return web.json_response({"error": str(e)}, status=401)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
            return web.json_response({"error": f"Нет связи с Яндекс Диском: {e}"}, status=502)

    async def handle_plan(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            gid, day = int(q.get("group_id", "")), self._day(q.get("date"))
        except ValueError:
            return web.json_response({"error": "Некорректная группа или дата"}, status=400)
        return await self._guard(self.build_plan(gid, day, " ".join(q.get("program", "").split())[:80]))

    async def handle_send(self, request: web.Request) -> web.Response:
        b = await request.json()
        try:
            gid, day, lesson = int(b.get("group_id")), self._day(b.get("date")), int(b.get("lesson"))
        except (TypeError, ValueError):
            return web.json_response({"error": "Некорректная группа, дата или номер урока"}, status=400)
        program = " ".join(str(b.get("program") or "").split())[:80]
        if not program:
            return web.json_response({"error": "Выберите программу (например, Make Code)"}, status=400)
        if not 1 <= lesson <= 300:
            return web.json_response({"error": "Номер урока: от 1 до 300"}, status=400)
        choices = b.get("students") if isinstance(b.get("students"), list) else []
        return await self._guard(self.send(gid, day, program, lesson, str(b.get("topic") or ""),
                                           [c for c in choices if isinstance(c, dict)]))
