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

    def to_dict(self, platform: Platform) -> dict:
        return {
            "teacher_id": self.teacher_id,
            "laptop_number": self.laptop_number,
            "host": self.host,
            "port": self.port,
            "known_hosts": [[h, p] for h, p in self.known_hosts],
            "token_protected": base64.b64encode(platform.protect(self.token.encode())).decode(),
        }

    @classmethod
    def from_dict(cls, platform: Platform, data: dict) -> "AgentState":
        token = platform.unprotect(base64.b64decode(data["token_protected"])).decode()
        return cls(
            teacher_id=data["teacher_id"],
            laptop_number=str(data["laptop_number"]),
            host=data["host"],
            port=int(data["port"]),
            token=token,
            known_hosts=[(str(h), int(p)) for h, p in data.get("known_hosts", [])][:cls.MAX_KNOWN],
        )

    def save(self, platform: Platform) -> None:
        # Совместимость со старыми версиями: одиночный state.json И список teachers.json
        _atomic_json(self.path(platform), self.to_dict(platform))
        AgentConfig.load(platform).add(self).save(platform)

    @classmethod
    def load(cls, platform: Platform) -> "AgentState | None":
        path = cls.path(platform)
        if not path.exists():
            return None
        try:
            return cls.from_dict(platform, json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            # Например, файл скопировали с другого компьютера — DPAPI его не расшифрует
            log.exception("Не удалось прочитать состояние — нужна повторная привязка")
            return None

    @classmethod
    def clear(cls, platform: Platform) -> None:
        for p in (cls.path(platform), AgentConfig.path(platform)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def _atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class AgentConfig:
    """Все учителя, к которым привязан этот ноутбук (может быть несколько — например, два препода)."""
    accounts: list[AgentState] = field(default_factory=list)

    @staticmethod
    def path(platform: Platform) -> Path:
        return platform.data_dir() / "teachers.json"

    def find(self, teacher_id: str) -> "AgentState | None":
        return next((a for a in self.accounts if a.teacher_id == teacher_id), None)

    def add(self, account: AgentState) -> "AgentConfig":
        """Добавить учителя (или обновить, если такой teacher_id уже есть)."""
        self.accounts = [a for a in self.accounts if a.teacher_id != account.teacher_id] + [account]
        return self

    def remove(self, teacher_id: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.teacher_id != teacher_id]
        return len(self.accounts) != before

    def save(self, platform: Platform) -> None:
        data = {"accounts": [a.to_dict(platform) for a in self.accounts]}
        _atomic_json(self.path(platform), data)

    @classmethod
    def load(cls, platform: Platform) -> "AgentConfig":
        path = cls.path(platform)
        if path.exists():
            accounts = []
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for item in data.get("accounts", []):
                    try:
                        accounts.append(AgentState.from_dict(platform, item))
                    except Exception:  # noqa: BLE001 — один повреждённый аккаунт не теряет остальные
                        log.exception("Пропущен повреждённый учитель в teachers.json")
                return cls(accounts)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось прочитать teachers.json")
                return cls([])
        # Переход со старой версии: один учитель из state.json
        old = AgentState.load(platform)
        return cls([old] if old else [])
