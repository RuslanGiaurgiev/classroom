"""
Сбор проектов на ноутбуке.

  • watchdog сообщает об изменениях в папках, плюс полный обход раз в 30 секунд
    (на случай пропущенных событий) и при запуске (файлы, появившиеся, пока агент
    был выключен);
  • файл отправляется, когда он «готов» (см. ReadinessTracker);
  • учёт — в SQLite (Ledger); если Mac недоступен, файлы ждут в очереди;
  • отправка — HTTP с докачкой; учитель подтверждает только после проверки SHA-256;
  • «Конец урока»: в корзину агента переносятся ТОЛЬКО подтверждённые файлы.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from agent.files import (
    NOISY_STABLE_SECONDS,
    NOISY_VERSIONS,
    NOISY_WINDOW,
    ReadinessTracker,
    sha256_file,
    should_ignore,
)
from agent.ledger import FileRow, Ledger
from agent.platform.base import Platform
from shared.ignore import clean_patterns

if TYPE_CHECKING:
    from agent.client import AgentClient

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "folders": [{"base": "downloads", "sub": ""}, {"base": "desktop", "sub": "Проекты"}],
    "max_file_mb": 500,
    "ignore": [],
}
FOLDER_BASES = ("downloads", "desktop", "documents")
CHUNK = 4 * 1024 * 1024          # размер куска при отправке
FULL_SCAN_EVERY = 30.0           # полный обход папок, секунд
TRASH_DAYS = 7


def folder_key(spec: dict) -> str:
    sub = spec.get("sub", "").strip("\\/ ")
    return f"{spec['base']}/{sub}" if sub else spec["base"]


def validate_config(config: dict) -> dict:
    """Проверить настройки от учителя (агент не доверяет им вслепую)."""
    folders = []
    for f in config.get("folders", [])[:10]:
        base = f.get("base")
        sub = str(f.get("sub", "")).strip().strip("\\/")
        if base not in FOLDER_BASES:
            continue
        parts = Path(sub.replace("\\", "/")).parts if sub else ()
        if any(p in ("..", ".") or ":" in p for p in parts):
            continue  # никаких выходов за пределы системной папки
        folders.append({"base": base, "sub": "/".join(parts)})
    max_mb = config.get("max_file_mb", DEFAULT_CONFIG["max_file_mb"])
    if not isinstance(max_mb, int) or not 1 <= max_mb <= 4096:
        max_mb = DEFAULT_CONFIG["max_file_mb"]
    return {"folders": folders or DEFAULT_CONFIG["folders"], "max_file_mb": max_mb,
            "ignore": clean_patterns(config.get("ignore", []))}


class Collector:
    def __init__(self, platform: Platform, ledger: Ledger | None = None, tracker: ReadinessTracker | None = None,
                 use_watchdog: bool = True, full_scan_every: float = FULL_SCAN_EVERY):
        self.platform = platform
        self.ledger = ledger if ledger is not None else Ledger(platform.data_dir() / "files.sqlite3")
        self.tracker = tracker if tracker is not None else ReadinessTracker()
        self.use_watchdog = use_watchdog
        self.full_scan_every = full_scan_every
        self.client: AgentClient | None = None
        self.config = self._load_config()
        self._wake = asyncio.Event()
        self._upload_lock = asyncio.Lock()
        self._observer = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_full_scan = 0.0
        self._last_pending_sent: int | None = None
        self._folders_cache: dict[str, Path] | None = None
        self._noisy_logged: set[str] = set()

    # ================= Настройки =================

    @property
    def _config_path(self) -> Path:
        return self.platform.data_dir() / "collect_config.json"

    def _load_config(self) -> dict:
        try:
            return validate_config(json.loads(self._config_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return validate_config(DEFAULT_CONFIG)

    def apply_config(self, config: dict) -> None:
        new = validate_config(config)
        if new == self.config:
            return
        self.config = new
        self._folders_cache = None
        self._config_path.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
        log.info("Новые настройки сбора: %s", new)
        self._restart_watchdog()
        self.wake(full_scan=True)

    @property
    def max_bytes(self) -> int:
        return self.config["max_file_mb"] * 1024 * 1024

    def folders(self, refresh: bool = False) -> dict[str, Path]:
        """Отслеживаемые папки: ключ → путь. Папки вида «Рабочий стол\\Проекты» создаём, если их нет."""
        if self._folders_cache is not None and not refresh:
            return dict(self._folders_cache)
        result = {}
        for spec in self.config["folders"]:
            try:
                base = self.platform.known_folder(spec["base"])
            except OSError:
                log.exception("Нет системной папки %s", spec["base"])
                continue
            path = base / spec["sub"] if spec["sub"] else base
            if spec["sub"] and not path.exists() and base.exists():
                try:
                    path.mkdir(parents=True, exist_ok=True)
                    log.info("Создана папка %s", path)
                except OSError:
                    log.exception("Не удалось создать %s", path)
            if path.is_dir():
                result[folder_key(spec)] = path
        self._folders_cache = result
        return dict(result)

    # ================= Запуск =================

    def start_background(self) -> list[asyncio.Task]:
        self._loop = asyncio.get_running_loop()
        if self.ledger.get_meta("baseline_done") is None:
            self.make_baseline()
        self._restart_watchdog()
        self.purge_trash()
        return [asyncio.create_task(self._scan_loop()), asyncio.create_task(self._upload_loop())]

    def make_baseline(self) -> int:
        """
        Первый запуск: всё, что уже лежит в папках, — «старое». Не отправляем и не удаляем.
        """
        n = 0
        for key, root, rel, st in self._walk():
            self.ledger.add_baseline(key, rel.as_posix(), st.st_size, st.st_mtime_ns)
            n += 1
        self.ledger.set_meta("baseline_done", str(time.time()))
        log.info("Первый запуск: %s старых файлов отмечены как «не отправлять»", n)
        return n

    def wake(self, full_scan: bool = False) -> None:
        if full_scan:
            self._last_full_scan = 0.0
        self._wake.set()

    # ================= Слежение за папками =================

    def _restart_watchdog(self) -> None:
        if not self.use_watchdog:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog не установлен — только периодический обход")
            return
        if self._observer is not None:
            self._observer.stop()
            self._observer = None

        collector = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory:
                    return
                path = getattr(event, "dest_path", None) or event.src_path
                if collector._loop is not None:
                    collector._loop.call_soon_threadsafe(collector._on_fs_event, Path(path))

        obs = Observer()
        for path in self.folders().values():
            try:
                obs.schedule(Handler(), str(path), recursive=True)
            except OSError:
                log.exception("Не удалось следить за %s", path)
        obs.daemon = True
        obs.start()
        self._observer = obs

    def _on_fs_event(self, path: Path) -> None:
        loc = self._locate(path)
        if loc is None:
            return
        try:
            st = path.stat()
        except OSError:
            self.tracker.forget(path)
            return
        key, rel = loc
        if not path.is_file() or should_ignore(rel, st.st_size, self.max_bytes, self.platform.is_hidden, path, self.config["ignore"]):
            return
        if not self.ledger.is_known(key, rel.as_posix(), st.st_size, st.st_mtime_ns):
            self.tracker.observe(path, st.st_size, st.st_mtime_ns)
            self._wake.set()

    def _locate(self, path: Path) -> tuple[str, Path] | None:
        """В какой отслеживаемой папке лежит файл (самая глубокая подходящая)."""
        best = None
        for key, root in self.folders().items():
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if best is None or len(root.parts) > len(best[2].parts):
                best = (key, rel, root)
        return (best[0], best[1]) if best else None

    def _walk(self):
        """Все подходящие файлы во всех папках: (ключ, корень, относительный путь, stat)."""
        folders = self.folders()
        # Если одна папка лежит внутри другой — файл относим к более глубокой
        for key, root in folders.items():
            nested = [p for k, p in folders.items() if k != key and p != root and root in p.parents]
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                dirnames[:] = [n for n in dirnames if not n.startswith(".") and d / n not in nested
                               and not self.platform.is_hidden(d / n)]
                for name in filenames:
                    full = d / name
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    rel = full.relative_to(root)
                    if should_ignore(rel, st.st_size, self.max_bytes, self.platform.is_hidden, full, self.config["ignore"]):
                        continue
                    yield key, root, rel, st

    def full_scan(self) -> int:
        """Найти новые (ещё не учтённые) файлы и поставить их на проверку готовности."""
        self.folders(refresh=True)  # папку могли удалить/создать
        n = 0
        for key, root, rel, st in self._walk():
            full = root / rel
            if not self.ledger.is_known(key, rel.as_posix(), st.st_size, st.st_mtime_ns):
                self.tracker.observe(full, st.st_size, st.st_mtime_ns)
                n += 1
        self._last_full_scan = time.monotonic()
        return n

    async def process_candidates(self) -> int:
        """Проверить кандидатов: изменились ли, готовы ли. Готовые — посчитать SHA-256 и записать."""
        added = 0
        for path in self.tracker.candidates():
            loc = self._locate(path)
            try:
                st = path.stat()
            except OSError:
                self.tracker.forget(path)  # файл удалили или переименовали
                continue
            if loc is None:
                self.tracker.forget(path)
                continue
            self.tracker.observe(path, st.st_size, st.st_mtime_ns)
            key, rel = loc
            # «Шумный» файл (меняется каждые несколько секунд — журнал программы и т.п.):
            # отправляем, только когда он перестанет меняться на 2 минуты
            need = None
            if self.ledger.recent_versions(key, rel.as_posix(), NOISY_WINDOW) >= NOISY_VERSIONS:
                need = max(NOISY_STABLE_SECONDS, self.tracker.stable_seconds)
                if rel.as_posix() not in self._noisy_logged:
                    self._noisy_logged.add(rel.as_posix())
                    log.warning("Файл %s/%s постоянно меняется — отправлю, когда он успокоится", key, rel)
            if not self.tracker.is_ready(path, need):
                continue
            try:
                sha = await asyncio.to_thread(sha256_file, path)
                st2 = path.stat()
            except OSError:
                continue  # файл заняли — попробуем позже
            if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                continue  # файл изменился, пока считали хэш
            self.tracker.forget(path)
            status = self.ledger.add_found(key, rel.as_posix(), st.st_size, st.st_mtime_ns, sha)
            if status == "pending":
                added += 1
                log.info("Новый файл: %s/%s (%s байт)", key, rel.as_posix(), st.st_size)
            elif status == "confirmed":
                log.info("Файл %s/%s уже был получен учителем (такое же содержимое) — не отправляю", key, rel)
        return added

    async def _scan_loop(self) -> None:
        while True:
            try:
                if time.monotonic() - self._last_full_scan >= self.full_scan_every:
                    await asyncio.to_thread(self.full_scan)
                if await self.process_candidates():
                    self._wake.set()
                await self._report_pending()
            except Exception:
                log.exception("Ошибка обхода папок")
            await asyncio.sleep(1.0)

    # ================= Отправка =================

    def is_uploading(self) -> bool:
        return self._upload_lock.locked()

    def pending_count(self) -> int:
        return self.ledger.pending_count() + len(self.tracker)

    async def _report_pending(self) -> None:
        n = self.pending_count()
        if n != self._last_pending_sent and self.client is not None:
            self._last_pending_sent = n
            await self.client.send_status()

    def _file_path(self, row: FileRow) -> Path | None:
        root = self.folders().get(row.folder_key)
        return root / row.rel_path if root else None

    async def _upload_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.upload_pending()
            except Exception:
                log.exception("Ошибка отправки")

    async def upload_pending(self) -> int:
        """Отправить всё из очереди. Возвращает число подтверждённых файлов."""
        async with self._upload_lock:
            if self.client is None or not self.client.base_url:
                return 0
            done = 0
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=60)
            headers = {"Authorization": f"Bearer {self.client.state.token}"}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for row in self.ledger.pending():
                    base = self.client.base_url
                    if not base:
                        break
                    try:
                        if await self._upload_one(session, base, row):
                            done += 1
                            await self._report_pending()
                    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
                        log.info("Отправка прервана (%s) — продолжу, когда будет связь", e)
                        break
            return done

    async def _upload_one(self, session: aiohttp.ClientSession, base: str, row: FileRow) -> bool:
        path = self._file_path(row)
        try:
            st = path.stat() if path else None
        except OSError:
            st = None
        if st is None:
            self.ledger.set_status(row.id, "gone", "файл удалён до отправки")
            log.warning("Файл %s/%s пропал до отправки", row.folder_key, row.rel_path)
            return False
        reason = should_ignore(Path(row.rel_path), st.st_size, self.max_bytes, self.platform.is_hidden, path, self.config["ignore"])
        if reason:
            # Правила игнорирования могли измениться (новая версия агента) — не отправляем
            self.ledger.set_status(row.id, "ignored", reason)
            return False
        if (st.st_size, st.st_mtime_ns) != (row.size, row.mtime_ns):
            # Файл изменили после того, как мы его нашли: эту версию не шлём, новую найдёт обход
            self.ledger.set_status(row.id, "superseded", "файл изменён")
            self.wake(full_scan=True)
            return False

        meta = {"sha256": row.sha256, "size": row.size, "name": Path(row.rel_path).name,
                "rel_path": row.rel_path, "folder_key": row.folder_key, "mtime_ns": row.mtime_ns}
        async with session.post(f"{base}/api/files/init", json=meta) as r:
            data = await r.json(content_type=None)
            r.raise_for_status()
        if data.get("status") == "done":
            self.ledger.set_status(row.id, "confirmed")
            self.ledger.mark_confirmed_by_sha(row.sha256)
            return True

        offset = int(data.get("offset", 0))
        with open(path, "rb") as f:
            f.seek(offset)
            while offset < row.size:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                async with session.put(f"{base}/api/files/{row.sha256}", params={"offset": str(offset)}, data=chunk) as r:
                    data = await r.json(content_type=None)
                    if r.status == 409:      # учитель ждёт другое смещение — продолжаем с него
                        offset = int(data["offset"])
                        f.seek(offset)
                        continue
                    r.raise_for_status()
                    offset = int(data["offset"])

        async with session.post(f"{base}/api/files/{row.sha256}/complete") as r:
            data = await r.json(content_type=None)
            if r.status == 422:
                log.warning("Учитель не подтвердил %s: %s — отправлю заново", row.rel_path, data.get("error"))
                return False
            r.raise_for_status()
        if data.get("status") == "done":
            self.ledger.set_status(row.id, "confirmed")
            self.ledger.mark_confirmed_by_sha(row.sha256)
            log.info("Подтверждено учителем: %s/%s", row.folder_key, row.rel_path)
            return True
        return False

    async def flush(self) -> None:
        """Найти и отправить всё, что можно, прямо сейчас (перед выключением, по кнопке)."""
        await asyncio.to_thread(self.full_scan)
        await self.process_candidates()
        await self.upload_pending()

    async def collect_now(self, wait_ready: float = 20.0) -> dict:
        """
        «Собрать сейчас»: обойти папки, подождать, пока файлы станут готовыми
        (не дольше wait_ready секунд), отправить всё. Возвращает итог для учителя.
        """
        await asyncio.to_thread(self.full_scan)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_ready
        while True:
            await self.process_candidates()
            if not len(self.tracker) or loop.time() >= deadline:
                break
            await asyncio.sleep(0.5)
        sent = await self.upload_pending()
        not_ready = [p.name for p in self.tracker.candidates()]
        pending = self.ledger.pending_count()
        await self._report_pending()
        return {"sent": sent, "pending": pending, "not_ready": not_ready}

    # ================= «Конец урока»: безопасная очистка =================

    def trash_dir(self) -> Path:
        return self.platform.data_dir() / "trash"

    def cleanup_sync(self, confirmed_by_teacher: set[str]) -> dict:
        """
        Перенести в корзину ТОЛЬКО те файлы, которые:
          1) в нашем учёте подтверждены учителем (status=confirmed),
          2) учитель прямо сейчас ещё раз назвал их SHA-256 в списке полученных,
          3) не изменились с момента отправки (размер, время И заново посчитанный SHA-256).
        Всё остальное остаётся на месте.
        """
        moved, kept = 0, []
        today = date.today().isoformat()
        folders = self.folders()
        for row in self.ledger.confirmed():
            root = folders.get(row.folder_key)
            if root is None or not row.sha256:
                continue
            path = root / row.rel_path
            if row.sha256 not in confirmed_by_teacher:
                kept.append(f"{row.rel_path}: учитель не подтвердил")
                continue
            try:
                st = path.stat()
            except OSError:
                self.ledger.set_status(row.id, "trashed", "файла уже нет")
                continue
            if (st.st_size, st.st_mtime_ns) != (row.size, row.mtime_ns):
                kept.append(f"{row.rel_path}: изменён после отправки")
                continue
            try:
                if sha256_file(path) != row.sha256:
                    kept.append(f"{row.rel_path}: содержимое изменилось")
                    continue
                dest = self._unique(self.trash_dir() / today / row.folder_key.replace("/", "_") / row.rel_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dest))
            except OSError as e:
                kept.append(f"{row.rel_path}: не удалось переместить ({e})")
                continue
            self.ledger.set_status(row.id, "trashed", str(dest))
            moved += 1
            self._remove_empty_dirs(path.parent, root)
        log.info("Очистка: перемещено в корзину %s, оставлено %s", moved, len(kept))
        return {"moved": moved, "kept": kept}

    async def cleanup(self, confirmed_by_teacher: set[str]) -> dict:
        return await asyncio.to_thread(self.cleanup_sync, confirmed_by_teacher)

    @staticmethod
    def _unique(path: Path) -> Path:
        if not path.exists():
            return path
        i = 2
        while True:
            p = path.with_name(f"{path.stem} ({i}){path.suffix}")
            if not p.exists():
                return p
            i += 1

    @staticmethod
    def _remove_empty_dirs(d: Path, root: Path) -> None:
        """Убрать опустевшие подпапки (саму отслеживаемую папку не трогаем)."""
        while d != root and root in d.parents:
            try:
                d.rmdir()  # удаляет только пустую папку
            except OSError:
                return
            d = d.parent

    def purge_trash(self, days: int = TRASH_DAYS) -> None:
        """Корзина агента хранит удалённое 7 дней."""
        trash = self.trash_dir()
        if not trash.is_dir():
            return
        limit = date.today() - timedelta(days=days)
        for d in trash.iterdir():
            try:
                if d.is_dir() and datetime.strptime(d.name, "%Y-%m-%d").date() < limit:
                    shutil.rmtree(d, ignore_errors=True)
                    log.info("Корзина: удалена папка %s", d.name)
            except ValueError:
                continue
