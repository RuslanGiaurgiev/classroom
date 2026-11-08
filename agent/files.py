"""
Правила для файлов проектов: что игнорировать, когда файл «готов», SHA-256.
Чистая логика без Windows — полностью тестируется на Mac.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from shared.ignore import BUILTIN_PATTERNS, ignored_by_patterns

NOISY_VERSIONS = 3          # столько версий одного файла за NOISY_WINDOW секунд → файл «шумный»
NOISY_WINDOW = 600.0
NOISY_STABLE_SECONDS = 120  # «шумный» файл отправляем, только когда он не меняется 2 минуты

STABLE_SECONDS = 3.0      # размер не меняется столько секунд → файл готов
HASH_CHUNK = 1024 * 1024


def should_ignore(rel_path: Path, size: int, max_bytes: int, is_hidden: Callable[[Path], bool], full_path: Path,
                  extra_patterns: list[str] | None = None) -> str | None:
    """Причина игнорирования или None, если файл надо отправить.
    extra_patterns — шаблоны, добавленные учителем («Игнорировать такие файлы»)."""
    hit = ignored_by_patterns(rel_path.as_posix(), BUILTIN_PATTERNS + (extra_patterns or []))
    if hit:
        return f"служебный файл ({hit})"
    if any(part.startswith(".") for part in rel_path.parts):
        return "скрытый файл"
    if is_hidden(full_path):
        return "скрытый файл"
    if size == 0:
        return "пустой файл"
    if size > max_bytes:
        return f"больше {max_bytes // (1024 * 1024)} МБ"
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def can_open_for_read(path: Path) -> bool:
    """
    На Windows файл, который ещё пишет другая программа (браузер, редактор),
    обычно нельзя открыть — это второй признак «не готов».
    """
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


@dataclass
class _Observation:
    size: int
    mtime_ns: int
    stable_since: float


class ReadinessTracker:
    """
    Следит за файлами-кандидатами. Файл готов, когда его размер и время изменения
    не меняются STABLE_SECONDS секунд и он открывается на чтение.
    """

    def __init__(self, stable_seconds: float = STABLE_SECONDS, clock=time.monotonic,
                 can_open: Callable[[Path], bool] = can_open_for_read):
        self.stable_seconds = stable_seconds
        self.clock = clock
        self.can_open = can_open
        self._obs: dict[Path, _Observation] = {}

    def __len__(self) -> int:
        return len(self._obs)

    def __contains__(self, path: Path) -> bool:
        return path in self._obs

    def candidates(self) -> list[Path]:
        return list(self._obs)

    def observe(self, path: Path, size: int, mtime_ns: int) -> None:
        """Записать текущий размер/время файла. Любое изменение сбрасывает отсчёт."""
        now = self.clock()
        old = self._obs.get(path)
        if old is None or old.size != size or old.mtime_ns != mtime_ns:
            self._obs[path] = _Observation(size, mtime_ns, now)

    def forget(self, path: Path) -> None:
        self._obs.pop(path, None)

    def is_ready(self, path: Path, stable_seconds: float | None = None) -> bool:
        o = self._obs.get(path)
        if o is None:
            return False
        need = self.stable_seconds if stable_seconds is None else stable_seconds
        if self.clock() - o.stable_since < need:
            return False
        return self.can_open(path)
