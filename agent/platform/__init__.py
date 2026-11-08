"""Выбор реализации платформы."""

from __future__ import annotations

import sys

from agent.platform.base import Platform


def get_platform() -> Platform:
    if sys.platform == "win32":
        from agent.platform.windows import WindowsPlatform
        return WindowsPlatform()
    raise RuntimeError("Настоящий агент работает только на Windows. Для Mac используйте tools/fake_agent.py")
