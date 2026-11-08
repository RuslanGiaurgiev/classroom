"""Скрипт-обёртка для PyInstaller: собирает agent.exe из этого файла."""

import sys

from agent.main import main

if __name__ == "__main__":
    sys.exit(main())
