"""
Всё, что зависит от операционной системы в приложении учителя: открыть файл/папку,
открыть ссылку в браузере, окно выбора файлов, сообщение об ошибке, «не давать компьютеру уснуть».

На Mac — через стандартные команды macOS, на Windows — через проводник и WinAPI.
Так приложение учителя работает и на Mac, и на Windows (админский ноутбук).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
_NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW — не мигать чёрным окном консоли


def open_path(path: str | Path) -> None:
    """Открыть файл или папку программой по умолчанию."""
    path = str(path)
    try:
        if IS_MAC:
            subprocess.Popen(["open", path])
        elif IS_WIN:
            import os
            os.startfile(path)  # noqa: S606 — путь локальный, не из сети
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        log.exception("Не удалось открыть %s", path)


def reveal(path: str | Path) -> None:
    """Показать файл в Finder (Mac) или в Проводнике (Windows), выделив его."""
    path = str(path)
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-R", path])
        elif IS_WIN:
            subprocess.Popen(["explorer", "/select,", path], creationflags=_NO_WINDOW)
        else:
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
    except OSError:
        log.exception("Не удалось показать %s", path)


def open_url(url: str) -> None:
    """Открыть ссылку в браузере по умолчанию."""
    try:
        if IS_MAC:
            subprocess.Popen(["open", url])
        elif IS_WIN:
            import os
            os.startfile(url)  # noqa: S606 — только https-ссылки, проверено выше по стеку
        else:
            subprocess.Popen(["xdg-open", url])
    except OSError:
        log.exception("Не удалось открыть ссылку %s", url)


def pick_dialog(kind: str) -> list[str]:
    """Окно выбора файлов/папок. kind: files | folders. Отмена или ошибка → []."""
    if IS_MAC:
        return _pick_mac(kind)
    if IS_WIN:
        return _pick_windows(kind)
    return _pick_tk(kind)


def _pick_mac(kind: str) -> list[str]:
    what = ('choose file with prompt "Выберите файлы для отправки на ноутбуки"' if kind == "files"
            else 'choose folder with prompt "Выберите папки для отправки на ноутбуки"')
    script = (
        "tell me to activate\n"
        f"set chosen to ({what} with multiple selections allowed)\n"
        'set out to ""\n'
        "repeat with x in chosen\n"
        "  set out to out & POSIX path of x & linefeed\n"
        "end repeat\n"
        "return out"
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [line.rstrip("/") for line in r.stdout.splitlines() if line.strip()]


def _pick_windows(kind: str) -> list[str]:
    """Стандартное окно выбора Windows (через PowerShell, отдельным процессом — без конфликта с tkinter)."""
    if kind == "folders":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            '$d.Description = "Выберите папку для отправки на ноутбуки";'
            "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
        )
    else:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Multiselect = $true;"
            '$d.Title = "Выберите файлы для отправки на ноутбуки";'
            "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write(($d.FileNames -join \"`n\")) }"
        )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                           capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        log.exception("Окно выбора недоступно")
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _pick_tk(kind: str) -> list[str]:
    """Окно выбора через tkinter (есть и в собранном exe). Папки — по одной за раз."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:  # noqa: BLE001
        log.exception("Окно выбора недоступно")
        return []
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "folders":
            d = filedialog.askdirectory(title="Выберите папку для отправки на ноутбуки")
            return [d] if d else []
        files = filedialog.askopenfilenames(title="Выберите файлы для отправки на ноутбуки")
        return list(files)
    finally:
        root.destroy()


def alert(title: str, text: str) -> None:
    """Сообщение об ошибке отдельным окном (у собранного приложения нет консоли)."""
    try:
        if IS_MAC:
            script = f'display alert "{_esc(title)}" message "{_esc(text)}" as critical'
            subprocess.run(["osascript", "-e", script], timeout=120)
        elif IS_WIN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
        else:
            print(f"{title}: {text}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError):
        log.exception("Не удалось показать сообщение")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class KeepAwake:
    """Пока приложение учителя открыто, компьютер не засыпает (чтобы не терять ноутбуки на уроке)."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._win = False

    def start(self) -> None:
        try:
            if IS_MAC:
                import os
                self._proc = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
            elif IS_WIN:
                import ctypes
                # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
                self._win = True
        except OSError:
            log.exception("Не удалось запретить сон")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None
        if self._win:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS — снять запрет
            except OSError:
                pass
            self._win = False
