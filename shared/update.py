"""
Обновления агента: проверка цифровой подписи и версий.

Каждый релиз agent.exe сопровождается файлом manifest.json
    {"version": "0.3.0", "sha256": "...", "size": 12345678, "file": "agent.exe"}
и подписью manifest.sig (Ed25519, base64). Подписывает GitHub Actions закрытым
ключом из секрета AGENT_SIGNING_KEY. Агент (и учитель) проверяют подпись этим
открытым ключом, а затем — SHA-256 самого файла. Подделать обновление, не имея
закрытого ключа, невозможно, откуда бы файл ни пришёл.
"""

from __future__ import annotations

import base64
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Открытый ключ проверки обновлений (создан tools/make_signing_key.py)
PUBLIC_KEY_B64 = "HcAXT/W0mse7qrolkEIy7APqjtxY8V/8lyqj3JRJKfg="

_VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_AGENT_SIZE = 200 * 1024 * 1024


class UpdateError(Exception):
    pass


def parse_version(v: str) -> tuple[int, int, int]:
    if not isinstance(v, str) or not _VERSION_RE.match(v):
        raise UpdateError(f"Некорректная версия: {v!r}")
    a, b, c = (int(x) for x in v.split("."))
    return a, b, c


def is_newer(candidate: str, current: str) -> bool:
    try:
        return parse_version(candidate) > parse_version(current)
    except UpdateError:
        return False


def verify_manifest(manifest_bytes: bytes, signature_b64: str | bytes, public_key_b64: str | None = None) -> dict:
    """Проверить подпись манифеста и его содержимое. Возвращает манифест или бросает UpdateError."""
    public_key_b64 = public_key_b64 or PUBLIC_KEY_B64
    try:
        sig = base64.b64decode(signature_b64, validate=True)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(sig, manifest_bytes)
    except (InvalidSignature, ValueError) as e:
        raise UpdateError("Подпись обновления неверна — файл не от разработчика") from e
    try:
        m = json.loads(manifest_bytes)
    except ValueError as e:
        raise UpdateError("Повреждённый манифест") from e
    parse_version(m.get("version"))
    if not _SHA_RE.match(str(m.get("sha256", ""))):
        raise UpdateError("В манифесте нет SHA-256")
    if not isinstance(m.get("size"), int) or not 0 < m["size"] <= MAX_AGENT_SIZE:
        raise UpdateError("Некорректный размер в манифесте")
    return m
