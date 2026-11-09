"""
База данных учителя (SQLite): полученные файлы, группы и ученики, очередь Яндекс Диска.
Файл: ~/Library/Application Support/ClassroomTeacher/teacher.sqlite3
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS received (
    id INTEGER PRIMARY KEY,
    laptop TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    name TEXT NOT NULL,           -- имя файла на ноутбуке
    rel_path TEXT NOT NULL,       -- путь внутри отслеживаемой папки
    folder_key TEXT NOT NULL,
    received_at REAL NOT NULL,
    lesson_date TEXT NOT NULL,    -- ГГГГ-ММ-ДД
    inbox_path TEXT NOT NULL,     -- копия во «Входящих»
    status TEXT NOT NULL DEFAULT 'new',   -- new | sorted | unsorted
    student_id INTEGER,
    project_path TEXT,            -- где лежит в «Проектах» или «Неразобранном»
    UNIQUE (laptop, sha256)
);
CREATE INDEX IF NOT EXISTS received_status ON received(status);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]'    -- JSON-список вариантов написания
);

CREATE TABLE IF NOT EXISTS disk_queue (
    id INTEGER PRIMARY KEY,
    received_id INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | error
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT '',
    updated REAL NOT NULL,
    UNIQUE (received_id, remote_path)
);

-- Какая папка на Яндекс Диске принадлежит ученику (учитель подтвердил один раз)
CREATE TABLE IF NOT EXISTS disk_links (
    student_id INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
    folder TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def insert(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).lastrowid

    # ---------- Полученные файлы ----------

    def find_received(self, laptop: str, sha256: str) -> dict | None:
        return self.one("SELECT * FROM received WHERE laptop=? AND sha256=?", (laptop, sha256))

    def add_received(self, **f: Any) -> int:
        return self.insert(
            "INSERT INTO received(laptop, sha256, size, name, rel_path, folder_key, received_at, lesson_date, inbox_path)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f["laptop"], f["sha256"], f["size"], f["name"], f["rel_path"], f["folder_key"],
             f.get("received_at", time.time()), f["lesson_date"], f["inbox_path"]))

    def confirmed_shas(self, laptop: str) -> list[str]:
        return [r["sha256"] for r in self.q("SELECT sha256 FROM received WHERE laptop=?", (laptop,))]

    # ---------- Группы и ученики ----------

    def groups(self, today: str | None = None) -> list[dict]:
        """Группы с учениками; у каждого ученика — сколько работ всего и сегодня."""
        groups = self.q("SELECT * FROM groups ORDER BY name COLLATE NOCASE")
        students = self.q("SELECT * FROM students ORDER BY name COLLATE NOCASE")
        counts = {r["student_id"]: r for r in self.q(
            "SELECT student_id, COUNT(*) AS total, SUM(lesson_date=?) AS today FROM received"
            " WHERE status='sorted' AND student_id IS NOT NULL GROUP BY student_id", (today or "",))}
        for g in groups:
            g["students"] = [{**s, "aliases": json.loads(s["aliases"]),
                              "files_total": (counts.get(s["id"]) or {}).get("total", 0),
                              "files_today": (counts.get(s["id"]) or {}).get("today", 0) or 0}
                             for s in students if s["group_id"] == g["id"]]
        return groups

    def students(self, group_id: int | None = None) -> list[dict]:
        sql = "SELECT s.*, g.name AS group_name FROM students s JOIN groups g ON g.id = s.group_id"
        rows = self.q(sql + (" WHERE s.group_id=?" if group_id else ""), (group_id,) if group_id else ())
        return [{**r, "aliases": json.loads(r["aliases"])} for r in rows]

    def student(self, student_id: int) -> dict | None:
        r = self.one("SELECT s.*, g.name AS group_name FROM students s JOIN groups g ON g.id = s.group_id WHERE s.id=?",
                     (student_id,))
        return {**r, "aliases": json.loads(r["aliases"])} if r else None
