"""
Хранение токена Яндекс Диска на Windows через DPAPI (как у агента на ноутбуках).

DPAPI шифрует данные ключом текущего пользователя Windows: расшифровать их сможет
только тот же пользователь на этом же компьютере. Файл с токеном лежит в папке данных
приложения. Так не нужен keyring и его зависимости в собранном exe.
"""

from __future__ import annotations

import base64
import ctypes
import logging
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_UI_FORBIDDEN = 0x01
crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _blob(data: bytes) -> tuple[_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _out(blob: _BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


def _protect(data: bytes) -> bytes:
    blob_in, _keep = _blob(data)
    blob_out = _BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), "ClassroomTeacher", None, None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _out(blob_out)


def _unprotect(data: bytes) -> bytes:
    blob_in, _keep = _blob(data)
    blob_out = _BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _out(blob_out)


class DpapiTokenStore:
    """Токен Яндекс Диска, зашифрованный DPAPI, в файле data_dir/yadisk_token.dpapi."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "yadisk_token.dpapi"

    def get(self) -> str | None:
        try:
            if not self.path.exists():
                return None
            return _unprotect(base64.b64decode(self.path.read_bytes())).decode("utf-8")
        except Exception:  # noqa: BLE001 — файл скопировали с другого ПК / повреждён
            log.exception("Не удалось прочитать токен Диска")
            return None

    def set(self, token: str) -> None:
        self.path.write_bytes(base64.b64encode(_protect(token.encode("utf-8"))))

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
