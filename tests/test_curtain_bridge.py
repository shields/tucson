import asyncio
import json
lazy from pathlib import Path
lazy from typing import Any

import aiomqtt
import pytest

from .curtain_profile import BASELINE
from .process import Observed, finish
from .test_process import configure_process, http_snapshot
lazy from .broker import Broker
lazy from .tcp import Simulator

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
async def test_curtain_through_tcp_and_mqtt(
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await broker.ready()
    simulator.mock.dps.clear()
    simulator.mock.dps.update(BASELINE)
    config_data["controllers"][0] |= {"profile": "test-curtain", "zones": ["curtain"]}
    daemon = configure_process(config_path, config_data, secret_env, broker, simulator)
    daemon.module = "tests.curtain_daemon"
    observed = Observed()
    topic = "tuya/bed/curtain"
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as client:
        await client.subscribe("tuya/#", qos=1)
        consume = asyncio.create_task(observed.consume(client))
        try:
            daemon.start()
            await observed.wait(
                lambda view: (
                    "tuya/bed/availability" in view.latest
                    and view.latest["tuya/bed/availability"].payload == b"online"
                )
            )
            assert observed.payload(topic)["current_position"] == 0
            request = {"request_id": "curtain-target", "target_position": 80}
            await client.publish(f"{topic}/set", json.dumps(request))
            await observed.wait(lambda view: f"{topic}/result" in view.latest)
            assert observed.payload(f"{topic}/result") == {
                "request_id": "curtain-target",
                "outcome": "confirmed",
            }
            assert observed.payload(topic)["target_position"] == 80
            assert observed.payload(topic)["current_position"] == 0
            assert simulator.writes == [{"202": 20}]
            previous = observed.count(f"{topic}/result")
            await client.publish(f"{topic}/set", json.dumps(request))
            await observed.wait(lambda view: view.count(f"{topic}/result") > previous)
            assert len(simulator.writes) == 1
            assert simulator.mock.push({"201": 60, "203": "opening"})
            await observed.wait(
                lambda view: view.payload(topic)["current_position"] == 40
            )
            status = await http_snapshot(config_data)
            assert status["readiness"][0] == 200
            assert (
                status["data"]["controllers"][0]["zones"][0]["reported"][
                    "current_position"
                ]
                == 40
            )
            await client.publish(
                f"{topic}/set",
                '{"request_id":"retained","target_position":0}',
                retain=True,
            )
            await observed.wait(
                lambda view: view.payload(f"{topic}/result")["request_id"] == "retained"
            )
            assert (
                observed.payload(f"{topic}/result")["error_code"] == "retained_command"
            )
            assert await daemon.stop() == 0
            daemon.start()
            await observed.wait(
                lambda view: (
                    view.latest["tuya/bed/availability"].payload == b"online"
                    and view.payload(topic)["session_id"]
                    != status["data"]["session_id"]
                )
            )
            assert simulator.writes == [{"202": 20}]
            await client.publish(f"{topic}/set", '{"request_id":"stop","stop":true}')
            await observed.wait(
                lambda view: view.payload(f"{topic}/result")["request_id"] == "stop"
            )
            assert (
                observed.payload(f"{topic}/result")["error_code"] == "invalid_command"
            )
        finally:
            await finish(daemon, consume)
