"""Сетевые мелочи: какой IP у Mac в локальной сети."""

from __future__ import annotations

import ipaddress
import socket


def primary_ipv4() -> str:
    """
    IP-адрес Mac в локальной сети. Трюк: «подключаем» UDP-сокет к внешнему
    адресу (пакеты при этом не отправляются) и смотрим, какой локальный
    адрес выбрала система.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def all_ipv4() -> list[str]:
    """Все не-loopback IPv4-адреса (для объявления через mDNS)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.add(primary_ipv4())
    return sorted(ip for ip in ips if not ipaddress.IPv4Address(ip).is_loopback)


def is_loopback(remote: str | None) -> bool:
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False
