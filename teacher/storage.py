"""
Хранение данных учителя: настройки и список привязанных ноутбуков.

Папка данных: ~/Library/Application Support/ClassroomTeacher
(для тестов можно переопределить переменной окружения CLASSROOM_TEACHER_DATA).

Токены ноутбуков храним только в виде SHA-256 хэша: даже если файл
кто-то прочитает, подключиться от имени ноутбука он не сможет.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from shared.protocol import DEFAULT_PORT

# Настройки по умолчанию (учитель меняет их в панели)
DEFAULT_SETTINGS: dict[str, Any] = {
    "search_engine": "yandex",
    # Программы, которые можно закрывать кнопкой «Закрыть программу»
    "apps": [
        {"title": "Google Chrome", "process": "chrome.exe"},
        {"title": "Яндекс Браузер", "process": "browser.exe"},
        {"title": "Microsoft Edge", "process": "msedge.exe"},
        {"title": "Scratch", "process": "Scratch 3.exe"},
    ],
    # Наборы вкладок урока: {"id", "name", "items": [адреса или запросы], "browser"}
    "lesson_sets": [],
    # Сбор проектов: какие папки на ноутбуках отслеживать и лимит размера файла
    "watch_folders": [{"base": "downloads", "sub": ""}, {"base": "desktop", "sub": "Проекты"}],
    "max_file_mb": 500,
    # Куда складывать проекты на Mac ("" — ~/Documents/Компьютерный класс)
    "projects_root": "",
    # Сортировка: текущая группа (id или None — искать во всех) и порог похожести имён
    "active_group": None,
    "name_threshold": 85,
    # Яндекс Диск: корневая папка школы и ClientID приложения (токен — в Связке ключей)
    "disk_root": "Компьютерный класс",
    "disk_client_id": "",
    # Файлы, которые учитель велел игнорировать («*.dpapi», «logs/»)
    "ignore_patterns": [],
}


def default_data_dir() -> Path:
    env = os.environ.get("CLASSROOM_TEACHER_DATA")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "ClassroomTeacher"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, data: Any) -> None:
    """Пишем во временный файл и переименовываем — файл никогда не окажется «наполовину записан»."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Storage:
    """Потокобезопасное хранилище в JSON-файле state.json."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "state.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        data.setdefault("teacher_id", uuid.uuid4().hex[:12])
        data.setdefault("settings", {})
        data["settings"].setdefault("port", DEFAULT_PORT)
        for k, v in DEFAULT_SETTINGS.items():
            data["settings"].setdefault(k, json.loads(json.dumps(v)))
        data.setdefault("laptops", {})  # номер (строка) -> данные
        if not self.path.exists():
            atomic_write_json(self.path, data)
        return data

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self._data)

    # ---------- Общие свойства ----------

    @property
    def teacher_id(self) -> str:
        return self._data["teacher_id"]

    @property
    def port(self) -> int:
        return int(self._data["settings"]["port"])

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data["settings"]))

    def update_settings(self, **values: Any) -> None:
        with self._lock:
            self._data["settings"].update(values)
            self.save()

    def get_ui_token(self) -> str:
        """
        Токен для веб-панели (окна приложения). Хранится в файле с правами 600:
        им пользуется само приложение и фейковый агент для автопривязки.
        """
        path = self.data_dir / "ui_token"
        if path.exists():
            return path.read_text().strip()
        token = secrets.token_urlsafe(24)
        path.write_text(token)
        os.chmod(path, 0o600)
        return token

    # ---------- Ноутбуки ----------

    def laptops(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._data["laptops"]))  # копия

    def add_laptop(self, number: str, token: str) -> None:
        """Привязать ноутбук (повторная привязка того же номера заменяет старый токен)."""
        with self._lock:
            old = self._data["laptops"].get(number, {})
            self._data["laptops"][number] = {
                "token_hash": hash_token(token),
                "paired_at": time.time(),
                "username": old.get("username", ""),
                "hostname": old.get("hostname", ""),
                "last_seen": None,
            }
            self.save()

    def remove_laptop(self, number: str) -> bool:
        with self._lock:
            existed = self._data["laptops"].pop(number, None) is not None
            if existed:
                self.save()
            return existed

    def find_laptop_by_token(self, token: str) -> str | None:
        """Вернуть номер ноутбука по токену или None. Сравнение — постоянного времени."""
        h = hash_token(token)
        with self._lock:
            for number, info in self._data["laptops"].items():
                if secrets.compare_digest(info["token_hash"], h):
                    return number
        return None

    def update_laptop_info(self, number: str, **fields: Any) -> None:
        with self._lock:
            info = self._data["laptops"].get(number)
            if info is None:
                return
            info.update(fields)
            self.save()
