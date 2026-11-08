"""
Материалы урока: общие проверки для учителя и агента.

Учитель собирает «пакет» (файлы и папки), ноутбуки скачивают его с Mac.
Пути внутри пакета — относительные, со слешами «/». Агент обязан проверить,
что путь не выходит за папку материалов и допустим в Windows.
"""

from __future__ import annotations

import re

MAX_PACKAGE_FILES = 50_000
MAX_PACKAGE_BYTES = 50 * 1024 ** 3
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Имена, запрещённые в Windows
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f\\]')


class UnsafePath(ValueError):
    pass


def safe_rel_path(rel: str) -> str:
    """
    Проверить относительный путь из пакета. Возвращает нормализованный путь
    («Урок 5/картинки/кот.png») или бросает UnsafePath.
    """
    if not isinstance(rel, str) or not rel or len(rel) > 400:
        raise UnsafePath("пустой или слишком длинный путь")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise UnsafePath(f"абсолютный путь: {rel}")
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", ".."):
            raise UnsafePath(f"недопустимая часть пути: {rel}")
        if _BAD_CHARS.search(p) or p != p.rstrip(" ."):
            raise UnsafePath(f"недопустимые символы: {p}")
        if p.split(".")[0].lower() in _RESERVED:
            raise UnsafePath(f"имя зарезервировано в Windows: {p}")
        if len(p) > 150:
            raise UnsafePath(f"слишком длинное имя: {p}")
    return "/".join(parts)
