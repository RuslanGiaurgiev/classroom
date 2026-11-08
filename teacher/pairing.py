"""
Одноразовые коды привязки ноутбуков.

Учитель нажимает «Добавить ноутбук», вводит номер с наклейки →
получает код, который действует 10 минут и срабатывает один раз.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from shared.pairing import PairingCode, generate_secret, normalize_secret

CODE_TTL_SECONDS = 10 * 60

# Защита от перебора: не больше стольких неудачных попыток в минуту
MAX_FAILED_PER_MINUTE = 20


@dataclass
class PendingCode:
    number: str
    secret: str
    expires_at: float


class PairingManager:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._codes: dict[str, PendingCode] = {}  # secret -> код
        self._failures: list[float] = []

    def create(self, number: str, host: str, port: int) -> tuple[PairingCode, float]:
        """Создать код для ноутбука с номером number. Старый код того же номера аннулируется."""
        self._cleanup()
        for s, c in list(self._codes.items()):
            if c.number == number:
                del self._codes[s]
        secret = generate_secret()
        while secret in self._codes:
            secret = generate_secret()
        expires = self._clock() + CODE_TTL_SECONDS
        self._codes[secret] = PendingCode(number=number, secret=secret, expires_at=expires)
        return PairingCode(host=host, port=port, secret=secret), expires

    def redeem(self, secret: str) -> str | None:
        """
        Погасить код. Возвращает номер ноутбука или None, если код неверный,
        просрочен или уже использован.
        """
        self._cleanup()
        if self.is_rate_limited():
            return None
        secret = normalize_secret(secret)
        found = None
        for s in self._codes:
            if secrets.compare_digest(s, secret):
                found = s
        if found is None:
            self._failures.append(self._clock())
            return None
        return self._codes.pop(found).number

    def is_rate_limited(self) -> bool:
        now = self._clock()
        self._failures = [t for t in self._failures if now - t < 60]
        return len(self._failures) >= MAX_FAILED_PER_MINUTE

    def _cleanup(self) -> None:
        now = self._clock()
        for s, c in list(self._codes.items()):
            if c.expires_at <= now:
                del self._codes[s]
