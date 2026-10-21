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
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.curtain_profile import BASELINE as CURTAIN_BASELINE
from tests.test_process import configure_process
lazy from tests.broker import Broker
lazy from tests.process import Daemon
lazy from tests.tcp import Simulator


@pytest.fixture
def homekit_driver() -> Path:
    source = os.environ.get("HOBOKEN_SOURCE")
    assert source, (
        "HOBOKEN_SOURCE must name a Hoboken checkout with frozen dependencies"
    )
    checkout = Path(source).resolve()
    driver = checkout / "integration/tucson-roundtrip.ts"
    assert driver.is_file(), "Hoboken's Tucson qualification driver is required"
    return driver


@pytest.mark.parametrize("curtain", [False, True])
@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
async def test_controller_through_paired_homekit(
    *,
    curtain: bool,
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
    homekit_driver: Path,
) -> None:
    node = os.environ.get("NODE", "node")
    await broker.ready()
    if curtain:
        simulator.mock.dps.clear()
        simulator.mock.dps.update(CURTAIN_BASELINE)
        del config_data["controllers"][0]["zones"]
        config_data["controllers"][0]["profile"] = "test-curtain"
    daemon: Daemon = configure_process(
        config_path, config_data, secret_env, broker, simulator
    )
    if curtain:
        daemon.module = "tests.curtain_daemon"
    process: asyncio.subprocess.Process | None = None
    transcript: list[str] = []
    events: list[str] = []
    try:
        daemon.start()
        process = await asyncio.create_subprocess_exec(
            node,
            str(homekit_driver),
            cwd=homekit_driver.parent.parent,
            env=os.environ
            | {
                "MQTT_TEST_URL": f"mqtt://127.0.0.1:{broker.port}",
                "MQTT_TEST_CURTAIN": "1" if curtain else "0",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        async with asyncio.timeout(90):
            async for line in process.stdout:
                text = line.decode()
                transcript.append(text)
                assert sum(map(len, transcript)) <= 65536, (
                    "Qualification output exceeded its limit"
                )
                if not text.startswith("{"):
                    continue
                event = json.loads(text).get("event")
                if event == "ready_for_push":
                    assert events == []
                    assert simulator.mock.push(
                        {"201": 60, "203": "opening"}
                        if curtain
                        else {"14": False, "20": "level_3", "24": False}
                    )
                    events.append(event)
                elif event == "ready_for_shutdown":
                    assert events == ["ready_for_push"]
                    assert await daemon.stop() == 0
                    events.append(event)
                elif event == "qualification_passed":
                    events.append(event)
            assert await process.wait() == 0, "".join(transcript)
        assert events == [
            "ready_for_push",
            "ready_for_shutdown",
            "qualification_passed",
        ]
        assert simulator.writes == (
            [{"202": 20}, {"202": 100}]
            if curtain
            else [
                {"14": True, "20": "level_6", "26": "6h"},
                {"15": True, "21": "level_8", "27": "6h"},
                {"24": True},
                {"14": False},
            ]
        )
        assert all(secret not in "".join(transcript) for secret in secret_env.values())
        assert all(
            secret not in daemon.log_path.read_text() for secret in secret_env.values()
        )
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await daemon.stop()
