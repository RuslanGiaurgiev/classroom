"""
Интерфейс всего, что зависит от операционной системы.

Вся логика агента (связь, протокол, очередь файлов) работает только через
этот интерфейс, поэтому её можно тестировать на Mac с FakePlatform.
Настоящая реализация для Windows — в windows.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agent.pairing import PairingFlow
    from agent.state import AgentState


class OverlayHandle(Protocol):
    """Запущенное полноэкранное окно (процесс)."""

    def poll(self) -> int | None:
        """None — окно ещё открыто; число — процесс завершился."""

    def terminate(self) -> None:
        """Закрыть окно."""


class Platform(ABC):
    # ---------- Сведения о компьютере ----------

    @abstractmethod
    def username(self) -> str:
        """Имя пользователя Windows."""

    @abstractmethod
    def hostname(self) -> str:
        """Имя компьютера."""

    @abstractmethod
    def data_dir(self) -> Path:
        """Папка данных агента (%LOCALAPPDATA%\\ClassroomAgent)."""

    @abstractmethod
    def known_folder(self, name: str) -> Path:
        """Системная папка: 'downloads' или 'desktop' (через Known Folders API)."""

    # ---------- Секреты ----------

    @abstractmethod
    def protect(self, data: bytes) -> bytes:
        """Зашифровать данные для текущего пользователя (Windows DPAPI)."""

    @abstractmethod
    def unprotect(self, data: bytes) -> bytes:
        """Расшифровать данные, зашифрованные protect()."""

    # ---------- Запуск ----------

    @abstractmethod
    def acquire_single_instance(self) -> bool:
        """Захватить именованный mutex. False — агент уже запущен."""

    @abstractmethod
    def release_single_instance(self) -> None:
        """Отпустить mutex (перед запуском новой версии при обновлении)."""

    @abstractmethod
    def can_self_update(self) -> bool:
        """Можно ли обновляться (только собранный agent.exe)."""

    @abstractmethod
    def spawn_agent(self, exe_path: Path, args: list[str]) -> OverlayHandle:
        """Запустить новую версию агента отдельным процессом (не дочерним для текущего)."""

    def describe_agent_processes(self) -> str:
        """Для диагностики: какие процессы агента запущены и откуда."""
        return ""

    @abstractmethod
    def install_autorun(self, exe_path: Path) -> None:
        """Прописать автозапуск при входе пользователя (HKCU\\...\\Run)."""

    @abstractmethod
    def uninstall_autorun(self) -> None:
        """Убрать автозапуск."""

    # ---------- Действия ----------

    @abstractmethod
    def detect_browsers(self) -> list[str]:
        """Какие браузеры установлены: подмножество ['chrome', 'yandex', 'edge']."""

    @abstractmethod
    def open_urls(self, urls: list[str], browser: str) -> str:
        """
        Открыть адреса (уже проверенные: только http/https).
        Возвращает текст для журнала учителя, например «открыто в Chrome».
        """

    @abstractmethod
    def close_processes(self, names: list[str]) -> str:
        """Закрыть программы по имени процесса (имена уже проверены). Возвращает текст для журнала."""

    # ---------- Что открыто ----------

    def list_open(self) -> dict:
        """
        Что открыто на экране: окна программ (как на панели задач) и вкладки браузеров.
        {"windows": [{"id": HWND, "title", "app", "process", "browser": bool,
                      "tabs": [{"title", "index", "active", "url"}]}],
         "tabs_error": "почему вкладки не удалось прочитать" или ""}
        """
        raise NotImplementedError("Список окон на этом компьютере недоступен")

    def close_window(self, window: int, force: bool) -> str:
        """
        Закрыть окно из list_open (как крестиком). force — завершить программу этого окна.
        Нельзя трогать окна агента и системные. Отказ — ProtocolError.
        """
        raise NotImplementedError("Закрытие окон на этом компьютере недоступно")

    def close_tab(self, window: int, title: str, index: int) -> str:
        """Закрыть вкладку браузера: окно window, вкладка с таким заголовком (ближайшая к index)."""
        raise NotImplementedError("Закрытие вкладок на этом компьютере недоступно")

    @abstractmethod
    def spawn_overlay(self, spec: dict) -> OverlayHandle:
        """
        Запустить полноэкранное окно поверх всех окон отдельным процессом.
        spec["mode"]: lock | message | countdown; spec["text"]; spec["seconds"].
        """

    @abstractmethod
    def shutdown(self, reboot: bool) -> None:
        """Выключить или перезагрузить компьютер немедленно."""

    @abstractmethod
    def is_hidden(self, path: Path) -> bool:
        """Скрытый файл (атрибут «скрытый» в Windows или имя с точкой)."""

    # ---------- Диалоги (только при первой настройке, видит учитель) ----------

    @abstractmethod
    def run_pairing_window(self, flow: "PairingFlow") -> "AgentState | None":
        """
        Показать окно первого подключения и выполнить flow.run().
        Возвращает состояние после «Принять» у учителя или None, если окно закрыли.
        """

    @abstractmethod
    def show_info(self, title: str, text: str) -> None:
        """Показать сообщение."""
