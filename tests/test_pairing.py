"""Запросы на подключение (учитель) и разбор вручную введённого адреса (агент)."""

import pytest

from agent.pairing import parse_manual_address
from teacher.pairing import REQUEST_TTL_SECONDS, PairRequestManager


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_request_lifecycle():
    pm = PairRequestManager()
    r = pm.create("PC-1", "Маша", "secret-123456789", "10.0.0.5")
    assert len(r.match_code) == 2 and pm.pending() == [r]
    assert pm.poll(r.id, "secret-123456789").status == "pending"
    assert pm.poll(r.id, "wrong") is None
    pm.approve(r.id, "5", "TOKEN")
    assert pm.pending() == []
    got = pm.poll(r.id, "secret-123456789")
    assert (got.status, got.token, got.laptop_number) == ("approved", "TOKEN", "5")
    assert pm.poll(r.id, "secret-123456789") is None  # токен отдаётся один раз


def test_cannot_approve_twice_or_after_reject():
    pm = PairRequestManager()
    r = pm.create("PC-1", "", "s" * 20, "10.0.0.5")
    assert pm.reject(r.id)
    assert pm.approve(r.id, "1", "T") is None


def test_repeat_request_from_same_pc_replaces_old():
    pm = PairRequestManager()
    r1 = pm.create("PC-1", "", "s" * 20, "10.0.0.5")
    r2 = pm.create("PC-1", "", "t" * 20, "10.0.0.5")
    assert pm.pending() == [r2] and pm.get(r1.id) is None


def test_match_codes_unique_among_pending():
    pm = PairRequestManager()
    codes = {pm.create(f"PC-{i}", "", "s" * 20, f"10.0.0.{i}").match_code for i in range(30)}
    assert len(codes) == 30


def test_requests_expire():
    clock = Clock()
    pm = PairRequestManager(clock=clock)
    r = pm.create("PC-1", "", "s" * 20, "10.0.0.5")
    clock.t += REQUEST_TTL_SECONDS + 1
    assert pm.pending() == [] and pm.approve(r.id, "1", "T") is None


@pytest.mark.parametrize("text, expected", [
    ("192.168.3.96", ("192.168.3.96", 8765)),
    (" 192.168.3.96 ", ("192.168.3.96", 8765)),
    ("192,168,3,96", ("192.168.3.96", 8765)),
    ("192ю168ю3ю96", ("192.168.3.96", 8765)),          # русская раскладка
    ("http://192.168.3.96:8765/", ("192.168.3.96", 8765)),
    ("192.168.3.96:9000", ("192.168.3.96", 9000)),
    ("192.168.3", None),
    ("300.1.1.1", None),
    ("mac.local", None),
    ("", None),
])
def test_manual_address(text, expected):
    assert parse_manual_address(text) == expected
