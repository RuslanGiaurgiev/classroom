@echo off
chcp 65001 >nul
rem Сборка agent.exe на любом Windows-компьютере.
rem 1) Установите Python 3.12 с python.org (галочка "Add python.exe to PATH").
rem 2) Скачайте проект (zip с GitHub), распакуйте, дважды щёлкните этот файл.
rem Готовый файл появится в папке dist\agent.exe
cd /d "%~dp0\.."
py -3.12 -m venv .venv-build || goto :error
call .venv-build\Scripts\activate.bat
pip install -r requirements-agent.txt pyinstaller==6.15.0 || goto :error
pyinstaller --onefile --noconsole --clean --name agent --paths . --collect-submodules zeroconf agent\run_agent.py || goto :error
echo.
echo Готово: dist\agent.exe
pause
exit /b 0
:error
echo ОШИБКА сборки, см. сообщения выше
pause
exit /b 1
