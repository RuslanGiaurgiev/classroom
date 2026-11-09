# Стартовый хук agent.exe (PyInstaller --runtime-hook). Выполняется ПЕРВЫМ, до хуков PyInstaller.
#
# Зачем. Собранный в один файл agent.exe при запуске распаковывается во временную папку _MEI…
# Если его запустил другой процесс agent.exe (самообновление, окно «Внимание»), новая копия
# по наследству берёт папку запустившего. Когда тот завершается, папка частично удаляется —
# пропадают данные tkinter, и стандартный хук PyInstaller падает:
#     FileNotFoundError: Tcl data directory "…\_MEI…\_tcl_data" not found.
# Так ломались окна «Внимание»/сообщения и обновление с 0.5.1.
#
# Что делаем. Если данных tkinter в нашей папке нет — перезапускаем себя как независимую
# программу (своя свежая распаковка) с теми же аргументами.


def _classroom_fresh_unpack():
    import os
    import sys

    base = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not base:
        return
    if os.environ.pop("CLASSROOM_FRESH_UNPACK", None):
        return  # мы уже перезапущенная копия — второй раз не пробуем (без бесконечного цикла)
    if os.path.isdir(os.path.join(base, "_tcl_data")) and os.path.isdir(os.path.join(base, "_tk_data")):
        return  # всё на месте — обычный запуск

    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["CLASSROOM_FRESH_UNPACK"] = "1"
    cmd = [sys.executable, *sys.argv[1:]]
    if "--after-update" in sys.argv:
        # Основной агент после обновления: запускаем отдельно и выходим (старая версия ждёт сигнала
        # готовности от новой — его подаст перезапущенная копия)
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(cmd, env=env, close_fds=True, creationflags=flags | 0x01000000)  # + BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(cmd, env=env, close_fds=True, creationflags=flags)
        os._exit(0)
    # Окно, самопроверка и т.п.: ждём копию, чтобы запустивший видел тот же процесс «живым»
    os._exit(subprocess.call(cmd, env=env, close_fds=True, creationflags=0x08000000))  # CREATE_NO_WINDOW


_classroom_fresh_unpack()
del _classroom_fresh_unpack
