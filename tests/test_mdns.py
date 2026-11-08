"""
Обнаружение учителя через mDNS. Требует работающего сетевого интерфейса,
поэтому в облачной сборке (переменная CI) пропускается.
"""

import os
import uuid

import pytest

from agent.discovery import find_teacher
from teacher.discovery import Advertiser
from teacher.netutil import all_ipv4

pytestmark = pytest.mark.skipif(bool(os.environ.get("CI")), reason="mDNS в CI недоступен")


async def test_agent_finds_teacher_by_id():
    tid = uuid.uuid4().hex[:12]
    adv = Advertiser(tid, 54321)
    await adv.start()
    try:
        found = await find_teacher(tid, timeout=4)
        assert found, "учитель не найден через mDNS"
        assert all(port == 54321 for _, port in found)
        assert {ip for ip, _ in found} & set(all_ipv4())
    finally:
        await adv.stop()


async def test_unknown_teacher_not_found():
    assert await find_teacher("nonexistent00", timeout=1) == []
