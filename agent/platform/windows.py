"""
Реализация платформы для Windows 11. Работает без прав администратора.

Используем только стандартную библиотеку Python (ctypes, winreg, tkinter),
чтобы не тянуть лишние зависимости в agent.exe.
"""

from __future__ import annotations

import ctypes
import getpass
import logging
import os
import socket
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

from agent.platform.base import Platform
from shared.protocol import BROWSER_TITLES, Browser

assert sys.platform == "win32", "Этот модуль только для Windows"
import winreg  # noqa: E402

log = logging.getLogger(__name__)

APP_NAME = "ClassroomAgent"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_NAME = "Local\\ClassroomAgent_SingleInstance"
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)


# ---------- Known Folders API ----------

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> "GUID":
        u = uuid.UUID(s)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = list(u.bytes[8:])
        return g


KNOWN_FOLDERS = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",  # FOLDERID_Downloads
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",    # FOLDERID_Desktop
}

shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


# ---------- DPAPI ----------

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_UI_FORBIDDEN = 0x01
crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _blob_bytes(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


# ---------- Браузеры ----------

def _env_path(var: str, *parts: str) -> Path | None:
    base = os.environ.get(var)
    return Path(base, *parts) if base else None


def _app_path_from_registry(exe_name: str) -> Path | None:
    """Путь из реестра App Paths (туда браузеры записывают себя при установке)."""
    sub = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub) as k:
                value, _ = winreg.QueryValueEx(k, "")
                p = Path(str(value).strip('"'))
                if p.is_file():
                    return p
        except OSError:
            continue
    return None


def _browser_candidates(browser: str) -> list[Path | None]:
    if browser == "chrome":
        return [
            _app_path_from_registry("chrome.exe"),
            _env_path("ProgramFiles", "Google", "Chrome", "Application", "chrome.exe"),
            _env_path("ProgramFiles(x86)", "Google", "Chrome", "Application", "chrome.exe"),
            _env_path("LOCALAPPDATA", "Google", "Chrome", "Application", "chrome.exe"),
        ]
    if browser == "edge":
        return [
            _app_path_from_registry("msedge.exe"),
            _env_path("ProgramFiles(x86)", "Microsoft", "Edge", "Application", "msedge.exe"),
            _env_path("ProgramFiles", "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
    if browser == "yandex":
        return [
            _env_path("LOCALAPPDATA", "Yandex", "YandexBrowser", "Application", "browser.exe"),
            _env_path("ProgramFiles", "Yandex", "YandexBrowser", "Application", "browser.exe"),
            _env_path("ProgramFiles(x86)", "Yandex", "YandexBrowser", "Application", "browser.exe"),
        ]
    return []


def find_browser(browser: str) -> Path | None:
    for p in _browser_candidates(browser):
        if p is not None and p.is_file():
            return p
    return None


class WindowsPlatform(Platform):
    def __init__(self):
        self._mutex = None

    # ---------- Сведения ----------

    def username(self) -> str:
        return os.environ.get("USERNAME") or getpass.getuser()

    def hostname(self) -> str:
        return os.environ.get("COMPUTERNAME") or socket.gethostname()

    def data_dir(self) -> Path:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(base) / APP_NAME
        p.mkdir(parents=True, exist_ok=True)
        return p

    def known_folder(self, name: str) -> Path:
        guid = GUID.from_str(KNOWN_FOLDERS[name])
        out = ctypes.c_wchar_p()
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        if hr != 0:
            raise OSError(f"SHGetKnownFolderPath({name}) вернул {hr:#x}")
        try:
            return Path(out.value)
        finally:
            ole32.CoTaskMemFree(out)

    # ---------- DPAPI ----------

    def protect(self, data: bytes) -> bytes:
        blob_in, _keep = _blob(data)
        blob_out = DATA_BLOB()
        if not crypt32.CryptProtectData(ctypes.byref(blob_in), "ClassroomAgent", None, None, None,
                                        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _blob_bytes(blob_out)

    def unprotect(self, data: bytes) -> bytes:
        blob_in, _keep = _blob(data)
        blob_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                          CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _blob_bytes(blob_out)

    # ---------- Запуск ----------

    def acquire_single_instance(self) -> bool:
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._mutex = handle  # держим, пока процесс жив
        return True

    def install_autorun(self, exe_path: Path) -> None:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        log.info("Автозапуск установлен: %s", exe_path)

    def uninstall_autorun(self) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, APP_NAME)
            log.info("Автозапуск удалён")
        except FileNotFoundError:
            pass

    # ---------- Действия ----------

    def detect_browsers(self) -> list[str]:
        return [b for b in ("chrome", "yandex", "edge") if find_browser(b)]

    def open_urls(self, urls: list[str], browser: str) -> str:
        # Адреса уже проверены протоколом: только http/https
        if browser != Browser.DEFAULT.value:
            exe = find_browser(browser)
            if exe:
                subprocess.Popen(
                    [str(exe), *urls],
                    close_fds=True,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                return f"открыто в: {BROWSER_TITLES[Browser(browser)]}"
            note = f"{BROWSER_TITLES[Browser(browser)]} не установлен — открыто в браузере по умолчанию"
        else:
            note = "открыто в браузере по умолчанию"
        for url in urls:
            os.startfile(url)  # noqa: S606 — только http/https, проверено выше
        return note

    # ---------- Диалоги ----------

    def ask_pairing_code(self, error: str | None = None) -> str | None:
        import tkinter as tk

        result: dict[str, str | None] = {"code": None}
        root = tk.Tk()
        root.title("Подключение к компьютеру учителя")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        frm = tk.Frame(root, padx=20, pady=16)
        frm.pack()
        tk.Label(frm, text="Введите код из приложения учителя:", font=("Segoe UI", 11)).pack(anchor="w")
        tk.Label(frm, text="например: 192.168.1.10:8765-K7M2QX9P", fg="#777").pack(anchor="w")
        entry = tk.Entry(frm, width=34, font=("Consolas", 14))
        entry.pack(pady=8)
        entry.focus_set()
        if error:
            tk.Label(frm, text=error, fg="#c62828", wraplength=380, justify="left").pack(anchor="w")

        def ok(_=None):
            result["code"] = entry.get()
            root.destroy()

        btns = tk.Frame(frm)
        btns.pack(fill="x", pady=(8, 0))
        tk.Button(btns, text="Отмена", width=10, command=root.destroy).pack(side="right")
        tk.Button(btns, text="Подключить", width=12, command=ok).pack(side="right", padx=6)
        root.bind("<Return>", ok)
        root.mainloop()
        return result["code"]

    def show_info(self, title: str, text: str) -> None:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, text, parent=root)
        root.destroy()
