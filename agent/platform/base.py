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
