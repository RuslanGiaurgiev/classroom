"""Скрипт-обёртка для PyInstaller: собирает «Айтишкино.exe» (приложение учителя) из этого файла."""

import sys

from teacher.main import main

if __name__ == "__main__":
    sys.exit(main())
