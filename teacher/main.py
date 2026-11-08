"""
Точка входа приложения учителя.

Запуск для разработки:
    .venv/bin/python -m teacher.main            — окно приложения
    .venv/bin/python -m teacher.main --browser  — панель в обычном браузере (удобно для отладки)

Сервер aiohttp работает в отдельном потоке со своим циклом asyncio,
а главный поток занят окном pywebview (так требует macOS).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler

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


def start_caffeinate() -> subprocess.Popen | None:
    """
    Не даём Mac уснуть, пока работает приложение.
    -i: запрет сна при бездействии, -m: не усыплять диск, -s: не спать от сети питания,
    -w PID: caffeinate сам завершится, когда закроется наше приложение.
    """
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
    except OSError:
        log.exception("Не удалось запустить caffeinate")
        return None


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Компьютерный класс — приложение учителя")
    parser.add_argument("--browser", action="store_true", help="открыть панель в браузере вместо окна")
    args = parser.parse_args()

    storage = Storage()
    setup_logging(storage)
    log.info("Запуск. Данные: %s", storage.data_dir)
    caff = start_caffeinate()

    backend = Backend(storage)
    threading.Thread(target=backend.run, name="backend", daemon=True).start()
    backend.ready.wait()
    if backend.error:
        msg = f"Не удалось запустить сервер: {backend.error}"
        if isinstance(backend.error, OSError):
            msg += f"\nВозможно, порт {storage.port} занят — приложение уже запущено?"
        print(msg, file=sys.stderr)
        sys.exit(1)

    url = f"http://127.0.0.1:{backend.server.port}/ui/?t={storage.get_ui_token()}"
    try:
        if args.browser:
            webbrowser.open(url)
            print(f"Панель: {url}\nДля выхода нажмите Ctrl+C")
            threading.Event().wait()
        else:
            import webview  # pywebview
            webview.create_window("Компьютерный класс", url, width=1200, height=820, min_size=(800, 600))
            webview.start()
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop()
        if caff:
            caff.terminate()
        log.info("Выход")


if __name__ == "__main__":
    main()
