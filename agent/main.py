"""
Точка входа агента (agent.exe).

    agent.exe              — обычный запуск (при первом запуске сам найдёт учителя
                             и попросит его нажать «Принять» в панели)
    agent.exe --install    — скопировать себя в %LOCALAPPDATA%\\ClassroomAgent и включить автозапуск
    agent.exe --uninstall  — выключить автозапуск

После подключения агент сам устанавливает автозапуск.
"""

from __future__ import annotations

import argparse
import os
import asyncio
import logging
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent import __version__
from agent.client import AgentClient
from agent.collector import Collector
from agent.core import AgentCore
from agent.pairing import PairingFlow
from agent.updater import (
    after_update_started,
    cleanup_after_update,
    installed_exe_path,
    installed_version,
    write_installed_version,
)
from shared.update import is_newer
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


def _selftest_version(exe: Path) -> str | None:
    """Спросить у exe его версию (для установок без файла VERSION)."""
    import subprocess
    import tempfile
    out = Path(tempfile.gettempdir()) / f"agent-version-{os.getpid()}.txt"
    try:
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")   # другой exe — пусть распакуется сам
        subprocess.run([str(exe), "--selftest", str(out)], timeout=60, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.read_text(encoding="utf-8")
        return text.split()[1] if text.startswith("ok ") else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None
    finally:
        out.unlink(missing_ok=True)


def install(platform: Platform) -> Path | None:
    """
    Копируем agent.exe в постоянное место (чтобы его не удалили вместе с «Загрузками»)
    и прописываем автозапуск. Работает только для собранного .exe.
    Старая копия НИКОГДА не заменяет установленную более новую версию.
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
            write_installed_version(platform, __version__)
        except PermissionError:
            # Файл занят — автозапуск будет на текущий файл
            log.warning("Не удалось заменить %s (файл занят), автозапуск на %s", target, current)
            target = current
    else:
        write_installed_version(platform, __version__)
    platform.install_autorun(target)
    return target


def newer_installed_elsewhere(platform: Platform) -> Path | None:
    """
    Запустили agent.exe не из постоянного места (например, старую копию из «Загрузок»),
    а установлена такая же или более новая версия — вернуть путь к установленной.
    """
    if not getattr(sys, "frozen", False):
        return None
    target = installed_exe_path(platform)
    if Path(sys.executable).resolve() == target.resolve() or not target.exists():
        return None
    v = installed_version(platform, run_selftest=_selftest_version)
    if v and not is_newer(__version__, v):
        return target
    return None


def selftest(out_file: str) -> int:
    """
    Проверка собранного agent.exe перед публикацией: все модули на месте,
    ключ проверки подписи корректный. Результат пишется в файл (у exe нет консоли).
    """
    try:
        import importlib
        for mod in ("agent.collector", "agent.core", "agent.client", "agent.pairing", "agent.overlay_window",
                    "agent.updater", "agent.discovery", "watchdog.observers", "zeroconf", "aiohttp",
                    "cryptography.hazmat.primitives.asymmetric.ed25519", "tkinter", "sqlite3"):
            importlib.import_module(mod)
        if sys.platform == "win32":
            importlib.import_module("agent.platform.windows")
            # tkinter (окна «Внимание», сообщения) находит свои файлы
            from agent.platform.windows import own_files_ok
            if not own_files_ok():
                raise RuntimeError("нет файлов Tcl/Tk в распаковке exe")
            import tkinter
            tkinter.Tcl().eval("info patchlevel")
            # Список окон и UI Automation (comtypes) работают в собранном exe
            from agent.platform import win_open
            win_open.list_open()
            if win_open._uia() is None:
                raise RuntimeError(win_open._uia_error)
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        from shared.update import PUBLIC_KEY_B64
        Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
        Path(out_file).write_text(f"ok {__version__}", encoding="utf-8")
        return 0
    except Exception as e:  # noqa: BLE001
        Path(out_file).write_text(f"fail {e!r}", encoding="utf-8")
        return 1


def ci_inherit_test(out_file: str) -> int:
    """
    Проверка в CI ровно того, что ломалось при самообновлении: у текущей распаковки удаляем
    данные tkinter (так делал завершившийся старый агент) и запускаем свою копию «по наследству»,
    без сброса окружения. Копия должна сама перезапуститься начисто и пройти самопроверку.
    """
    import subprocess
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        Path(out_file).write_text("fail не собранный exe", encoding="utf-8")
        return 1
    shutil.rmtree(os.path.join(base, "_tcl_data"), ignore_errors=True)
    shutil.rmtree(os.path.join(base, "_tk_data"), ignore_errors=True)
    return subprocess.call([sys.executable, "--selftest", out_file], timeout=120)


def main(argv: list[str] | None = None, platform: Platform | None = None) -> int:
    parser = argparse.ArgumentParser(description="Агент компьютерного класса")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--overlay", help=argparse.SUPPRESS)  # служебный: полноэкранное окно
    parser.add_argument("--after-update", help=argparse.SUPPRESS)  # служебный: запуск после обновления
    parser.add_argument("--selftest", help=argparse.SUPPRESS)      # служебный: проверка сборки в CI
    parser.add_argument("--ci-inherit-test", help=argparse.SUPPRESS)  # служебный: запуск «по наследству» (CI)
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(args.selftest)

    if args.ci_inherit_test:
        return ci_inherit_test(args.ci_inherit_test)

    if args.overlay:
        # Отдельный процесс полноэкранного окна — без логов, mutex и сети
        from agent.overlay_window import decode_spec, run_overlay
        return run_overlay(decode_spec(args.overlay))

    platform = platform or get_platform()
    setup_logging(platform)
    log.info("Агент %s запущен", __version__)

    if args.uninstall:
        platform.uninstall_autorun()
        platform.show_info("Агент класса", "Автозапуск выключен. Привязка к учителю сохранена.")
        return 0

    if args.after_update:
        # Старая версия отпустила «замок» и ждёт нашего сигнала; даём ей до 20 секунд
        import time
        for _ in range(100):
            if platform.acquire_single_instance():
                break
            time.sleep(0.2)
        else:
            log.error("После обновления не удалось стать единственным экземпляром")
            return 1
        after_update_started(platform, args.after_update)
        log.info("Запущена обновлённая версия %s", __version__)
    elif not platform.acquire_single_instance():
        log.info("Агент уже запущен — выходим")
        if args.install:
            install(platform)
        return 0

    state = AgentState.load(platform)
    if state is None:
        # Первое подключение: окно со статусом, ждём «Принять» у учителя
        state = platform.run_pairing_window(PairingFlow(platform))
        if state is None:
            log.info("Подключение отменено")
            return 1

    # Запустили старую копию, а установлена такая же или новее — запускаем установленную и выходим
    newer = None if args.after_update else newer_installed_elsewhere(platform)
    if newer is not None:
        log.info("Установлена более новая или такая же версия (%s) — запускаю её, эта копия завершается", newer)
        platform.release_single_instance()
        platform.spawn_agent(newer, [])
        return 0

    # Запустили agent.exe не из постоянного места (например, новую версию из «Загрузок») —
    # обновляем постоянную копию, чтобы автозапуск стартовал именно её
    running_elsewhere = getattr(sys, "frozen", False) and \
        Path(sys.executable).resolve() != installed_exe_path(platform).resolve()
    if args.install or running_elsewhere or not AgentState.path(platform).with_name("installed").exists():
        install(platform)
        AgentState.path(platform).with_name("installed").touch()

    # Убираем управляющие файлы полноэкранных окон, оставшиеся от прошлого запуска
    for f in (platform.data_dir() / "overlay").glob("*.active"):
        f.unlink(missing_ok=True)

    # Через минуту после старта убираем остатки прошлого обновления
    import threading
    t = threading.Timer(60, cleanup_after_update, args=(platform,))
    t.daemon = True
    t.start()

    core = AgentCore(platform)
    core.collector = Collector(platform)
    core.collector.excluded_roots = core.materials_roots
    client = AgentClient(platform, state, core=core)
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
