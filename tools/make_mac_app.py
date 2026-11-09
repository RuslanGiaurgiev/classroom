"""
Создаёт приложение «Айтишкино.app» в «Программах» — запуск панели учителя иконкой, без Терминала.

Ничего не скачивает: внутри .app лежит маленький загрузчик (сам интерпретатор Python, ~30 КБ),
а рабочая копия программы и её библиотеки — в ~/Library/Application Support/Aitishkino
(копируются из этой папки и её .venv, без интернета). Защищённые папки macOS (Рабочий стол)
приложению не нужны, а правки в этой папке не мешают рабочей версии. Поэтому:
  • в Dock и меню — «Айтишкино» со своей иконкой, ⌘Q закрывает приложение;
  • повторный запуск просто показывает уже открытое окно;
  • при каждом запуске приложение само подтягивает обновления с GitHub (если в папке
    нет своих правок) и доустанавливает библиотеки, если список изменился.

Запуск (один раз; повторный запуск пересоздаёт рабочую копию, данные учителя не трогаются):
    .venv/bin/python tools/make_mac_app.py
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_NAME = "Айтишкино"
BUNDLE_ID = "ru.aitishkino.classroom-teacher"
EXEC_NAME = "Aitishkino"          # скрипт запуска
RUNTIME_NAME = "Aitishkino-runtime"  # копия загрузчика Python (чтобы процесс принадлежал нашему .app)
REPO = Path(__file__).resolve().parents[1]
APP_HOME = Path.home() / "Library" / "Application Support" / "Aitishkino"   # рабочая копия программы

LAUNCHER = r'''#!/bin/bash
# Запуск «Айтишкино». Создано tools/make_mac_app.py — не редактируйте вручную.
APP_HOME=__APP_HOME__
REPO="$APP_HOME/app"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/Айтишкино.log"
mkdir -p "$HOME/Library/Logs"
exec >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') запуск"

fail() {
  /usr/bin/osascript -e "display alert \"Айтишкино\" message \"$1\" as critical" >/dev/null 2>&1
  exit 1
}

cd "$REPO" 2>/dev/null || fail "Не найдена программа ($REPO). Запустите tools/make_mac_app.py ещё раз."
VENV_PY="$APP_HOME/venv/bin/python"
[ -x "$VENV_PY" ] || fail "Не найдено окружение Python ($APP_HOME/venv). Запустите tools/make_mac_app.py ещё раз."

# --- Обновление с GitHub: только если в папке нет своих правок; без интернета — пропускаем ---
if [ -d .git ] && [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  BEFORE=$(git rev-parse HEAD 2>/dev/null)
  GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=8 pull --ff-only -q origin main \
    && AFTER=$(git rev-parse HEAD 2>/dev/null)
  if [ -n "$AFTER" ] && [ "$BEFORE" != "$AFTER" ]; then
    echo "обновлено: $BEFORE → $AFTER"
    if ! git diff --quiet "$BEFORE" "$AFTER" -- requirements-teacher.txt; then
      "$VENV_PY" -m pip install -q -r requirements-teacher.txt || echo "не удалось обновить библиотеки"
    fi
  fi
fi

# --- Загрузчик Python внутри .app. Сам .app не меняем (иначе macOS заново спрашивает разрешения);
#     если Python обновили через Homebrew и загрузчик не запускается — работаем через .venv ---
RUNTIME="$HERE/__RUNTIME__"
SITE=$("$VENV_PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)
export AITISHKINO_REPO="$REPO" AITISHKINO_SITE="$SITE"
if [ -x "$RUNTIME" ] && "$RUNTIME" -c 'import sys' 2>/dev/null; then
  exec "$RUNTIME" "$HERE/../Resources/launch.py" "$@"
fi
# Запасной путь: обычный Python (работает, но в Dock будет значок Python).
# Чтобы вернуть значок «Айтишкино» — запустите tools/make_mac_app.py ещё раз.
echo "загрузчик в .app не запускается — запуск через venv"
exec "$VENV_PY" -m teacher.main "$@"
'''

LAUNCH_PY = '''"""Запуск teacher.main с библиотеками из .venv проекта (создано tools/make_mac_app.py)."""
import os
import runpy
import site
import sys

site.addsitedir(os.environ["AITISHKINO_SITE"])        # библиотеки из .venv (с .pth-файлами)
sys.path.insert(0, os.environ["AITISHKINO_REPO"])     # код приложения
sys.argv = ["teacher.main", *[a for a in sys.argv[1:] if not a.startswith("-psn_")]]
runpy.run_module("teacher.main", run_name="__main__", alter_sys=True)
'''


def make_icon(dest_icns: Path) -> None:
    """Круглый логотип → квадратная иконка .icns (прозрачные поля), средствами macOS."""
    from AppKit import NSBezierPath, NSBitmapImageRep, NSGraphicsContext, NSImage, NSPNGFileType
    from Foundation import NSMakeRect

    logo = NSImage.alloc().initWithContentsOfFile_(str(REPO / "teacher" / "web" / "logo.png"))
    if logo is None:
        raise RuntimeError("нет teacher/web/logo.png")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                    None, px, px, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0)
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
                # Круг логотипа — нижний квадрат картинки (сверху белое поле), фон вокруг круга белый:
                # берём этот квадрат и обрезаем по кругу
                w, h = logo.size()
                side = min(w, h)
                d = px * 0.92
                dest = NSMakeRect((px - d) / 2, (px - d) / 2, d, d)
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(dest.origin.x + 1, dest.origin.y + 1, d - 2, d - 2)).addClip()
                logo.drawInRect_fromRect_operation_fraction_(dest, NSMakeRect((w - side) / 2, 0, side, side), 2, 1.0)  # 2 = SourceOver
                NSGraphicsContext.restoreGraphicsState()
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(str(iconset / name), True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest_icns)], check=True)


def build(app: Path) -> Path:
    base = Path(sys.base_prefix)
    runtime_src = base / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    if not runtime_src.exists():
        raise SystemExit(f"Не найден загрузчик Python: {runtime_src}\nЗапустите через .venv/bin/python (Python с python.org или Homebrew).")
    tmp_app = app.with_name(app.name + ".new")
    shutil.rmtree(tmp_app, ignore_errors=True)
    macos, res = tmp_app / "Contents" / "MacOS", tmp_app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)

    from agent import __version__ as agent_version  # версия для «О программе»
    with open(tmp_app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleExecutable": EXEC_NAME,
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": agent_version,
            "CFBundleVersion": agent_version,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.education",
            # Ноутбуки находят учителя через Bonjour и подключаются по локальной сети
            "NSLocalNetworkUsageDescription": "Чтобы ноутбуки учеников находили компьютер учителя и подключались к нему.",
            "NSBonjourServices": ["_classroom._tcp"],
        }, f)

    launcher = LAUNCHER.replace("__APP_HOME__", _sh_quote(str(APP_HOME))).replace("__RUNTIME__", RUNTIME_NAME)
    (macos / EXEC_NAME).write_text(launcher, encoding="utf-8")
    os.chmod(macos / EXEC_NAME, 0o755)
    shutil.copy2(runtime_src, macos / RUNTIME_NAME)
    (res / "launch.py").write_text(LAUNCH_PY, encoding="utf-8")
    make_icon(res / "AppIcon.icns")

    shutil.rmtree(app, ignore_errors=True)
    tmp_app.rename(app)
    # Подписать «на месте» (ad-hoc), чтобы macOS не ругалась на изменённый пакет; ошибки не критичны
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], capture_output=True)
    # Обновить иконку в Finder/Dock
    subprocess.run(["touch", str(app)])
    lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if os.path.exists(lsregister):
        subprocess.run([lsregister, "-f", str(app)], capture_output=True)
    return app


def install_copy() -> None:
    """
    Рабочая копия программы в APP_HOME: код (git clone из этой папки — без интернета; обновления
    потом приходят с GitHub) и библиотеки (копия .venv). Данные учителя здесь не хранятся.
    """
    APP_HOME.mkdir(parents=True, exist_ok=True)
    app, venv = APP_HOME / "app", APP_HOME / "venv"
    for d in (app.with_name("app.new"), venv.with_name("venv.new")):
        shutil.rmtree(d, ignore_errors=True)
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(REPO), str(app.with_name("app.new"))], check=True)
    origin = subprocess.run(["git", "-C", str(REPO), "remote", "get-url", "origin"],
                            capture_output=True, text=True).stdout.strip()
    if origin:
        subprocess.run(["git", "-C", str(app.with_name("app.new")), "remote", "set-url", "origin", origin], check=True)
    shutil.copytree(REPO / ".venv", venv.with_name("venv.new"), symlinks=True)
    for d, new in ((app, app.with_name("app.new")), (venv, venv.with_name("venv.new"))):
        shutil.rmtree(d, ignore_errors=True)
        new.rename(d)


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main() -> None:
    target_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Applications")
    if not os.access(target_dir, os.W_OK):
        target_dir = Path.home() / "Applications"
        target_dir.mkdir(exist_ok=True)
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("Внимание: в папке есть незакоммиченные правки — в приложение попадёт только последний коммит.")
    install_copy()
    app = build(target_dir / f"{APP_NAME}.app")
    print(f"Готово: {app}\nЗапуск — из «Программ» или Launchpad. Чтобы закрепить в Dock: правый щелчок по значку → «Параметры» → «Оставить в Dock».")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    main()
