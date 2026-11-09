"""
Точка входа приложения учителя.

Запуск для разработки:
    .venv/bin/python -m teacher.main            — окно приложения
    .venv/bin/python -m teacher.main --browser  — панель в обычном браузере (удобно для отладки)

Сервер aiohttp работает в отдельном потоке со своим циклом asyncio.
На Mac главный поток занят окном приложения (pywebview), на Windows — панель
открывается в браузере, а приложение показывает маленькое окно управления.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from teacher import osbridge
from teacher.discovery import Advertiser
from teacher.server import TeacherServer
from teacher.storage import Storage

log = logging.getLogger("teacher")


def setup_logging(storage: Storage) -> None:
    logs = storage.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(logs / "teacher.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


class Backend:
    """Сервер + mDNS в фоновом потоке."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self.loop = asyncio.new_event_loop()
        self.server = TeacherServer(storage)
        self.advertiser: Advertiser | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except BaseException as e:  # например, порт занят
            self.error = e
            log.exception("Не удалось запустить сервер")
            self.ready.set()
            return
        self.ready.set()
        self.loop.run_forever()

    async def _start(self) -> None:
        port = await self.server.start()
        self.advertiser = Advertiser(self.storage.teacher_id, port)
        await self.advertiser.start()

    def stop(self) -> None:
        async def _stop():
            if self.advertiser:
                await self.advertiser.stop()
            await self.server.stop()
        fut = asyncio.run_coroutine_threadsafe(_stop(), self.loop)
        try:
            fut.result(timeout=5)
        except Exception:
            log.exception("Ошибка при остановке")
        self.loop.call_soon_threadsafe(self.loop.stop)


def selftest(out_file: str) -> int:
    """Проверка собранного Айтишкино.exe в CI: все модули на месте, веб-панель внутри сборки."""
    try:
        import importlib
        for mod in ("teacher.server", "teacher.storage", "teacher.yadisk", "teacher.disk_send",
                    "teacher.importer", "teacher.osbridge", "teacher.discovery", "aiohttp", "zeroconf",
                    "cryptography.hazmat.primitives.asymmetric.ed25519", "tkinter", "sqlite3", "rapidfuzz"):
            importlib.import_module(mod)
        if sys.platform == "win32":
            importlib.import_module("teacher.winsecret")
        from teacher.server import WEB_DIR
        if not (WEB_DIR / "index.html").exists():
            raise FileNotFoundError(f"нет веб-панели: {WEB_DIR}")
        from teacher import __version__
        Path(out_file).write_text(f"ok {__version__}", encoding="utf-8")
        return 0
    except Exception as e:  # noqa: BLE001
        Path(out_file).write_text(f"fail {e!r}", encoding="utf-8")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Айтишкино — компьютерный класс (приложение учителя)")
    parser.add_argument("--browser", action="store_true", help="открыть панель в браузере, без окна")
    parser.add_argument("--window", action="store_true", help="окно приложения (pywebview), даже на Windows")
    parser.add_argument("--selftest", help=argparse.SUPPRESS)   # служебный: проверка сборки в CI
    args = parser.parse_args()

    if args.selftest:
        sys.exit(selftest(args.selftest))

    storage = Storage()
    setup_logging(storage)
    log.info("Запуск. Данные: %s", storage.data_dir)
    awake = osbridge.KeepAwake()
    awake.start()

    backend = Backend(storage)
    threading.Thread(target=backend.run, name="backend", daemon=True).start()
    backend.ready.wait()
    if backend.error:
        msg = f"Не удалось запустить сервер: {backend.error}"
        if isinstance(backend.error, OSError):
            msg += f"\nВозможно, приложение уже запущено, или порт {storage.port} занят."
        print(msg, file=sys.stderr)
        osbridge.alert("Айтишкино", msg)
        awake.stop()
        sys.exit(1)

    url = f"http://127.0.0.1:{backend.server.port}/ui/?t={storage.get_ui_token()}"
    ip, port = backend.server.host_ip_fn(), backend.server.port
    try:
        if args.browser:
            webbrowser.open(url)
            print(f"Панель: {url}\nДля выхода нажмите Ctrl+C")
            threading.Event().wait()
        elif args.window or osbridge.IS_MAC:
            import webview  # pywebview — окно приложения (на Mac)
            webview.create_window("Айтишкино — компьютерный класс", url, width=1200, height=820, min_size=(800, 600))
            webview.start()
        else:
            run_control_window(url, ip, port)   # Windows: панель в браузере + окно управления
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop()
        awake.stop()
        log.info("Выход")


def run_control_window(url: str, ip: str, port: int) -> None:
    """
    Маленькое окно «Айтишкино работает» (Windows): кнопки «Открыть панель» и «Выход».
    Панель открывается в браузере по умолчанию. Пока окно открыто — приложение работает.
    """
    import tkinter as tk

    webbrowser.open(url)   # сразу открываем панель

    root = tk.Tk()
    root.title("Айтишкино")
    root.configure(bg="#eef7f5")
    try:
        root.iconbitmap(_icon_path())
    except Exception:  # noqa: BLE001 — без иконки тоже работает
        pass
    root.geometry("430x250")
    root.minsize(380, 230)

    tk.Label(root, text="Айтишкино", font=("Segoe UI", 22, "bold"), fg="#437160", bg="#eef7f5").pack(pady=(22, 2))
    tk.Label(root, text="Компьютерный класс работает", font=("Segoe UI", 11), fg="#5d6e6a", bg="#eef7f5").pack()
    tk.Label(root, text=f"Адрес для ноутбуков: {ip}:{port}", font=("Segoe UI", 10), fg="#5d6e6a", bg="#eef7f5").pack(pady=(10, 0))
    tk.Label(root, text="Панель открыта в браузере. Не закрывайте это окно во время урока.",
             font=("Segoe UI", 9), fg="#5d6e6a", bg="#eef7f5", wraplength=380).pack(pady=(6, 14))

    btns = tk.Frame(root, bg="#eef7f5")
    btns.pack()
    tk.Button(btns, text="Открыть панель", font=("Segoe UI", 11, "bold"), bg="#69b9ad", fg="white",
              activebackground="#50936b", relief="flat", padx=16, pady=7, cursor="hand2",
              command=lambda: webbrowser.open(url)).pack(side="left", padx=6)
    tk.Button(btns, text="Выход", font=("Segoe UI", 11), bg="#ffffff", fg="#437160",
              relief="flat", padx=16, pady=7, cursor="hand2", command=root.destroy).pack(side="left", padx=6)

    root.mainloop()


def _icon_path() -> str:
    """Путь к иконке .ico внутри собранного приложения (или в исходниках)."""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    for cand in (root / "teacher" / "web" / "icon.ico", root / "web" / "icon.ico", root / "icon.ico"):
        if cand.exists():
            return str(cand)
    return ""


if __name__ == "__main__":
    main()
