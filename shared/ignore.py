"""
Какие файлы НЕ являются проектами учеников. Общие правила для агента и учителя.

Шаблоны:
  "*.log"   — по имени файла (регистр не важен)
  "logs/"   — всё, что лежит в папке с таким именем (на любой глубине)
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

# Встроенные правила (всегда действуют)
BUILTIN_PATTERNS = [
    # Файл ещё скачивается
    "*.crdownload", "*.part", "*.partial", "*.download", "*.opdownload",
    # Временные файлы Office и программ
    "*.tmp", "~$*", "desktop.ini", "thumbs.db", ".ds_store",
    # Рабочие файлы программ (журналы, блокировки, базы)
    "*.log", "*.dpapi", "*.lock", "*.lck", "*-journal", "*.wal", "*.shm", "*.ldb", "*.etl",
    "*.pid", "*.dmp",
    # Служебные папки программ
    "logs/", "log/", "cache/", "caches/", "temp/", "tmp/", "crashpad/", "crash reports/", "gpucache/",
    "code cache/", "node_modules/", "__pycache__/", ".git/",
]
# Сам агент (его скачивают в «Загрузки», чтобы установить) — не работа ученика.
# Иначе он уходит учителю, а «Конец урока» убирает запущенный agent.exe в корзину.
AGENT_PATTERNS = ["agent.exe", "agent (*).exe", "agent(*).exe"]
BUILTIN_PATTERNS += AGENT_PATTERNS

MAX_USER_PATTERNS = 100


def clean_patterns(patterns) -> list[str]:
    """Проверить шаблоны учителя: строки до 100 символов, без путей вида «C:\\»."""
    out: list[str] = []
    for p in patterns or []:
        if not isinstance(p, str):
            continue
        p = p.strip().lower().replace("\\", "/")
        if not p or len(p) > 100 or ":" in p or p in out:
            continue
        if "/" in p.rstrip("/"):
            continue  # только имя файла или имя папки
        out.append(p)
    return out[:MAX_USER_PATTERNS]


def ignored_by_patterns(rel_path: str, patterns: list[str]) -> str | None:
    """Шаблон, под который попал файл (rel_path — путь внутри отслеживаемой папки), или None."""
    parts = PurePosixPath(rel_path.replace("\\", "/").lower()).parts
    if not parts:
        return None
    name, dirs = parts[-1], parts[:-1]
    for p in patterns:
        if p.endswith("/"):
            d = p[:-1]
            if any(fnmatch.fnmatchcase(x, d) for x in dirs):
                return p
        elif fnmatch.fnmatchcase(name, p):
            return p
    return None


def pattern_for(filename: str) -> str:
    """Какой шаблон предложить для «Игнорировать такие файлы»: по расширению, иначе по имени."""
    name = filename.lower()
    if "." in name.strip(".") and len(name.rsplit(".", 1)[1]) <= 10:
        return "*." + name.rsplit(".", 1)[1]
    return name
