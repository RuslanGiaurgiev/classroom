"""
Полноэкранное окно поверх всех окон на всех мониторах. Запускается отдельным
процессом: agent.exe --overlay <параметры>.

Режимы:
  lock      — «Внимание на учителя»: закрыть нельзя, снимается командой учителя
  message   — сообщение учителя (убирается по таймеру или командой)
  countdown — «Компьютер выключится через N секунд»

Как мешаем ребёнку закрыть окно (без прав администратора):
  • окно без рамки, на весь экран, поверх всех, каждые 0,3 с возвращается наверх;
  • перехват клавиатуры (low-level hook): Win, Alt+Tab, Alt+F4, Esc, Ctrl+Shift+Esc, меню;
  • если процесс всё-таки убили — агент запускает окно снова (см. OverlaySupervisor).
Ctrl+Alt+Del Windows перехватить не даёт — это ограничение системы.

Окно само закрывается, если агент завершился или удалил управляющий файл
(spec["control_file"]) — так агент надёжно убирает окно.
"""

from __future__ import annotations

import base64
import json
import os
import sys

COLORS = {
    "lock": ("#1d3f8f", "#ffffff"),
    "message": ("#1f2937", "#ffffff"),
    "countdown": ("#8a1c1c", "#ffffff"),
}


def decode_spec(arg: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(arg.encode()).decode())


# ---------- Windows: мониторы, клавиатура, родительский процесс ----------

def _monitor_rects() -> list[tuple[int, int, int, int]]:
    """Прямоугольники всех мониторов (x, y, ширина, высота)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    rects: list[tuple[int, int, int, int]] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, hdc, lprect, lparam):
        r = lprect.contents
        rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return rects


def _set_dpi_aware() -> None:
    """Чтобы координаты мониторов совпадали с координатами окон при масштабе 125–150%."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_hook_refs: list = []  # держим ссылки, иначе сборщик мусора удалит callback


def _install_keyboard_hook() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    VK_TAB, VK_ESCAPE, VK_F4, VK_LWIN, VK_RWIN, VK_APPS, VK_SPACE = 0x09, 0x1B, 0x73, 0x5B, 0x5C, 0x5D, 0x20
    LLKHF_ALTDOWN = 0x20

    def proc(n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            alt = bool(kb.flags & LLKHF_ALTDOWN)
            vk = kb.vkCode
            if vk in (VK_LWIN, VK_RWIN, VK_APPS, VK_ESCAPE) or (alt and vk in (VK_TAB, VK_F4, VK_SPACE)):
                return 1  # «съедаем» клавишу
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    cb = HOOKPROC(proc)
    hook = user32.SetWindowsHookExW(13, cb, kernel32.GetModuleHandleW(None), 0)  # 13 = WH_KEYBOARD_LL
    _hook_refs.extend([cb, hook])


def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        k = ctypes.windll.kernel32
        h = k.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------- Окно ----------

def run_overlay(spec: dict) -> int:
    import tkinter as tk

    mode = spec.get("mode", "message")
    text = spec.get("text", "")
    seconds = int(spec.get("seconds") or 0)
    parent_pid = int(spec.get("parent_pid") or 0)
    control_file = spec.get("control_file")
    bg, fg = COLORS.get(mode, COLORS["message"])

    _set_dpi_aware()
    root = tk.Tk()
    rects = _monitor_rects() or [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
    windows = [root] + [tk.Toplevel(root) for _ in rects[1:]]
    countdown_labels = []

    for win, (x, y, w, h) in zip(windows, rects):
        win.overrideredirect(True)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.configure(bg=bg, cursor="none" if mode == "lock" else "")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: None)  # закрыть нельзя
        frame = tk.Frame(win, bg=bg)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        size = max(28, min(96, w // 16))
        tk.Label(frame, text=text, bg=bg, fg=fg, font=("Segoe UI", size, "bold"),
                 wraplength=int(w * 0.85), justify="center").pack()
        if mode == "countdown":
            lbl = tk.Label(frame, text="", bg=bg, fg=fg, font=("Segoe UI", size * 2, "bold"))
            lbl.pack(pady=(20, 0))
            countdown_labels.append(lbl)
            tk.Label(frame, text="Сохраните работу", bg=bg, fg=fg, font=("Segoe UI", size // 2)).pack()

    if mode in ("lock", "countdown", "message"):
        _install_keyboard_hook()

    def keep_on_top():
        for win in windows:
            win.attributes("-topmost", True)
            win.lift()
        try:
            root.focus_force()
        except tk.TclError:
            pass
        if not _parent_alive(parent_pid) or (control_file and not os.path.exists(control_file)):
            root.destroy()
            return
        root.after(300, keep_on_top)

    left = {"s": seconds}

    def tick():
        for lbl in countdown_labels:
            lbl.config(text=str(max(0, left["s"])))
        left["s"] -= 1
        root.after(1000, tick)

    if countdown_labels:
        tick()
    keep_on_top()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run_overlay(decode_spec(sys.argv[1])))
