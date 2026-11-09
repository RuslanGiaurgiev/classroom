"""
Фейковая платформа: для тестов и фейкового агента на Mac.
Ничего не делает с системой — только записывает, что её попросили сделать.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.platform.base import Platform
from shared.protocol import BROWSER_TITLES, PROTECTED_PROCESSES, Browser, ProtocolError

log = logging.getLogger(__name__)


class FakeOverlay:
    """Имитация окна-процесса: тест может «закрыть» его через kill()."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:  # «ребёнок закрыл окно»
        self.returncode = 1


class FakePlatform(Platform):
    def __init__(
        self,
        data_dir: Path,
        username: str = "ученик",
        hostname: str = "FAKE-PC",
        browsers: list[str] | None = None,
        label: str = "",
    ):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._username = username
        self._hostname = hostname
        self._browsers = browsers if browsers is not None else ["chrome", "edge"]
        self.label = label or hostname
        self.actions: list[tuple] = []  # журнал выполненных действий (для тестов)
        self.autorun: Path | None = None
        self.infos: list[tuple[str, str]] = []
        self.overlays: list[FakeOverlay] = []
        self.running_processes: set[str] = {"chrome.exe", "msedge.exe"}
        self.shutdowns: list[bool] = []
        self.self_update = False
        self.mutex_held = True
        self.spawned: list[tuple[Path, list[str]]] = []
        self.on_spawn_agent = None   # тест задаёт, что «делает» новая версия
        self.open_windows: list[dict] = [
            {"id": 101, "title": "Scratch 3.0 - Google Chrome", "app": "Google Chrome", "process": "chrome.exe",
             "browser": True, "tabs": [
                 {"title": "Scratch 3.0", "index": 0, "active": True, "url": "scratch.mit.edu/projects/editor"},
                 {"title": "YouTube", "index": 1, "active": False, "url": ""},
             ]},
            {"id": 202, "title": "Безымянный — Paint", "app": "Paint", "process": "mspaint.exe",
             "browser": False, "tabs": []},
        ]

    def username(self) -> str:
        return self._username

    def hostname(self) -> str:
        return self._hostname

    def data_dir(self) -> Path:
        return self._data_dir

    def known_folder(self, name: str) -> Path:
        mapping = {"downloads": "Downloads", "desktop": "Desktop", "documents": "Documents"}
        p = self._data_dir / "home" / mapping[name]
        p.mkdir(parents=True, exist_ok=True)
        return p

    # «Шифрование» для тестов: просто метка, чтобы проверить, что unprotect вызывается
    def protect(self, data: bytes) -> bytes:
        return b"FAKE:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"FAKE:"):
            raise ValueError("Данные зашифрованы не этой платформой")
        return data[5:]

    def acquire_single_instance(self) -> bool:
        self.mutex_held = True
        return True

    def install_autorun(self, exe_path: Path) -> None:
        self.autorun = exe_path

    def release_single_instance(self) -> None:
        self.mutex_held = False

    def can_self_update(self) -> bool:
        return self.self_update

    def spawn_agent(self, exe_path: Path, args: list[str]):
        self.spawned.append((exe_path, list(args)))
        proc = FakeOverlay({"agent": str(exe_path)})
        if self.on_spawn_agent:
            self.on_spawn_agent(exe_path, args, proc)
        return proc

    def uninstall_autorun(self) -> None:
        self.autorun = None

    def detect_browsers(self) -> list[str]:
        return list(self._browsers)

    def open_urls(self, urls: list[str], browser: str) -> str:
        self.actions.append(("open_urls", list(urls), browser))
        if browser != Browser.DEFAULT.value and browser not in self._browsers:
            msg = f"{BROWSER_TITLES[Browser(browser)]} не установлен — открыто в браузере по умолчанию"
        else:
            msg = f"открыто в: {BROWSER_TITLES[Browser(browser)]}"
        log.info("[%s] открыть %s (%s) → %s", self.label, urls, browser, msg)
        return msg

    def close_processes(self, names: list[str]) -> str:
        self.actions.append(("close_processes", list(names)))
        closed = [n for n in names if n.lower() in self.running_processes]
        self.running_processes -= {n.lower() for n in closed}
        log.info("[%s] закрыть %s → закрыто %s", self.label, names, closed)
        return "закрыто: " + (", ".join(closed) or "ничего не было запущено")

    # «Открытые окна»: тест может поменять список
    def list_open(self) -> dict:
        self.actions.append(("list_open",))
        return {"windows": [dict(w, tabs=[dict(t) for t in w.get("tabs", [])]) for w in self.open_windows],
                "tabs_error": ""}

    def _window(self, window: int) -> dict:
        for w in self.open_windows:
            if w["id"] == window:
                return w
        raise ProtocolError("окно уже закрыто")

    def close_window(self, window: int, force: bool) -> str:
        self.actions.append(("close_window", window, force))
        w = self._window(window)
        if w["process"].lower() in PROTECTED_PROCESSES and (force or w["process"].lower() != "explorer.exe"):
            raise ProtocolError("это окно закрывать нельзя")
        self.open_windows.remove(w)
        return f"закрыто: {w['app']}"

    def close_tab(self, window: int, title: str, index: int) -> str:
        self.actions.append(("close_tab", window, title, index))
        w = self._window(window)
        tabs = w.get("tabs", [])
        same = [i for i, t in enumerate(tabs) if t["title"] == title]
        if not same:
            return "вкладка уже закрыта"
        del tabs[min(same, key=lambda i: abs(i - index))]
        for i, t in enumerate(tabs):
            t["index"] = i
        if not tabs:
            self.open_windows.remove(w)
        return f"вкладка закрыта: {title}"

    def spawn_overlay(self, spec: dict) -> FakeOverlay:
        ov = FakeOverlay(spec)
        self.overlays.append(ov)
        log.info("[%s] на экране: %s «%s»", self.label, spec.get("mode"), spec.get("text", ""))
        return ov

    def current_overlay(self) -> FakeOverlay | None:
        live = [o for o in self.overlays if o.poll() is None]
        return live[-1] if live else None

    def shutdown(self, reboot: bool) -> None:
        self.shutdowns.append(reboot)
        log.info("[%s] %s", self.label, "ПЕРЕЗАГРУЗКА" if reboot else "ВЫКЛЮЧЕНИЕ")

    def is_hidden(self, path: Path) -> bool:
        return path.name.startswith(".")

    def run_pairing_window(self, flow):
        import asyncio

        from agent.pairing import PairingCancelled
        try:
            return asyncio.run(flow.run())
        except PairingCancelled:
            return None

    def show_info(self, title: str, text: str) -> None:
        self.infos.append((title, text))
