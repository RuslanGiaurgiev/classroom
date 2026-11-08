#!/bin/bash
# Двойной щелчок — запуск приложения учителя (временно, до сборки .app)
cd "$(dirname "$0")"
.venv/bin/python -m teacher.main
