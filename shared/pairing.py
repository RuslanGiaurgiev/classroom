"""
Код привязки ноутбука.

Формат (вводится руками учителем на ноутбуке):
    192.168.1.10:8765-K7M2QX9P

Слева — адрес Mac (запасной вариант, если mDNS не сработает),
справа — одноразовый секрет из 8 символов. Алфавит без похожих
символов (нет 0/O, 1/I/L), регистр не важен.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from dataclasses import dataclass

# Алфавит без путающихся символов (Crockford-подобный)
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
SECRET_LEN = 8

_CODE_RE = re.compile(r"^\s*([0-9.]+):(\d{1,5})\s*-\s*([A-Za-z0-9]+)\s*$")


@dataclass(frozen=True)
class PairingCode:
    host: str
    port: int
    secret: str

    def __str__(self) -> str:
        return f"{self.host}:{self.port}-{self.secret}"


def generate_secret() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(SECRET_LEN))


def normalize_secret(secret: str) -> str:
    """Регистр и пробелы не важны."""
    return secret.strip().upper().replace("-", "").replace(" ", "")


def parse_code(text: str) -> PairingCode:
    m = _CODE_RE.match(text or "")
    if not m:
        raise ValueError("Код должен выглядеть так: 192.168.1.10:8765-K7M2QX9P")
    host, port_s, secret = m.groups()
    try:
        ipaddress.IPv4Address(host)
    except ValueError as e:
        raise ValueError(f"Некорректный адрес в коде: {host}") from e
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError(f"Некорректный порт в коде: {port}")
    secret = normalize_secret(secret)
    if len(secret) != SECRET_LEN:
        raise ValueError(f"Секрет в коде должен состоять из {SECRET_LEN} символов")
    return PairingCode(host=host, port=port, secret=secret)
