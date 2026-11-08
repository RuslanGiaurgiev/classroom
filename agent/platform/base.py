"""
Интерфейс всего, что зависит от операционной системы.

Вся логика агента (связь, протокол, очередь файлов) работает только через
этот интерфейс, поэтому её можно тестировать на Mac с FakePlatform.
Настоящая реализация для Windows — в windows.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


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

    # ---------- Диалоги (только при первой настройке, видит учитель) ----------

    @abstractmethod
    def ask_pairing_code(self, error: str | None = None) -> str | None:
        """Спросить код привязки. None — пользователь нажал «Отмена»."""

    @abstractmethod
    def show_info(self, title: str, text: str) -> None:
        """Показать сообщение."""
