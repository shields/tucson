# Copyright © 2026 Michael Shields
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from typing import BinaryIO
lazy from collections.abc import Iterator
lazy from pathlib import Path

import pytest


@dataclass
class Broker:
    directory: Path
    extra: str = ""
    port: int = 0
    process: subprocess.Popen[bytes] | None = None
    _log: BinaryIO | None = field(default=None, repr=False)

    def start(self) -> None:
        data = self.directory / "data"
        data.mkdir(exist_ok=True)
        if not self.port:
            with socket.socket() as allocated:
                allocated.bind(("127.0.0.1", 0))
                self.port = allocated.getsockname()[1]
        config = self.directory / "mosquitto.conf"
        config.write_text(
            f"listener {self.port} 127.0.0.1\nallow_anonymous true\n"
            f"persistence true\npersistence_location {data}/\n"
            f"autosave_interval 1\n{self.extra}",
            encoding="utf-8",
        )
        executable = shutil.which("mosquitto")
        assert executable is not None, "Mosquitto is required for integration tests"
        self._log = (self.directory / "broker.log").open("ab")
        self.process = subprocess.Popen(
            [executable, "-c", str(config)], stdout=self._log, stderr=self._log
        )

    async def ready(self) -> None:
        for _ in range(200):
            if self.process is not None:
                assert self.process.poll() is None, (
                    self.directory / "broker.log"
                ).read_text()
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError:
                await asyncio.sleep(0.025)
            else:
                writer.close()
                await writer.wait_closed()
                return
        message = "broker did not become ready"
        raise RuntimeError(message)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None
        if self._log is not None:
            self._log.close()
            self._log = None


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    service = Broker(tmp_path)
    try:
        service.start()
        yield service
    finally:
        service.stop()
