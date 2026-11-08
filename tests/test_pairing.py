"""Одноразовые коды привязки на стороне учителя."""

from teacher.pairing import CODE_TTL_SECONDS, MAX_FAILED_PER_MINUTE, PairingManager


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_code_is_one_time():
    pm = PairingManager()
    code, _ = pm.create("5", "10.0.0.2", 8765)
    assert pm.redeem(code.secret.lower()) == "5"
    assert pm.redeem(code.secret) is None


def test_code_expires_after_10_minutes():
    clock = Clock()
    pm = PairingManager(clock=clock)
    code, expires = pm.create("5", "10.0.0.2", 8765)
    assert expires == clock.t + CODE_TTL_SECONDS
    clock.t += CODE_TTL_SECONDS + 1
    assert pm.redeem(code.secret) is None


def test_new_code_for_same_number_cancels_old():
    pm = PairingManager()
    old, _ = pm.create("5", "10.0.0.2", 8765)
    new, _ = pm.create("5", "10.0.0.2", 8765)
    assert pm.redeem(old.secret) is None
    assert pm.redeem(new.secret) == "5"


def test_bruteforce_is_rate_limited():
    clock = Clock()
    pm = PairingManager(clock=clock)
    code, _ = pm.create("5", "10.0.0.2", 8765)
    for _ in range(MAX_FAILED_PER_MINUTE):
        assert pm.redeem("ZZZZZZZZ") is None
    assert pm.redeem(code.secret) is None  # заблокировано даже с верным кодом
    clock.t += 61
    assert pm.redeem(code.secret) == "5"
