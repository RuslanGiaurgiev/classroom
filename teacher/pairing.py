"""
Подключение новых ноутбуков без ввода кодов.

1. Агент сам находит учителя в сети и присылает «запрос на подключение»
   (имя компьютера, пользователь, свой секрет запроса).
2. В панели появляется запрос с двузначным «числом для сверки» — такое же
   число крупно показано на экране ноутбука, чтобы не перепутать ноутбуки.
3. Учитель вводит номер ноутбука и нажимает «Принять». Только после этого
   ноутбук получает постоянный токен (забирает его, предъявив секрет запроса).

Без нажатия «Принять» ноутбук ничего не получает — посторонний в сети
не сможет подключиться сам.
"""

from __future__ import annotations

import hashlib
import random
import secrets
import time
import uuid
from dataclasses import dataclass, field

REQUEST_TTL_SECONDS = 15 * 60   # сколько живёт неподтверждённый запрос
MAX_PENDING = 40                # защита от засорения панели


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class PairRequest:
    id: str
    secret_hash: str
    hostname: str
    username: str
    ip: str
    match_code: str
    created_at: float
    status: str = "pending"            # pending | approved | rejected
    laptop_number: str = ""
    token: str = field(default="", repr=False)

    def public(self) -> dict:
        """Что показываем в панели."""
        return {
            "id": self.id,
            "hostname": self.hostname,
            "username": self.username,
            "ip": self.ip,
            "match_code": self.match_code,
            "created_at": self.created_at,
        }


class PairRequestManager:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._requests: dict[str, PairRequest] = {}

    def create(self, hostname: str, username: str, secret: str, ip: str) -> PairRequest:
        """Новый запрос. Повторный запрос с того же компьютера заменяет старый."""
        self._cleanup()
        for rid, r in list(self._requests.items()):
            if r.status == "pending" and r.ip == ip and r.hostname == hostname:
                del self._requests[rid]
        if sum(r.status == "pending" for r in self._requests.values()) >= MAX_PENDING:
            raise OverflowError("Слишком много неподтверждённых запросов")
        used = {r.match_code for r in self._requests.values() if r.status == "pending"}
        match_code = random.choice([f"{n:02d}" for n in range(10, 100) if f"{n:02d}" not in used])
        req = PairRequest(
            id=uuid.uuid4().hex,
            secret_hash=_h(secret),
            hostname=hostname[:100],
            username=username[:100],
            ip=ip,
            match_code=match_code,
            created_at=self._clock(),
        )
        self._requests[req.id] = req
        return req

    def pending(self) -> list[PairRequest]:
        self._cleanup()
        return sorted((r for r in self._requests.values() if r.status == "pending"), key=lambda r: r.created_at)

    def get(self, request_id: str) -> PairRequest | None:
        self._cleanup()
        return self._requests.get(request_id)

    def approve(self, request_id: str, laptop_number: str, token: str) -> PairRequest | None:
        req = self.get(request_id)
        if req is None or req.status != "pending":
            return None
        req.status, req.laptop_number, req.token = "approved", laptop_number, token
        return req

    def reject(self, request_id: str) -> bool:
        req = self.get(request_id)
        if req is None or req.status != "pending":
            return False
        req.status = "rejected"
        return True

    def poll(self, request_id: str, secret: str) -> PairRequest | None:
        """
        Агент спрашивает «меня уже приняли?». Нужен секрет запроса, который знает
        только этот агент. Выданный токен отдаётся один раз — после этого запрос удаляется.
        """
        req = self.get(request_id)
        if req is None or not secrets.compare_digest(req.secret_hash, _h(secret)):
            return None
        if req.status in ("approved", "rejected"):
            del self._requests[request_id]
        return req

    def _cleanup(self) -> None:
        now = self._clock()
        for rid, r in list(self._requests.items()):
            if now - r.created_at > REQUEST_TTL_SECONDS:
                del self._requests[rid]
