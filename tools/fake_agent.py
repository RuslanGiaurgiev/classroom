"""
Фейковый агент: имитирует N ноутбуков на самом Mac, чтобы проверять
приложение учителя без Windows.

Запуск (приложение учителя должно быть открыто):
    .venv/bin/python -m tools.fake_agent              — 11 ноутбуков
    .venv/bin/python -m tools.fake_agent --count 3

Ноутбуки привязываются автоматически: фейковый агент сам берёт коды
у приложения учителя (через токен панели — работает только на этом Mac).
Команды учителя печатаются в терминал. Ctrl+C — выход.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
from pathlib import Path

import aiohttp

from agent.client import AgentClient, Backoff, PairingError, pair_with_code
from agent.platform.fake import FakePlatform
from agent.state import AgentState
from teacher.storage import Storage

log = logging.getLogger("fake_agent")

BROWSER_SETS = [["chrome"], ["yandex"], ["edge"], ["chrome", "edge"], ["yandex", "edge"]]


async def request_code(teacher_url: str, ui_token: str, number: str) -> str:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{teacher_url}/ui/api/pairing-codes", json={"number": number},
                          headers={"X-UI-Token": ui_token}) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(data.get("error"))
            return data["code"]


async def run_laptop(number: str, root: Path, teacher_url: str, ui_token: str, use_mdns: bool) -> None:
    rnd = random.Random(number)
    platform = FakePlatform(
        root / f"laptop-{number}",
        username=f"Ученик{number}",
        hostname=f"CLASS-PC-{number}",
        browsers=rnd.choice(BROWSER_SETS),
        label=f"Ноутбук {number}",
    )
    while True:
        state = AgentState.load(platform)
        if state is None:
            try:
                code = await request_code(teacher_url, ui_token, number)
                state = await pair_with_code(code, platform)
            except (aiohttp.ClientError, PairingError, RuntimeError) as e:
                print(f"Ноутбук {number}: не удалось привязаться ({e}). Приложение учителя запущено? Повтор через 3 с")
                await asyncio.sleep(3)
                continue
            print(f"Ноутбук {number}: привязан кодом {code}")
        client = AgentClient(platform, state, use_mdns=use_mdns, backoff=Backoff(maximum=10))
        await client.run()
        if client.unpaired:
            print(f"Ноутбук {number}: учитель отвязал ноутбук, больше не подключаюсь "
                  f"(перезапустите фейковый агент, чтобы привязать заново)")
            AgentState.clear(platform)
            return


async def amain(args) -> None:
    storage = Storage()  # та же папка данных, что у приложения учителя
    ui_token = storage.get_ui_token()
    teacher_url = f"http://127.0.0.1:{storage.port}"
    root = Path(args.data_dir).expanduser()
    numbers = [str(args.start + i) for i in range(args.count)]
    print(f"Фейковые ноутбуки: {', '.join(numbers)}. Данные: {root}. Ctrl+C — выход.")
    await asyncio.gather(*(run_laptop(n, root, teacher_url, ui_token, not args.no_mdns) for n in numbers))


def main() -> None:
    parser = argparse.ArgumentParser(description="Фейковые ноутбуки для проверки приложения учителя")
    parser.add_argument("--count", type=int, default=11, help="сколько ноутбуков (по умолчанию 11)")
    parser.add_argument("--start", type=int, default=1, help="номер первого ноутбука")
    parser.add_argument("--data-dir", default="~/Library/Application Support/ClassroomFakeAgents")
    parser.add_argument("--no-mdns", action="store_true", help="не искать учителя через mDNS")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
