"""Обновления агента: подпись, раздача через Mac, подмена файла, откат."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import shared.update as su
from agent.updater import installed_exe_path
from shared.update import UpdateError, is_newer, verify_manifest
from tests.test_files import make_core
from tests.test_integration import start_agent, stop_agent, teacher, ui, wait_for  # noqa: F401

KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setattr(su, "PUBLIC_KEY_B64", PUB)


def sign(exe: bytes, version: str, key=KEY) -> tuple[bytes, str]:
    m = json.dumps({"version": version, "sha256": hashlib.sha256(exe).hexdigest(), "size": len(exe),
                    "file": "agent.exe"}).encode()
    return m, base64.b64encode(key.sign(m)).decode()


def test_versions():
    assert is_newer("0.10.0", "0.9.9") and not is_newer("0.3.0", "0.3.0") and not is_newer("x", "0.1.0")


def test_signature_checks():
    m, sig = sign(b"exe", "1.0.0")
    assert verify_manifest(m, sig)["version"] == "1.0.0"
    with pytest.raises(UpdateError):
        verify_manifest(m.replace(b"1.0.0", b"9.0.0"), sig)              # подменили манифест
    _, other = sign(b"exe", "1.0.0", key=Ed25519PrivateKey.generate())
    with pytest.raises(UpdateError):
        verify_manifest(m, other)                                         # чужой ключ


class FakeGitHub:
    def __init__(self, version, exe, tamper=False):
        self.version, self.exe = version, exe
        self.manifest, self.sig = sign(exe, version)
        self.tamper = tamper

    def app(self):
        app = web.Application()
        app.add_routes([web.get("/{o}/{r}/releases/latest/download/{name}", self.latest),
                        web.get("/{o}/{r}/releases/download/{tag}/{name}", self.by_tag)])
        return app

    async def latest(self, request):
        name = request.match_info["name"]
        if name == "manifest.json":
            return web.Response(body=self.manifest)
        if name == "manifest.sig":
            return web.Response(body=self.sig.encode())
        raise web.HTTPNotFound()

    async def by_tag(self, request):
        if request.match_info["tag"] != f"agent-v{self.version}" or request.match_info["name"] != "agent.exe":
            raise web.HTTPNotFound()
        return web.Response(body=self.exe + (b"!" if self.tamper else b""))


async def with_github(teacher, gh):
    runner = web.AppRunner(gh.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    teacher.updates.github_base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    return runner


async def test_teacher_downloads_verified_release(teacher):
    exe = os.urandom(50_000)
    runner = await with_github(teacher, FakeGitHub("9.9.9", exe))
    try:
        r = await teacher.updates.check()
        assert r["latest"] == "9.9.9" and not r["error"]
        assert Path(teacher.updates.latest["path"]).read_bytes() == exe
    finally:
        await runner.cleanup()


async def test_teacher_rejects_tampered_release(teacher):
    runner = await with_github(teacher, FakeGitHub("9.9.9", os.urandom(1000), tamper=True))
    try:
        r = await teacher.updates.check()
        assert r["latest"] is None and "не совпала" in r["error"] or "больше" in r["error"]
    finally:
        await runner.cleanup()


async def prepare(teacher, tmp_path, new_exe=b"NEW AGENT"):
    runner = await with_github(teacher, FakeGitHub("9.9.9", new_exe))
    await teacher.updates.check()
    platform, core = make_core(tmp_path, "1")
    platform.self_update = True
    target = installed_exe_path(platform)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"OLD AGENT")
    exits = []
    core.updater.exit_fn = exits.append
    core.updater.ready_timeout = 1.5
    _, client, task = await start_agent(teacher, tmp_path, "1", core=core, platform=platform)
    return runner, platform, core, client, task, target, exits


async def test_agent_updates_and_old_version_exits(teacher, tmp_path):
    runner, platform, core, client, task, target, exits = await prepare(teacher, tmp_path)

    def new_version_starts(exe, args, proc):     # новая версия запустилась и подала сигнал
        assert args[0] == "--after-update"
        assert Path(exe).read_bytes() == b"NEW AGENT"
        Path(args[1]).write_text("ok")
    platform.on_spawn_agent = new_version_starts

    msg = await teacher.updates.push("1")
    assert "перезапускается" in msg
    await wait_for(lambda: exits == [0], timeout=5)
    assert target.read_bytes() == b"NEW AGENT"
    assert target.with_name("agent-old.exe").read_bytes() == b"OLD AGENT"
    assert platform.autorun == target
    await stop_agent(client, task)
    await runner.cleanup()


async def test_rollback_if_new_version_does_not_start(teacher, tmp_path):
    runner, platform, core, client, task, target, exits = await prepare(teacher, tmp_path)
    platform.on_spawn_agent = lambda exe, args, proc: None     # новая версия «зависла» и не подала сигнал
    await teacher.updates.push("1")
    await wait_for(lambda: core.updater.last_error, timeout=6)
    assert exits == []                                          # старая версия продолжает работать
    assert target.read_bytes() == b"OLD AGENT"
    assert target.with_name("agent-bad.exe").read_bytes() == b"NEW AGENT"
    assert platform.mutex_held
    await wait_for(lambda: "не запустилась" in teacher.state_snapshot()["laptops"][0]["update_status"], timeout=5)
    await stop_agent(client, task)
    await runner.cleanup()


async def test_update_deferred_during_lock_screen(teacher, tmp_path):
    runner, platform, core, client, task, target, exits = await prepare(teacher, tmp_path)
    await teacher.send_command("all", "lock_screen", {})
    await wait_for(lambda: core.overlay.kind == "lock")
    msg = await teacher.updates.push("1")
    assert "отложено" in msg and target.read_bytes() == b"OLD AGENT"
    await stop_agent(client, task)
    await runner.cleanup()


async def test_agent_refuses_bad_signature_and_downgrade(teacher, tmp_path):
    runner, platform, core, client, task, target, exits = await prepare(teacher, tmp_path)
    # Mac «скомпрометирован»: подсовывает файл, подписанный чужим ключом
    m, sig = sign(b"EVIL", "9.9.9", key=Ed25519PrivateKey.generate())
    teacher.updates.latest = {**teacher.updates.latest, "manifest_b64": base64.b64encode(m).decode(), "signature": sig}
    msg = await teacher.updates.push("1")
    assert "Подпись" in msg and target.read_bytes() == b"OLD AGENT"
    # Откат на старую версию агент тоже не принимает
    out = await core.updater.update("0.0.1")
    assert out["updated"] is False
    await stop_agent(client, task)
    await runner.cleanup()
