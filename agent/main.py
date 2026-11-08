"""
Точка входа агента (agent.exe).

    agent.exe              — обычный запуск (при первом запуске спросит код привязки)
    agent.exe --install    — скопировать себя в %LOCALAPPDATA%\\ClassroomAgent и включить автозапуск
    agent.exe --uninstall  — выключить автозапуск
    agent.exe --pair КОД   — привязать без диалога

После успешной привязки агент сам устанавливает автозапуск.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent import __version__
from agent.client import AgentClient, PairingError, pair_with_code
from agent.platform import get_platform
from agent.platform.base import Platform
from agent.state import AgentState

log = logging.getLogger("agent")


def setup_logging(platform: Platform) -> None:
    logs = platform.data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / "agent.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:  # в .exe без консоли stderr отсутствует
        root.addHandler(logging.StreamHandler())


def installed_exe_path(platform: Platform) -> Path:
    return platform.data_dir() / "bin" / "agent.exe"


def install(platform: Platform) -> Path | None:
    """
    Копируем agent.exe в постоянное место (чтобы его не удалили вместе с «Загрузками»)
    и прописываем автозапуск. Работает только для собранного .exe.
    """
    if not getattr(sys, "frozen", False):
        log.info("Запуск из исходников — автозапуск не устанавливаем")
        return None
    current = Path(sys.executable).resolve()
    target = installed_exe_path(platform)
    if current != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(current, target)
        except PermissionError:
            # Старая версия ещё запущена и держит файл — автозапуск будет на текущий файл
            log.warning("Не удалось заменить %s (файл занят), автозапуск на %s", target, current)
            target = current
    platform.install_autorun(target)
    return target


def interactive_pairing(platform: Platform) -> AgentState | None:
    """Спрашиваем код, пока не получится или пока не нажмут «Отмена»."""
    error = None
    while True:
        code = platform.ask_pairing_code(error)
        if code is None:
            return None
        try:
            return asyncio.run(pair_with_code(code, platform))
        except PairingError as e:
            error = str(e)


def main(argv: list[str] | None = None, platform: Platform | None = None) -> int:
    parser = argparse.ArgumentParser(description="Агент компьютерного класса")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--pair", metavar="КОД")
    args = parser.parse_args(argv)

    platform = platform or get_platform()
    setup_logging(platform)
    log.info("Агент %s запущен", __version__)

    if args.uninstall:
        platform.uninstall_autorun()
        platform.show_info("Агент класса", "Автозапуск выключен. Привязка к учителю сохранена.")
        return 0

    if not platform.acquire_single_instance():
        log.info("Агент уже запущен — выходим")
        if args.install:
            install(platform)
        return 0

    state = AgentState.load(platform)
    if args.pair:
        try:
            state = asyncio.run(pair_with_code(args.pair, platform))
        except PairingError as e:
            log.error("Привязка не удалась: %s", e)
            platform.show_info("Агент класса", f"Привязка не удалась:\n{e}")
            return 2
    elif state is None:
        state = interactive_pairing(platform)
        if state is None:
            log.info("Привязка отменена")
            return 1

    if args.install or args.pair or not AgentState.path(platform).with_name("installed").exists():
        install(platform)
        AgentState.path(platform).with_name("installed").touch()
        if not args.install:
            platform.show_info("Агент класса", f"Готово! Это ноутбук № {state.laptop_number}.\n"
                                               "Агент будет запускаться сам при входе в Windows.")

    client = AgentClient(platform, state)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass
    if client.unpaired:
        # Учитель отвязал ноутбук: забываем токен. При следующем запуске спросим код.
        AgentState.clear(platform)
        AgentState.path(platform).with_name("installed").unlink(missing_ok=True)
        log.info("Ноутбук отвязан, агент завершает работу")
    return 0


if __name__ == "__main__":
    sys.exit(main())
