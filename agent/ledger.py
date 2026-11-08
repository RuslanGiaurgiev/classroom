"""
Учёт файлов на ноутбуке (SQLite): что найдено, что отправлено, что подтверждено.

Статусы:
  baseline  — лежал в папке до установки агента: не отправляем и не удаляем
  pending   — ждёт отправки
  confirmed — учитель получил и проверил SHA-256
  gone      — файл исчез до отправки (ребёнок удалил / переименовал)
  superseded— файл изменился до отправки — отправим новую версию
  trashed   — после «Конца урока» перемещён в корзину агента

Главное правило безопасности: удалять (в корзину) можно ТОЛЬКО confirmed.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    folder_key TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL,
    first_seen REAL NOT NULL,
    updated REAL NOT NULL,
    note TEXT DEFAULT '',
    UNIQUE (folder_key, rel_path, size, mtime_ns)
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class FileRow:
    id: int
    folder_key: str
    rel_path: str
    size: int
    mtime_ns: int
    sha256: str | None
    status: str
    note: str = ""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---------- meta ----------

    def get_meta(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._q("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # ---------- файлы ----------

    def is_known(self, folder_key: str, rel_path: str, size: int, mtime_ns: int) -> bool:
        """Эту версию файла (путь+размер+время) уже видели — не надо считать хэш снова."""
        return bool(self._q(
            "SELECT 1 FROM files WHERE folder_key=? AND rel_path=? AND size=? AND mtime_ns=?",
            (folder_key, rel_path, size, mtime_ns)))

    def add_baseline(self, folder_key: str, rel_path: str, size: int, mtime_ns: int) -> None:
        now = time.time()
        self._q("INSERT OR IGNORE INTO files(folder_key, rel_path, size, mtime_ns, sha256, status, first_seen, updated)"
                " VALUES (?,?,?,?,NULL,'baseline',?,?)", (folder_key, rel_path, size, mtime_ns, now, now))

    def add_found(self, folder_key: str, rel_path: str, size: int, mtime_ns: int, sha256: str) -> str:
        """
        Новый готовый файл. Возвращает итоговый статус:
          pending   — надо отправить;
          confirmed — точно такой же файл (тот же SHA-256) учитель уже получил — повторно не шлём;
          known     — эта версия уже записана.
        """
        if self.is_known(folder_key, rel_path, size, mtime_ns):
            return "known"
        dup = self._q("SELECT 1 FROM files WHERE sha256=? AND status IN ('confirmed','trashed') LIMIT 1", (sha256,))
        status = "confirmed" if dup else "pending"
        now = time.time()
        self._q("INSERT OR IGNORE INTO files(folder_key, rel_path, size, mtime_ns, sha256, status, first_seen, updated, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (folder_key, rel_path, size, mtime_ns, sha256, status, now, now, "дубликат" if dup else ""))
        return status

    def recent_versions(self, folder_key: str, rel_path: str, seconds: float) -> int:
        """Сколько версий этого файла нашли за последние seconds секунд (для «шумных» файлов)."""
        return self._q("SELECT COUNT(*) FROM files WHERE folder_key=? AND rel_path=? AND first_seen>? AND status!='baseline'",
                       (folder_key, rel_path, time.time() - seconds))[0][0]

    def set_status(self, row_id: int, status: str, note: str = "") -> None:
        self._q("UPDATE files SET status=?, note=?, updated=? WHERE id=?", (status, note, time.time(), row_id))

    def mark_confirmed_by_sha(self, sha256: str) -> None:
        """Все ожидающие копии этого содержимого считаются полученными."""
        self._q("UPDATE files SET status='confirmed', updated=? WHERE sha256=? AND status='pending'", (time.time(), sha256))

    def _rows(self, where: str, args: tuple = ()) -> list[FileRow]:
        rows = self._q(f"SELECT id, folder_key, rel_path, size, mtime_ns, sha256, status, note FROM files WHERE {where} ORDER BY id", args)
        return [FileRow(*r) for r in rows]

    def pending(self) -> list[FileRow]:
        return self._rows("status='pending'")

    def pending_count(self) -> int:
        return self._q("SELECT COUNT(*) FROM files WHERE status='pending'")[0][0]

    def confirmed(self) -> list[FileRow]:
        return self._rows("status='confirmed'")

    def get(self, row_id: int) -> FileRow | None:
        r = self._rows("id=?", (row_id,))
        return r[0] if r else None
