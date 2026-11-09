"""
Что открыто на ноутбуке (Windows): окна программ и вкладки браузеров.

Окна — как на панели задач: видимые окна верхнего уровня с заголовком (WinAPI через ctypes).
Вкладки — через Windows UI Automation (то же, чем пользуются экранные дикторы): браузеры
Chrome, Edge, Яндекс показывают полоску вкладок как элементы «вкладка» с заголовком.
Адрес видно только у активной вкладки (из адресной строки).

Закрыть вкладку: нажимаем её собственную кнопку «Закрыть» (через UI Automation, окно
не нужно выводить на передний план). Если кнопки нет — выбираем вкладку и нажимаем Ctrl+W.

Вся работа с UI Automation идёт в одном отдельном потоке (COM так требует).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

from shared.protocol import PROTECTED_PROCESSES, ProtocolError

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
version_dll = ctypes.WinDLL("version")

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
version_dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
version_dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
WS_EX_APPWINDOW = 0x40000
DWMWA_CLOAKED = 14
WM_CLOSE = 0x0010
SW_RESTORE = 9
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
VK_CONTROL, VK_MENU, VK_W = 0x11, 0x12, 0x57
KEYEVENTF_KEYUP = 0x2

# Окна рабочего стола и панели задач: в списке не показываем и никогда не закрываем
# (WM_CLOSE окну «Program Manager» открывает диалог выключения компьютера).
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"}
# Процессы агента: его окна (заставка, сообщение) ученикам и учителю закрывать нельзя
AGENT_PROCESSES = {"agent.exe"}
BROWSER_PROCESSES = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "browser.exe": "Яндекс Браузер",
    "firefox.exe": "Firefox", "opera.exe": "Opera",
}
MAX_WINDOWS = 80
MAX_TABS_PER_WINDOW = 150
MAX_TITLE = 300

# ---------- UI Automation: константы ----------
TreeScope_Children = 2
TreeScope_Descendants = 4
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_ClassNamePropertyId = 30012
UIA_ValueValuePropertyId = 30045
UIA_SelectionItemIsSelectedPropertyId = 30079
UIA_ButtonControlTypeId = 50000
UIA_EditControlTypeId = 50004
UIA_TabItemControlTypeId = 50019
UIA_DocumentControlTypeId = 50030
UIA_InvokePatternId = 10000
UIA_SelectionItemPatternId = 10010
UIA_LegacyIAccessiblePatternId = 10018
# Содержимое страницы в Chromium-браузерах: внутрь не заходим (долго, и там бывают свои «вкладки»)
WEB_CONTENT_CLASSES = {"Chrome_RenderWidgetHostHWND"}
ADDRESS_BAR_CLASSES = {"OmniboxViewViews"}
MAX_UIA_NODES = 2500
MAX_UIA_DEPTH = 30


# ================= Окна =================

def _window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_path(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


_descriptions: dict[str, str] = {}


def _file_description(path: str) -> str:
    """Понятное имя программы из свойств exe-файла («Paint», «Google Chrome»)."""
    if not path:
        return ""
    if path in _descriptions:
        return _descriptions[path]
    desc = ""
    try:
        size = version_dll.GetFileVersionInfoSizeW(path, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if version_dll.GetFileVersionInfoW(path, 0, size, buf):
                ptr, ln = ctypes.c_void_p(), wintypes.UINT()
                langs = []
                if version_dll.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(ln)) \
                        and ln.value >= 4:
                    words = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD))
                    langs = [(words[i * 2], words[i * 2 + 1]) for i in range(min(ln.value // 4, 8))]
                for lang, cp in langs + [(0x0419, 0x04B0), (0x0409, 0x04B0), (0x0409, 0x04E4)]:
                    key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
                    if version_dll.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(ln)) and ln.value > 1:
                        desc = ctypes.wstring_at(ptr.value, ln.value).rstrip("\0").strip()
                        if desc:
                            break
    except OSError:
        desc = ""
    _descriptions[path] = desc[:80]
    return _descriptions[path]


def _is_cloaked(hwnd) -> bool:
    """Скрытые системой окна (приложения из Microsoft Store в фоне, другие рабочие столы)."""
    val = wintypes.DWORD()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)) != 0:
        return False
    return val.value != 0


def _is_app_window(hwnd) -> bool:
    """Окно, которое видно на панели задач."""
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        return False
    if user32.GetWindow(hwnd, GW_OWNER):
        return False
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
        return False
    if _class_name(hwnd) in SHELL_CLASSES:
        return False
    if not _window_text(hwnd).strip():
        return False
    return not _is_cloaked(hwnd)


def _real_pid(hwnd, pid: int, process: str) -> int:
    """Приложения из Microsoft Store живут внутри ApplicationFrameHost: ищем настоящий процесс."""
    if process != "applicationframehost.exe":
        return pid
    found = []

    @WNDENUMPROC
    def cb(child, _):
        cpid = _window_pid(child)
        if cpid and cpid != pid:
            found.append(cpid)
            return False
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return found[0] if found else pid


def _describe(hwnd) -> dict | None:
    pid = _window_pid(hwnd)
    if pid == os.getpid():
        return None
    path = _process_path(pid)
    process = Path(path).name.lower() if path else ""
    real = _real_pid(hwnd, pid, process)
    if real != pid:
        path = _process_path(real) or path
        process = Path(path).name.lower() if path else process
    if process in AGENT_PROCESSES:
        return None
    app = BROWSER_PROCESSES.get(process) or _file_description(path) or (Path(path).stem if path else "") or "Программа"
    return {
        "id": int(hwnd or 0),
        "title": _window_text(hwnd)[:MAX_TITLE],
        "app": app,
        "process": process,
        "pid": real,
        "browser": process in BROWSER_PROCESSES,
        "minimized": bool(user32.IsIconic(hwnd)),
        "tabs": [],
    }


def list_windows() -> list[dict]:
    hwnds = []

    @WNDENUMPROC
    def cb(hwnd, _):
        hwnds.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    out = []
    for hwnd in hwnds:   # порядок EnumWindows — сверху вниз: верхнее (активное) окно первое
        try:
            if _is_app_window(hwnd):
                d = _describe(hwnd)
                if d and d["id"]:
                    out.append(d)
        except OSError:
            log.debug("Не удалось прочитать окно %s", hwnd, exc_info=True)
        if len(out) >= MAX_WINDOWS:
            break
    return out


def _checked_window(window: int) -> dict:
    """Окно из запроса учителя: должно существовать и быть обычным окном программы."""
    if not user32.IsWindow(window):
        raise ProtocolError("окно уже закрыто")
    if not _is_app_window(window):
        raise ProtocolError("это окно закрывать нельзя")
    d = _describe(window)
    if d is None:
        raise ProtocolError("это окно закрывать нельзя")
    return d


def _wait(cond, timeout: float, step: float = 0.15) -> bool:
    end = time.monotonic() + timeout
    while True:
        if cond():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(step)


def close_window(window: int, force: bool) -> str:
    d = _checked_window(window)
    name = d["app"]
    if force:
        if d["process"] in PROTECTED_PROCESSES:
            raise ProtocolError(f"{name}: эту программу завершать нельзя")
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, d["pid"])
        if not h:
            raise ProtocolError(f"{name}: нет доступа, чтобы завершить программу")
        try:
            if not kernel32.TerminateProcess(h, 1):
                raise ProtocolError(f"{name}: не удалось завершить программу")
        finally:
            kernel32.CloseHandle(h)
        _wait(lambda: not user32.IsWindow(window), 3)
        return f"программа завершена: {name}"
    if d["process"] in PROTECTED_PROCESSES and d["process"] != "explorer.exe":
        raise ProtocolError(f"{name}: это системное окно, его закрывать нельзя")
    user32.PostMessageW(window, WM_CLOSE, 0, 0)
    if _wait(lambda: not user32.IsWindow(window) or not user32.IsWindowVisible(window), 2.5):
        return f"окно закрыто: {name}"
    return f"{name}: программа не закрылась — возможно, спрашивает «Сохранить?». Можно завершить принудительно"


# ================= Вкладки (UI Automation) =================

class _UIA:
    """IUIAutomation в своём потоке. Создаётся при первом обращении."""

    def __init__(self):
        sys.coinit_flags = 0   # COINIT_MULTITHREADED — для этого потока (comtypes читает при импорте)
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass   # поток уже инициализирован
        self.mod = comtypes.client.GetModule("UIAutomationCore.dll")
        self.client = comtypes.client.CreateObject(self.mod.CUIAutomation, interface=self.mod.IUIAutomation)
        self.true = self.client.CreateTrueCondition()
        self.cache = self.client.CreateCacheRequest()
        for prop in (UIA_ControlTypePropertyId, UIA_NamePropertyId, UIA_ClassNamePropertyId,
                     UIA_ValueValuePropertyId, UIA_SelectionItemIsSelectedPropertyId):
            self.cache.AddProperty(prop)

    def children(self, elem) -> list:
        arr = elem.FindAllBuildCache(TreeScope_Children, self.true, self.cache)
        if not arr:
            return []
        return [arr.GetElement(i) for i in range(arr.Length)]

    def scan(self, hwnd: int) -> tuple[list, str]:
        """Вкладки окна браузера (элементы UIA, по порядку) и адрес из адресной строки."""
        root = self.client.ElementFromHandleBuildCache(hwnd, self.cache)
        tabs, address = [], ""
        stack, seen = [(root, 0)], 0
        while stack and seen < MAX_UIA_NODES:
            elem, depth = stack.pop()
            seen += 1
            try:
                ctype = elem.CachedControlType
                cls = elem.CachedClassName or ""
            except Exception:  # noqa: BLE001 — элемент исчез, пока читали
                continue
            if ctype == UIA_TabItemControlTypeId:
                tabs.append(elem)
                continue   # внутри вкладки — только её кнопки
            if ctype == UIA_DocumentControlTypeId or cls in WEB_CONTENT_CLASSES:
                continue
            if ctype == UIA_EditControlTypeId and cls in ADDRESS_BAR_CLASSES and not address:
                try:
                    address = str(elem.GetCachedPropertyValue(UIA_ValueValuePropertyId) or "")
                except Exception:  # noqa: BLE001
                    address = ""
                continue
            if depth < MAX_UIA_DEPTH:
                try:
                    kids = self.children(elem)
                except Exception:  # noqa: BLE001
                    continue
                stack.extend((k, depth + 1) for k in reversed(kids))   # обход по порядку слева направо
        return tabs, address

    @staticmethod
    def name(elem) -> str:
        try:
            return (elem.CachedName or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def selected(elem) -> bool:
        try:
            return elem.GetCachedPropertyValue(UIA_SelectionItemIsSelectedPropertyId) is True
        except Exception:  # noqa: BLE001
            return False

    def tabs(self, hwnd: int) -> list[dict]:
        elems, address = self.scan(hwnd)
        out = []
        for i, e in enumerate(elems[:MAX_TABS_PER_WINDOW]):
            active = self.selected(e)
            out.append({"title": self.name(e)[:MAX_TITLE], "index": i, "active": active,
                        "url": address[:500] if active else ""})
        return out

    def pattern(self, elem, pattern_id: int, iface):
        p = elem.GetCurrentPattern(pattern_id)
        return p.QueryInterface(iface) if p else None

    def close_button(self, tab):
        """Кнопка «Закрыть» внутри вкладки (у Chrome/Edge/Яндекса она есть у каждой вкладки)."""
        cond = self.client.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
        arr = tab.FindAll(TreeScope_Descendants, cond)
        buttons = [arr.GetElement(i) for i in range(arr.Length)] if arr else []
        for b in buttons:
            name = (b.CurrentName or "").lower()
            if "закр" in name or "close" in name:
                return b
        return buttons[-1] if len(buttons) == 1 else None

    def close_tab(self, hwnd: int, title: str, index: int) -> str:
        def find():
            elems, _ = self.scan(hwnd)
            names = [self.name(e) for e in elems]
            return elems, names

        elems, names = find()
        same = [i for i, n in enumerate(names) if n == title.strip()]
        if not same:
            return "вкладка уже закрыта"
        i = min(same, key=lambda k: abs(k - index))
        tab, before, before_same = elems[i], len(elems), len(same)

        def gone() -> bool:
            if not user32.IsWindow(hwnd):
                return True
            try:
                _, now = find()
            except Exception:  # noqa: BLE001
                return False
            return len(now) < before or sum(1 for n in now if n == title.strip()) < before_same

        # 1) Кнопка «Закрыть» на самой вкладке — без переключения окон
        try:
            btn = self.close_button(tab)
            inv = self.pattern(btn, UIA_InvokePatternId, self.mod.IUIAutomationInvokePattern) if btn else None
            if inv is not None:
                inv.Invoke()
                if _wait(gone, 2):
                    return f"вкладка закрыта: {title.strip() or 'без названия'}"
        except Exception:  # noqa: BLE001
            log.info("Кнопка закрытия вкладки не сработала", exc_info=True)

        # 2) Выбрать вкладку и нажать Ctrl+W в этом окне
        try:
            sel = self.pattern(tab, UIA_SelectionItemPatternId, self.mod.IUIAutomationSelectionItemPattern)
            if sel is not None:
                sel.Select()
            else:
                acc = self.pattern(tab, UIA_LegacyIAccessiblePatternId, self.mod.IUIAutomationLegacyIAccessiblePattern)
                if acc is not None:
                    acc.DoDefaultAction()
        except Exception:  # noqa: BLE001
            log.info("Не удалось выбрать вкладку", exc_info=True)
            raise ProtocolError("не удалось выбрать вкладку") from None
        time.sleep(0.2)
        if not _bring_to_front(hwnd):
            raise ProtocolError("не удалось закрыть вкладку: браузер не отвечает")
        _press_ctrl_w()
        if _wait(gone, 2):
            return f"вкладка закрыта: {title.strip() or 'без названия'}"
        raise ProtocolError("вкладка не закрылась (возможно, сайт спрашивает «Покинуть страницу?»)")


def _bring_to_front(hwnd: int) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    if _wait(lambda: user32.GetForegroundWindow() == hwnd, 0.5):
        return True
    # Windows не даёт фоновой программе выводить окна вперёд; короткое нажатие Alt снимает запрет
    user32.keybd_event(VK_MENU, 0, 0, None)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
    user32.SetForegroundWindow(hwnd)
    return _wait(lambda: user32.GetForegroundWindow() == hwnd, 1)


def _press_ctrl_w() -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, None)
    user32.keybd_event(VK_W, 0, 0, None)
    user32.keybd_event(VK_W, 0, KEYEVENTF_KEYUP, None)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, None)


# ================= Поток и публичные функции =================

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="open-windows")
_local = threading.local()
_uia_error = ""


def _uia() -> _UIA | None:
    global _uia_error
    if getattr(_local, "uia", None) is None and not _uia_error:
        try:
            _local.uia = _UIA()
        except Exception as e:  # noqa: BLE001
            log.exception("UI Automation недоступен")
            _uia_error = f"вкладки браузеров недоступны: {e}"[:200]
    return getattr(_local, "uia", None)


def _run(fn, *args, timeout: float = 40):
    return _executor.submit(fn, *args).result(timeout=timeout)


def _list_open() -> dict:
    windows = list_windows()
    uia = _uia() if any(w["browser"] for w in windows) else None
    tabs_error = _uia_error if any(w["browser"] for w in windows) else ""
    deadline = time.monotonic() + 15
    for w in windows:
        if not w["browser"] or uia is None:
            continue
        if time.monotonic() > deadline:
            tabs_error = "не все вкладки успели прочитаться"
            break
        try:
            w["tabs"] = uia.tabs(w["id"])
        except Exception as e:  # noqa: BLE001
            log.info("Не удалось прочитать вкладки окна %s: %s", w["id"], e)
    for w in windows:
        w.pop("pid", None)
    return {"windows": windows, "tabs_error": tabs_error}


def _close_tab(window: int, title: str, index: int) -> str:
    d = _checked_window(window)
    if not d["browser"]:
        raise ProtocolError("это окно — не браузер")
    uia = _uia()
    if uia is None:
        raise ProtocolError(_uia_error or "вкладки браузеров недоступны")
    return uia.close_tab(window, title, index)


def list_open() -> dict:
    return _run(_list_open)


def close_window_safe(window: int, force: bool) -> str:
    return _run(close_window, window, force)


def close_tab(window: int, title: str, index: int) -> str:
    return _run(_close_tab, window, title, index)
