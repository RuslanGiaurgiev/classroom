"""Протокол."""

import json

import pytest

from shared.protocol import PROTOCOL_VERSION, Message, MsgType, ProtocolError, validate_command, validate_url


def test_message_roundtrip():
    m = Message(MsgType.HELLO, {"username": "Маша"})
    m2 = Message.from_json(m.to_json())
    assert m2.type is MsgType.HELLO and m2.payload == {"username": "Маша"} and m2.id == m.id


@pytest.mark.parametrize("raw", [
    "не json",
    "[]",
    json.dumps({"type": "hello", "id": "1", "payload": {}}),                          # нет версии
    json.dumps({"v": PROTOCOL_VERSION + 1, "type": "hello", "id": "1", "payload": {}}),  # чужая версия
    json.dumps({"v": PROTOCOL_VERSION, "type": "rm -rf", "id": "1", "payload": {}}),    # неизвестный тип
    json.dumps({"v": PROTOCOL_VERSION, "type": "hello", "id": "1", "payload": []}),
])
def test_bad_messages_rejected(raw):
    with pytest.raises(ProtocolError):
        Message.from_json(raw)


def test_url_validation():
    assert validate_url("scratch.mit.edu") == "https://scratch.mit.edu"
    assert validate_url(" http://a.ru/x?y=1 ") == "http://a.ru/x?y=1"
    for bad in ["javascript:alert(1)", "file:///C:/Windows", "ftp://a.ru", "", "https://", "https://a.ru/ --flag"]:
        with pytest.raises(ProtocolError):
            validate_url(bad)


def test_only_known_actions():
    with pytest.raises(ProtocolError):
        validate_command("run_shell", {"cmd": "calc.exe"})
    with pytest.raises(ProtocolError):
        validate_command("open_urls", {"urls": []})
    with pytest.raises(ProtocolError):
        validate_command("open_urls", {"urls": ["a.ru"], "browser": "opera"})
    act, params = validate_command("open_urls", {"urls": ["a.ru"]})
    assert params == {"urls": ["https://a.ru"], "browser": "default"}
