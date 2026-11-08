"""
Протокол обмена между Teacher (Mac) и Agent (Windows).

Все сообщения по WebSocket — JSON-объекты вида:
    {"v": 1, "type": "<тип>", "id": "<уникальный id>", "payload": {...}}

Поле "v" — версия протокола. Если у агента и учителя разные ОСНОВНЫЕ
версии, учитель закрывает соединение с понятной ошибкой (агент нужно обновить).

ВАЖНО ДЛЯ БЕЗОПАСНОСТИ: агент выполняет только действия из фиксированного
списка Action ниже. Никаких произвольных команд оболочки по сети.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse

# Версия протокола. Увеличиваем при несовместимых изменениях.
PROTOCOL_VERSION = 1

# Тип сервиса для обнаружения учителя в сети (mDNS / zeroconf)
SERVICE_TYPE = "_classroom._tcp.local."
DEFAULT_PORT = 8765

# Заголовок для токена ноутбука
AUTH_HEADER = "Authorization"


def service_name(teacher_id: str) -> str:
    """Имя mDNS-сервиса конкретного учителя (агент знает teacher_id после привязки)."""
    return f"Classroom-{teacher_id}.{SERVICE_TYPE}"


class MsgType(str, Enum):
    # Агент → учитель
    HELLO = "hello"          # первое сообщение после подключения
    STATUS = "status"        # обновление статуса (например, сколько файлов ждёт отправки)
    RESULT = "result"        # результат выполнения команды
    # Учитель → агент
    WELCOME = "welcome"      # ответ на hello
    COMMAND = "command"      # команда из фиксированного списка
    ERROR = "error"          # ошибка (например, несовместимая версия)


class Action(str, Enum):
    """Фиксированный список действий, которые агент умеет выполнять."""
    OPEN_URLS = "open_urls"


class Browser(str, Enum):
    DEFAULT = "default"   # браузер по умолчанию на этом ноутбуке
    CHROME = "chrome"
    YANDEX = "yandex"
    EDGE = "edge"


BROWSER_TITLES = {
    Browser.DEFAULT: "Браузер по умолчанию",
    Browser.CHROME: "Google Chrome",
    Browser.YANDEX: "Яндекс Браузер",
    Browser.EDGE: "Microsoft Edge",
}


class ProtocolError(ValueError):
    """Сообщение не соответствует протоколу."""


@dataclass
class Message:
    type: MsgType
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    v: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {"v": self.v, "type": self.type.value, "id": self.id, "payload": self.payload},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Message":
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ProtocolError(f"Некорректный JSON: {e}") from e
        if not isinstance(data, dict):
            raise ProtocolError("Сообщение должно быть JSON-объектом")
        v = data.get("v")
        if not isinstance(v, int):
            raise ProtocolError("Нет версии протокола")
        if v != PROTOCOL_VERSION:
            raise ProtocolError(
                f"Несовместимая версия протокола: {v}, ожидается {PROTOCOL_VERSION}"
            )
        try:
            mtype = MsgType(data.get("type"))
        except ValueError as e:
            raise ProtocolError(f"Неизвестный тип сообщения: {data.get('type')!r}") from e
        payload = data.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ProtocolError("payload должен быть объектом")
        mid = data.get("id")
        if not isinstance(mid, str) or not mid:
            raise ProtocolError("Нет id сообщения")
        return cls(type=mtype, payload=payload, id=mid, v=v)


# ---------- Проверка параметров команд ----------

MAX_URLS = 20


def validate_url(url: str) -> str:
    """Разрешаем только http/https. file:, javascript: и прочее — запрещено."""
    if not isinstance(url, str):
        raise ProtocolError("Адрес должен быть строкой")
    url = url.strip()
    if not url:
        raise ProtocolError("Пустой адрес")
    # Учитель часто пишет просто «scratch.mit.edu» — добавим https://
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ProtocolError(f"Разрешены только адреса http/https: {url}")
    # Имя сайта — только буквы, цифры, точки и дефисы (защищает и от «javascript:…» без «://»)
    try:
        port = parsed.port  # бросает ValueError, если «порт» не число
    except ValueError:
        port = -1
    host = parsed.hostname or ""
    if not host or port == -1 or not re.fullmatch(r"[\w.-]+", host):
        raise ProtocolError(f"Некорректный адрес: {url}")
    if any(ch in url for ch in ("\n", "\r", "\t", " ", '"')):
        raise ProtocolError(f"Недопустимые символы в адресе: {url}")
    return url


def validate_command(action: str, params: dict[str, Any]) -> tuple[Action, dict[str, Any]]:
    """
    Проверяет команду и возвращает нормализованные параметры.
    Используется И учителем (перед отправкой), И агентом (перед выполнением).
    """
    try:
        act = Action(action)
    except ValueError as e:
        raise ProtocolError(f"Неизвестное действие: {action!r}") from e
    if not isinstance(params, dict):
        raise ProtocolError("params должен быть объектом")

    if act is Action.OPEN_URLS:
        urls = params.get("urls")
        if not isinstance(urls, list) or not urls:
            raise ProtocolError("Нужен хотя бы один адрес")
        if len(urls) > MAX_URLS:
            raise ProtocolError(f"Не больше {MAX_URLS} вкладок за раз")
        try:
            browser = Browser(params.get("browser", Browser.DEFAULT.value))
        except ValueError as e:
            raise ProtocolError(f"Неизвестный браузер: {params.get('browser')!r}") from e
        return act, {"urls": [validate_url(u) for u in urls], "browser": browser.value}

    raise ProtocolError(f"Действие не поддерживается: {act}")  # pragma: no cover
