"""
Сохранённое состояние агента после привязки: номер ноутбука, адрес учителя, токен.
Токен хранится зашифрованным через платформу (на Windows — DPAPI).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from agent.platform.base import Platform

log = logging.getLogger(__name__)


@dataclass
class AgentState:
    teacher_id: str
    laptop_number: str
    host: str
    port: int
    token: str
    # Последние адреса учителя (в разных сетях: роутер, точка доступа) — пробуем все
    known_hosts: list[tuple[str, int]] = field(default_factory=list)

    MAX_KNOWN = 6

    def remember(self, host: str, port: int) -> None:
        """Запомнить рабочий адрес учителя (самый свежий — первым)."""
        self.host, self.port = host, port
        self.known_hosts = [(host, port)] + [a for a in self.known_hosts if a != (host, port)]
        self.known_hosts = self.known_hosts[:self.MAX_KNOWN]

    @staticmethod
    def path(platform: Platform) -> Path:
        return platform.data_dir() / "state.json"

    def save(self, platform: Platform) -> None:
        path = self.path(platform)
        data = {
            "teacher_id": self.teacher_id,
            "laptop_number": self.laptop_number,
            "host": self.host,
            "port": self.port,
            "known_hosts": [[h, p] for h, p in self.known_hosts],
            "token_protected": base64.b64encode(platform.protect(self.token.encode())).decode(),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, platform: Platform) -> "AgentState | None":
        path = cls.path(platform)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            token = platform.unprotect(base64.b64decode(data["token_protected"])).decode()
            return cls(
                teacher_id=data["teacher_id"],
                laptop_number=str(data["laptop_number"]),
                host=data["host"],
                port=int(data["port"]),
                token=token,
                known_hosts=[(str(h), int(p)) for h, p in data.get("known_hosts", [])][:cls.MAX_KNOWN],
            )
        except Exception:
            # Например, файл скопировали с другого компьютера — DPAPI его не расшифрует
            log.exception("Не удалось прочитать состояние — нужна повторная привязка")
            return None

    @classmethod
    def clear(cls, platform: Platform) -> None:
        try:
            cls.path(platform).unlink()
        except FileNotFoundError:
            pass
