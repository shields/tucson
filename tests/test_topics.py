import json
from dataclasses import dataclass
lazy from pathlib import Path
lazy from typing import Any

import pytest
from prometheus_client.parser import text_string_to_metric_families

from tuya_mqtt.bridge import Bridge
from tuya_mqtt.config import load_config
from tuya_mqtt.metrics import Metrics
from tuya_mqtt.mqtt_client import Message
from tuya_mqtt.status_page import StatusServer

from .bridge_fakes import CURTAIN, MQTT, Scheduler, Worker, curtain_worker, eventually
from .controller_fakes import Log
from .test_bridge import publications, start, stop

BED_ROUTES = [
    "tuya/bed/side_a/set",
    "tuya/bed/side_a/get",
    "tuya/bed/side_b/set",
    "tuya/bed/side_b/get",
]


@dataclass
class Fleet:
    bridge: Bridge
    bed: Worker
    curtain: Worker
    mqtt: MQTT
    clock: Scheduler
    log: Log
    secrets: tuple[str, ...]


@pytest.fixture
def fleet(
    config_data: dict[str, Any], tmp_path: Path, secret_env: dict[str, str]
) -> Fleet:
    config_data["controllers"].append(
        {
            "name": "curtain",
            "device_id": "curtain-device",
            "ip": "192.0.2.11",
            "protocol_version": "3.3",
            "local_key": {"env": "SUNBEAM_LOCAL_KEY"},
            "profile": "zemismart-bcd300fb",
            "write_mode": "multi_dp",
        }
    )
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    resolved = load_config(path, env=secret_env)
    clock, mqtt, log = Scheduler(), MQTT(), Log()
    bed, curtain = Worker(clock), curtain_worker(clock)
    bridge = Bridge(
        resolved,
        (bed, curtain),
        lambda: mqtt,
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda: 0,
        logger=log,
    )
    return Fleet(bridge, bed, curtain, mqtt, clock, log, resolved.local_keys)


async def test_a_single_endpoint_controller_has_no_zone_in_its_topics(
    fleet: Fleet,
) -> None:
    task = await start(fleet.bridge, fleet.mqtt)
    try:
        assert fleet.mqtt.subscriptions == [
            *BED_ROUTES,
            "tuya/curtain/set",
            "tuya/curtain/get",
        ]
        assert fleet.mqtt.publications[:3] == [
            ("tuya/bed/availability", "offline", 1, True),
            ("tuya/curtain/availability", "offline", 1, True),
            ("tuya/bridge/availability", "offline", 1, True),
        ]
        session_id = fleet.bridge.session_id
        assert session_id is not None
        state = publications(fleet.mqtt, "tuya/curtain")
        assert [json.loads(payload) for _topic, payload, *_ in state] == [
            CURTAIN.payload(session_id)
        ]
        assert state[0][2:] == (1, True)
        assert publications(fleet.mqtt, "tuya/curtain/availability")[-1][1:] == (
            "online",
            1,
            True,
        )
        assert len(publications(fleet.mqtt, "tuya/bed/side_a")) == 1
        assert not [
            topic
            for topic, *_ in fleet.mqtt.publications
            if topic.startswith("tuya/curtain/")
            and topic != "tuya/curtain/availability"
        ]
        await fleet.bridge.handle(
            fleet.mqtt,
            Message(
                "tuya/curtain/set",
                b'{"request_id":"c1","target_position":80}',
                retained=False,
            ),
        )
        assert [request.endpoint for request in fleet.curtain.requests] == [0]
        assert not fleet.bed.requests
        fleet.clock.advance(0.05)
        await eventually(lambda: bool(publications(fleet.mqtt, "tuya/curtain/result")))
        assert json.loads(publications(fleet.mqtt, "tuya/curtain/result")[0][1]) == {
            "request_id": "c1",
            "outcome": "confirmed",
        }
        assert publications(fleet.mqtt, "tuya/curtain/result")[0][2:] == (0, False)
        await eventually(lambda: len(publications(fleet.mqtt, "tuya/curtain")) == 2)
        assert (
            json.loads(publications(fleet.mqtt, "tuya/curtain")[-1][1])[
                "target_position"
            ]
            == 80
        )
        fleet.mqtt.incoming.put_nowait(Message("tuya/curtain/get", b"", retained=False))
        await eventually(lambda: fleet.curtain.refreshes == 1)
        assert not fleet.bed.refreshes
        await fleet.bridge.handle(
            fleet.mqtt, Message("tuya/curtain/get", b"x", retained=False)
        )
        assert json.loads(publications(fleet.mqtt, "tuya/curtain/result")[-1][1])[
            "error_code"
        ] == ("invalid_command")
    finally:
        await stop(fleet.bridge, task)
    assert fleet.mqtt.publications[-3:] == [
        ("tuya/bed/availability", "offline", 1, True),
        ("tuya/curtain/availability", "offline", 1, True),
        ("tuya/bridge/availability", "offline", 1, True),
    ]


