"""«Что открыто»: список окон и вкладок на ноутбуке, закрытие окна или вкладки из панели учителя."""

import pytest

from agent.core import AgentCore
from agent.platform.fake import FakePlatform
from shared.protocol import Action, ProtocolError, validate_command
from tests.test_integration import start_agent, stop_agent, teacher, ui  # noqa: F401


def test_params_validated():
    assert validate_command("close_window", {"window": 5})[1] == {"window": 5, "force": False}
    assert validate_command("close_window", {"window": 5, "force": True})[1]["force"] is True
    assert validate_command("close_tab", {"window": 5, "title": " YouTube "})[1] == \
        {"window": 5, "title": " YouTube ", "index": 0}
    for action, params in [("close_window", {}), ("close_window", {"window": "5"}), ("close_window", {"window": 0}),
                           ("close_window", {"window": True}), ("close_tab", {"window": 5, "title": "x" * 301}),
                           ("close_tab", {"window": 5, "title": 3}), ("close_tab", {"window": 5, "index": -1})]:
        with pytest.raises(ProtocolError):
            validate_command(action, params)


async def test_core_list_and_close(tmp_path):
    p = FakePlatform(tmp_path)
    core = AgentCore(p)
    r = await core.execute(Action.LIST_OPEN, {})
    assert r["text"] == "окон: 2, вкладок: 2"
    assert [w["app"] for w in r["windows"]] == ["Google Chrome", "Paint"]

    assert "YouTube" in await core.execute(Action.CLOSE_TAB, {"window": 101, "title": "YouTube", "index": 1})
    assert [t["title"] for t in p.open_windows[0]["tabs"]] == ["Scratch 3.0"]
    assert await core.execute(Action.CLOSE_TAB, {"window": 101, "title": "YouTube", "index": 1}) == "вкладка уже закрыта"

    assert "Paint" in await core.execute(Action.CLOSE_WINDOW, {"window": 202, "force": False})
    with pytest.raises(ProtocolError):
        await core.execute(Action.CLOSE_WINDOW, {"window": 202, "force": False})


async def test_teacher_sees_and_closes(teacher, tmp_path):  # noqa: F811
    platform, client, task = await start_agent(teacher, tmp_path, "4")
    status, data = await ui(teacher, "GET", "/ui/api/laptops/4/open")
    assert status == 200
    chrome = data["windows"][0]
    assert chrome["browser"] and [t["title"] for t in chrome["tabs"]] == ["Scratch 3.0", "YouTube"]
    assert chrome["tabs"][0]["active"] and chrome["tabs"][0]["url"].startswith("scratch.mit.edu")

    status, r = await ui(teacher, "POST", "/ui/api/laptops/4/close",
                         json={"kind": "tab", "window": chrome["id"], "title": "YouTube", "index": 1})
    assert status == 200 and "YouTube" in r["message"]
    status, r = await ui(teacher, "POST", "/ui/api/laptops/4/close", json={"kind": "window", "window": 202})
    assert status == 200
    status, data = await ui(teacher, "GET", "/ui/api/laptops/4/open")
    assert [w["app"] for w in data["windows"]] == ["Google Chrome"] and len(data["windows"][0]["tabs"]) == 1

    # Окно уже закрыто — понятная ошибка, а не зависание
    status, r = await ui(teacher, "POST", "/ui/api/laptops/4/close", json={"kind": "window", "window": 202})
    assert status == 502 and "закрыто" in r["error"]
    # Плохие параметры не уходят на ноутбук
    status, r = await ui(teacher, "POST", "/ui/api/laptops/4/close", json={"kind": "window", "window": "x"})
    assert status == 400
    await stop_agent(client, task)


async def test_offline_and_old_agent(teacher, tmp_path):  # noqa: F811
    status, r = await ui(teacher, "GET", "/ui/api/laptops/9/open")
    assert status == 409 and "не на связи" in r["error"]

    class OldPlatform(FakePlatform):   # агент, который не умеет list_open
        list_open = FakePlatform.__bases__[0].list_open

    platform, client, task = await start_agent(teacher, tmp_path, "5",
                                               platform=OldPlatform(tmp_path / "agent-5", hostname="PC-5"))
    status, r = await ui(teacher, "GET", "/ui/api/laptops/5/open")
    assert status == 502 and "недоступен" in r["error"]
    await stop_agent(client, task)
