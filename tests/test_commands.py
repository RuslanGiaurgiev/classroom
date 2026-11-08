"""Этап 2: команды управления, заставка, выключение, поиск, наборы урока."""

import asyncio

import aiohttp
import pytest

from agent.core import AgentCore, OverlaySupervisor
from agent.platform.fake import FakePlatform
from shared.protocol import Action, ProtocolError, validate_command
from shared.search import looks_like_url, to_url
from tests.test_integration import start_agent, stop_agent, teacher, ui, wait_for  # noqa: F401


# ---------- Поиск ----------

@pytest.mark.parametrize("text, is_url", [
    ("scratch.mit.edu", True), ("https://ya.ru", True), ("сайт.рф", True), ("192.168.1.5:8000", True),
    ("code.org/learn", True), ("раскраски динозавры", False), ("scratch", False), ("2+2", False),
    ("v1.5", False),
])
def test_url_or_search(text, is_url):
    assert looks_like_url(text) is is_url


def test_search_urls_are_safe():
    assert to_url("кот и пёс") == "https://yandex.ru/search/?family=yes&text=%D0%BA%D0%BE%D1%82+%D0%B8+%D0%BF%D1%91%D1%81"
    assert to_url("cats", "google") == "https://www.google.com/search?safe=active&q=cats"
    assert to_url("scratch.mit.edu") == "https://scratch.mit.edu"


# ---------- Проверка команд ----------

def test_command_params_validated():
    assert validate_command("show_message", {"text": " Привет "})[1] == {"text": "Привет", "seconds": 0}
    assert validate_command("lock_screen", {})[1] == {"text": "Внимание на учителя!"}
    assert validate_command("shutdown", {})[1] == {"seconds": 30}
    for action, params in [("show_message", {"text": ""}), ("show_message", {"text": "x", "seconds": -1}),
                           ("shutdown", {"seconds": 1}), ("close_apps", {"process_names": ["explorer.exe"]}),
                           ("cleanup", {"confirmed_sha256": ["abc"]})]:
        with pytest.raises(ProtocolError):
            validate_command(action, params)


# ---------- Заставка / сообщение: окно возвращается, если его закрыли ----------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_lock_restarts_if_killed(tmp_path):
    p = FakePlatform(tmp_path)
    sup = OverlaySupervisor(p)
    sup.show("lock", {"text": "Внимание"})
    p.overlays[-1].kill()          # ребёнок закрыл окно
    sup.check()
    assert len(p.overlays) == 2 and p.current_overlay().spec["mode"] == "lock"
    assert sup.clear("lock") and p.current_overlay() is None
    sup.check()
    assert len(p.overlays) == 2    # после снятия — больше не появляется


def test_message_disappears_by_timer(tmp_path):
    p, clock = FakePlatform(tmp_path), Clock()
    sup = OverlaySupervisor(p, clock=clock)
    sup.show("message", {"text": "Сохраните работу"}, seconds=10)
    clock.t = 9
    sup.check()
    assert p.current_overlay() is not None
    clock.t = 10
    sup.check()
    assert p.current_overlay() is None and sup.kind is None


def test_unlock_does_not_remove_message(tmp_path):
    p = FakePlatform(tmp_path)
    sup = OverlaySupervisor(p)
    sup.show("message", {"text": "x"})
    assert not sup.clear("lock")
    assert p.current_overlay() is not None


async def test_shutdown_countdown_and_cancel(tmp_path):
    p = FakePlatform(tmp_path)
    core = AgentCore(p)
    await core.execute(Action.SHUTDOWN, {"seconds": 1})
    assert p.current_overlay().spec["mode"] == "countdown"
    # Заставка не перекрывает отсчёт
    assert "отсчёт" in await core.execute(Action.LOCK_SCREEN, {"text": "x"})
    assert await core.execute(Action.CANCEL_SHUTDOWN, {}) == "выключение отменено"
    await asyncio.sleep(1.3)
    assert p.shutdowns == [] and p.current_overlay() is None

    await core.execute(Action.REBOOT, {"seconds": 1})
    await asyncio.sleep(1.3)
    assert p.shutdowns == [True]


async def test_close_apps(tmp_path):
    p = FakePlatform(tmp_path)
    core = AgentCore(p)
    text = await core.execute(Action.CLOSE_APPS, {"process_names": ["chrome.exe", "browser.exe"]})
    assert "chrome.exe" in text and "chrome.exe" not in p.running_processes


# ---------- Сквозные проверки через сервер учителя ----------

async def test_lock_is_restored_after_reconnect(teacher, tmp_path):
    platform, client, task = await start_agent(teacher, tmp_path, "1")
    await teacher.send_command("all", "lock_screen", {})
    await wait_for(lambda: platform.current_overlay() is not None)
    await wait_for(lambda: teacher.state_snapshot()["laptops"][0]["screen"] == "lock")
    await stop_agent(client, task)

    # Ноутбук перезагрузили: новый агент, заставки нет → учитель включает её снова
    from agent.client import AgentClient, Backoff
    from agent.state import AgentState
    platform2 = FakePlatform(tmp_path / "agent-1", hostname="PC-1")
    client2 = AgentClient(platform2, AgentState.load(platform2), use_mdns=False, backoff=Backoff(initial=0.05))
    task2 = asyncio.create_task(client2.run())
    await wait_for(lambda: platform2.current_overlay() is not None)
    assert platform2.current_overlay().spec["mode"] == "lock"

    await teacher.send_command("all", "unlock_screen", {})
    await wait_for(lambda: platform2.current_overlay() is None)
    assert teacher.locked == {}
    await stop_agent(client2, task2)


async def test_search_query_opens_search(teacher, tmp_path):
    platform, client, task = await start_agent(teacher, tmp_path, "2")
    await teacher.send_command(["2"], "open_urls", {"urls": ["мультики про роботов", "code.org"]})
    await wait_for(lambda: platform.actions)
    urls = platform.actions[0][1]
    assert urls[0].startswith("https://yandex.ru/search/?family=yes&text=") and urls[1] == "https://code.org"
    await stop_agent(client, task)


async def test_settings_api_validates(teacher):
    status, _ = await ui(teacher, "PUT", "/ui/api/settings", json={
        "apps": [{"title": "Scratch", "process": "Scratch 3.exe"}],
        "lesson_sets": [{"name": "Урок 1", "items": ["scratch.mit.edu", "мультики"], "browser": "chrome"}],
        "search_engine": "google",
    })
    assert status == 200
    st = teacher.state_snapshot()["settings"]
    assert st["search_engine"] == "google" and st["lesson_sets"][0]["name"] == "Урок 1" and st["lesson_sets"][0]["id"]
    for bad in [{"apps": [{"title": "x", "process": "explorer.exe"}]},
                {"lesson_sets": [{"name": "", "items": ["a.ru"]}]},
                {"search_engine": "bing"}]:
        status, data = await ui(teacher, "PUT", "/ui/api/settings", json=bad)
        assert status == 400 and data["error"]