@pytest.mark.parametrize(
    "topic",
    [
        "tuya/curtain/curtain/set",
        "tuya/curtain/side_a/set",
        "tuya/curtain/result",
        "tuya/curtain/availability",
        "tuya/curtain",
    ],
)
async def test_unrouted_topics_under_a_single_endpoint_controller_are_ignored(
    topic: str, fleet: Fleet
) -> None:
    task = await start(fleet.bridge, fleet.mqtt)
    try:
        await fleet.bridge.handle(
            fleet.mqtt,
            Message(topic, b'{"request_id":"c","target_position":80}', retained=False),
        )
        assert not fleet.curtain.requests
        assert not fleet.bed.requests
        assert not publications(fleet.mqtt, "tuya/curtain/result")
    finally:
        await stop(fleet.bridge, task)


async def test_a_multi_endpoint_controller_keeps_its_zone_topics(
    fleet: Fleet,
) -> None:
    task = await start(fleet.bridge, fleet.mqtt)
    try:
        for zone, endpoint in (("side_a", 0), ("side_b", 1)):
            await fleet.bridge.handle(
                fleet.mqtt,
                Message(
                    f"tuya/bed/{zone}/set",
                    b'{"request_id":"' + zone.encode() + b'","power":true}',
                    retained=False,
                ),
            )
            assert fleet.bed.requests[-1].endpoint == endpoint
        fleet.clock.advance(0.05)
        await eventually(
            lambda: all(
                publications(fleet.mqtt, f"tuya/bed/{zone}/result")
                for zone in ("side_a", "side_b")
            )
        )
        assert not publications(fleet.mqtt, "tuya/bed/result")
        assert not fleet.curtain.requests
    finally:
        await stop(fleet.bridge, task)


async def test_diagnostics_name_a_zone_only_for_a_multi_endpoint_controller(
    fleet: Fleet,
) -> None:
    task = await start(fleet.bridge, fleet.mqtt)
    try:
        for topic, body in (
            ("tuya/bed/side_a/set", b'{"request_id":"a","power":true}'),
            ("tuya/curtain/set", b'{"request_id":"c","target_position":80}'),
        ):
            await fleet.bridge.handle(fleet.mqtt, Message(topic, body, retained=False))
        fleet.clock.advance(0.05)
        await eventually(lambda: len(fleet.bridge.outcomes) == 2)
        results = {
            fields["request_id"]: fields["zone"]
            for _level, event, fields in fleet.log.events
            if event == "command_result"
        }
        assert results == {"a": "side_a", "c": None}
        samples = {
            (sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in text_string_to_metric_families(
                Metrics(fleet.bridge, fleet.secrets).render().decode()
            )
            for sample in family.samples
        }
        for controller, zone in (("bed", "side_a"), ("curtain", "")):
            assert (
                samples[
                    "tuya_mqtt_command_outcomes_total",
                    (
                        ("controller", controller),
                        ("error_code", "none"),
                        ("outcome", "confirmed"),
                        ("zone", zone),
                    ),
                ]
                == 1
            )
            assert (
                samples[
                    "tuya_mqtt_command_duration_seconds_count",
                    (("controller", controller), ("zone", zone)),
                ]
                == 1
            )
        assert (
            "tuya_mqtt_command_duration_seconds_count",
            (("controller", "bed"), ("zone", "side_b")),
        ) in samples
        status = StatusServer(
            fleet.bridge, fleet.secrets, {}, wall_clock=lambda: "test time"
        ).data()
        bed, curtain = status["controllers"]
        assert [endpoint["zone"] for endpoint in bed["endpoints"]] == [
            "side_a",
            "side_b",
        ]
        assert [endpoint["zone"] for endpoint in curtain["endpoints"]] == [None]
    finally:
        await stop(fleet.bridge, task)
