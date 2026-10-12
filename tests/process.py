import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
lazy from collections.abc import Callable

lazy import aiomqtt


@dataclass
class Daemon:
    config: Path
    environment: dict[str, str] = field(repr=False)
    log_path: Path
    process: subprocess.Popen[bytes] | None = None
    _log: BinaryIO | None = field(default=None, repr=False)

    def start(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env = os.environ | self.environment | {"PYTHONPATH": str(root / "src")}
        self._log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tuya_mqtt", "run", "--config", str(self.config)],
            cwd=root,
            env=env,
            stdout=self._log,
            stderr=self._log,
        )

    @property
    def events(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.log_path.read_text().splitlines(keepends=True)
            if line.endswith("\n")
        ]

    async def wait_event(self, event: str) -> None:
        async with asyncio.timeout(10):
            while not any(item["event"] == event for item in self.events):
                assert self.process is not None
                assert self.process.poll() is None, self.log_path.read_text()
                changed = asyncio.Event()
                asyncio.get_running_loop().call_later(0.025, changed.set)
                await changed.wait()

    async def stop(self, *, abrupt: bool = False) -> int | None:
        result = None
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.send_signal(signal.SIGCONT)
                    self.process.send_signal(
                        signal.SIGKILL if abrupt else signal.SIGTERM
                    )
                try:
                    result = await asyncio.to_thread(self.process.wait, 12)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait, 5)
                    raise
                self.process = None
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None
        return result


@dataclass
class Observed:
    latest: dict[str, aiomqtt.Message] = field(default_factory=dict)
    history: list[aiomqtt.Message] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def consume(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            self.latest[str(message.topic)] = message
            self.history.append(message)
            self.changed.set()

    async def wait(self, predicate: Callable[[Observed], bool]) -> None:
        async with asyncio.timeout(30):
            while not predicate(self):
                self.changed.clear()
                if predicate(self):
                    return
                await self.changed.wait()

    def payload(self, topic: str) -> dict[str, object]:
        return json.loads(self.latest[topic].payload)

    def count(self, topic: str) -> int:
        return sum(str(message.topic) == topic for message in self.history)


async def ready(observed: Observed) -> None:
    await observed.wait(
        lambda data: (
            "tuya/bridge/heartbeat" in data.latest
            and "tuya/bed/availability" in data.latest
            and "tuya/bed/side_a" in data.latest
            and "tuya/bed/side_b" in data.latest
            and data.latest["tuya/bed/availability"].payload == b"online"
            and data.payload("tuya/bed/side_a")["session_id"]
            == data.payload("tuya/bridge/heartbeat")["session_id"]
            and data.payload("tuya/bed/side_b")["session_id"]
            == data.payload("tuya/bridge/heartbeat")["session_id"]
        )
    )


async def finish(daemon: Daemon, consumer: asyncio.Task[None]) -> int | None:
    try:
        return await daemon.stop()
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer
