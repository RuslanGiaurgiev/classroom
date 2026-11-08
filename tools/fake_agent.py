"""
Фейковый агент: имитирует N ноутбуков на самом Mac, чтобы проверять
приложение учителя без Windows.

Запуск (приложение учителя должно быть открыто):
    .venv/bin/python -m tools.fake_agent              — 11 ноутбуков
    .venv/bin/python -m tools.fake_agent --count 3

Ноутбуки находят учителя так же, как настоящие, и просят подключения.
По умолчанию фейковый агент сам нажимает «Принять» (через токен панели —
работает только на этом Mac). С флагом --manual принимать нужно в панели.
Команды учителя печатаются в терминал. Ctrl+C — выход.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
from pathlib import Path

import aiohttp

from agent.client import AgentClient, Backoff
from agent.pairing import PairingCancelled, PairingFlow
from agent.platform.fake import FakePlatform
from agent.state import AgentState
from teacher.storage import Storage

log = logging.getLogger("fake_agent")

BROWSER_SETS = [["chrome"], ["yandex"], ["edge"], ["chrome", "edge"], ["yandex", "edge"]]


async def auto_approve(teacher_url: str, ui_token: str, hostname: str, number: str) -> None:
    """Сами нажимаем «Принять» за учителя (режим по умолчанию)."""
    headers = {"X-UI-Token": ui_token}
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                async with s.get(f"{teacher_url}/ui/api/state", headers=headers) as r:
                    reqs = (await r.json()).get("pair_requests", [])
                for req in reqs:
                    if req["hostname"] == hostname:
                        async with s.post(f"{teacher_url}/ui/api/pair-requests/{req['id']}/approve",
                                          json={"number": number}, headers=headers) as r:
                            if r.status == 200:
                                return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.5)


async def run_laptop(number: str, root: Path, teacher_url: str, ui_token: str, port: int,
                     use_mdns: bool, manual: bool) -> None:
    rnd = random.Random(number)
    hostname = f"CLASS-PC-{number}"
    platform = FakePlatform(
        root / f"laptop-{number}",
        username=f"Ученик{number}",
        hostname=hostname,
        browsers=rnd.choice(BROWSER_SETS),
        label=f"Ноутбук {number}",
    )
    while True:
        state = AgentState.load(platform)
        if state is None:
            def show(st, n=number):
                if st.stage == "waiting":
                    print(f"Ноутбук {n}: ждёт «Принять», число для сверки {st.match_code}")
            # Ищем учителя тем же UDP-поиском, что и настоящий агент (на этом Mac — по 127.0.0.1)
            flow = PairingFlow(platform, on_status=show, discover_port=port,
                               discover_targets=["127.0.0.1", "255.255.255.255"], poll_interval=0.5)
            approver = None if manual else asyncio.create_task(auto_approve(teacher_url, ui_token, hostname, number))
            try:
                state = await flow.run()
            except PairingCancelled:
                print(f"Ноутбук {number}: учитель отклонил подключение")
                return
            finally:
                if approver:
                    approver.cancel()
            print(f"Ноутбук {number}: подключён")
        client = AgentClient(platform, state, use_mdns=use_mdns, backoff=Backoff(maximum=10))
        await client.run()
        if client.unpaired:
            print(f"Ноутбук {number}: учитель отвязал ноутбук — снова прошу подключения")
            AgentState.clear(platform)
            if not manual:
                return


async def amain(args) -> None:
    storage = Storage()  # та же папка данных, что у приложения учителя
    ui_token = storage.get_ui_token()
    teacher_url = f"http://127.0.0.1:{storage.port}"
    root = Path(args.data_dir).expanduser()
    numbers = [str(args.start + i) for i in range(args.count)]
    print(f"Фейковые ноутбуки: {', '.join(numbers)}. Данные: {root}. Ctrl+C — выход.")
    if args.manual:
        print("Режим --manual: нажимайте «Принять» в панели учителя.")
    await asyncio.gather(*(run_laptop(n, root, teacher_url, ui_token, storage.port, not args.no_mdns, args.manual)
                           for n in numbers))


def main() -> None:
    parser = argparse.ArgumentParser(description="Фейковые ноутбуки для проверки приложения учителя")
    parser.add_argument("--count", type=int, default=11, help="сколько ноутбуков (по умолчанию 11)")
    parser.add_argument("--start", type=int, default=1, help="номер первого ноутбука")
    parser.add_argument("--data-dir", default="~/Library/Application Support/ClassroomFakeAgents")
    parser.add_argument("--no-mdns", action="store_true", help="не искать учителя через mDNS")
    parser.add_argument("--manual", action="store_true", help="не подключаться автоматически: ждать «Принять» в панели")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
