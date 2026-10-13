import asyncio
import json
import signal
import socket
from contextlib import suppress
lazy from pathlib import Path
lazy from typing import Any

import aiomqtt
import pytest
from aiohttp import ClientSession
from tuyamock import MockDevice

from .broker import Broker
from .process import Daemon, Observed, finish, ready
lazy from .tcp import Simulator

pytestmark = pytest.mark.integration


def configure_process(
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
    broker: Broker,
    simulator: Simulator,
) -> Daemon:
    config_data["mqtt"] |= {"host": "127.0.0.1", "port": broker.port}
    config_data["controllers"][0] |= {
        "ip": "127.0.0.1",
        "port": simulator.mock.port,
        "protocol_version": simulator.mock.config.version,
        "write_mode": "multi_dp",
    }
    with socket.socket() as http_port:
        http_port.bind(("127.0.0.1", 0))
        config_data["http"] = {"bind": "127.0.0.1", "port": http_port.getsockname()[1]}
    config_path.write_text(json.dumps(config_data))
    return Daemon(config_path, secret_env, config_path.parent / "daemon.log")


async def http_snapshot(config_data: dict[str, Any]) -> dict[str, Any]:
    config = config_data["http"]
    base = f"http://{config['bind']}:{config['port']}"
    async with ClientSession() as client:
        async with client.get(f"{base}/healthz") as response:
            health = response.status, await response.json()
        async with client.get(f"{base}/readyz") as response:
            readiness = response.status, await response.json()
        async with client.get(f"{base}/metrics") as response:
            metrics = await response.text()
        async with client.get(f"{base}/events") as response:
            line = await asyncio.wait_for(response.content.readline(), 3)
            assert line.startswith(b"data: ")
            data = json.loads(line[6:])
    return {"health": health, "readiness": readiness, "metrics": metrics, "data": data}


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
async def test_process_commands_pushes_refresh_retained_state_and_shutdown(
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await broker.ready()
    daemon = configure_process(config_path, config_data, secret_env, broker, simulator)
    observed = Observed()
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as subscriber:
        await subscriber.subscribe("tuya/#", qos=1)
        consume = asyncio.create_task(observed.consume(subscriber))
        try:
            daemon.start()
            await ready(observed)
            session_id = observed.payload("tuya/bridge/heartbeat")["session_id"]
            await subscriber.publish(
                "tuya/bed/side_a/set",
                '{"request_id":"a","power":true,"heat_level":6}',
                qos=0,
            )
            await subscriber.publish(
                "tuya/bed/side_b/set",
                '{"request_id":"b","power":true,"heat_level":8}',
                qos=0,
            )
            await observed.wait(
                lambda data: (
                    "tuya/bed/side_a/result" in data.latest
                    and "tuya/bed/side_b/result" in data.latest
                )
            )
            assert observed.payload("tuya/bed/side_a/result") == {
                "request_id": "a",
                "outcome": "confirmed",
            }
            assert observed.payload("tuya/bed/side_b/result") == {
                "request_id": "b",
                "outcome": "confirmed",
            }
            assert simulator.writes == [
                {"14": True, "20": "level_6"},
                {"15": True, "21": "level_8"},
            ]
            await observed.wait(
                lambda data: (
                    data.payload("tuya/bed/side_a")["heat_level"] == 6
                    and data.payload("tuya/bed/side_b")["heat_level"] == 8
                )
            )
            for zone, command, expected in (
                ("side_a", '{"request_id":"level","heat_level":3}', {"20": "level_3"}),
                ("side_a", '{"request_id":"preheat","preheat":true}', {"24": True}),
                (
                    "side_b",
                    '{"request_id":"timer","auto_off_seconds":5400}',
                    {"27": "1h30m"},
                ),
            ):
                await subscriber.publish(f"tuya/bed/{zone}/set", command, qos=0)
                request_id = json.loads(command)["request_id"]
                await observed.wait(
                    lambda data, name=zone, identifier=request_id: (
                        data.payload(f"tuya/bed/{name}/result")["request_id"]
                        == identifier
                    )
                )
                assert (
                    observed.payload(f"tuya/bed/{zone}/result")["outcome"]
                    == "confirmed"
                )
                assert simulator.writes[-1] == expected
            assert simulator.mock.dps["14"] is True
            diagnostic = await http_snapshot(config_data)
            assert diagnostic["health"] == (200, {"healthy": True})
            assert diagnostic["readiness"] == (200, {"ready": True, "reasons": []})
            assert diagnostic["data"]["session_id"] == session_id
            assert (
                diagnostic["data"]["controllers"][0]["zones"][0]["reported"][
                    "heat_level"
                ]
                == 3
            )
            assert (
                'tuya_mqtt_command_outcomes_total{controller="bed",'
                'error_code="none",outcome="confirmed",zone="side_a"} 3.0'
                in diagnostic["metrics"]
            )
            assert all(
                secret not in json.dumps(diagnostic) for secret in secret_env.values()
            )
            assert all(
                observed.payload(f"tuya/bed/{zone}")["fault_code"] == 0
                for zone in ("side_a", "side_b")
            )
            pushed = {"14": False, "24": True, "102": 90, "13": 8}
            simulator.mock.dps.update(pushed)
            assert simulator.mock.push(pushed)
            await observed.wait(
                lambda data: (
                    data.payload("tuya/bed/side_a")["preheat_remaining_seconds"] == 90
                    and data.payload("tuya/bed/side_b")["fault_code"] == 8
                )
            )
            assert observed.payload("tuya/bed/side_a")["power"] is False
            assert (
                observed.payload("tuya/bed/side_a")["preheat_remaining_seconds"] == 90
            )
            assert observed.payload("tuya/bed/side_a")["fault_code"] == 8
            fault_page = await http_snapshot(config_data)
            assert fault_page["health"] == (200, {"healthy": True})
            assert fault_page["readiness"] == (200, {"ready": True, "reasons": []})
            assert observed.latest["tuya/bed/availability"].payload == b"online"
            assert (
                'tuya_mqtt_controller_fault_code{controller="bed"} 8.0'
                in fault_page["metrics"]
            )
            assert [
                zone["reported"]["fault_code"]
                for zone in fault_page["data"]["controllers"][0]["zones"]
            ] == [8, 8]
            before = observed.count("tuya/bed/side_b")
            await subscriber.publish("tuya/bed/side_b/get", b"", qos=0)
            await observed.wait(lambda data: data.count("tuya/bed/side_b") > before)
            async with aiomqtt.Client(
                "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
            ) as fresh:
                await fresh.subscribe("tuya/bed/side_a", qos=1)
                snapshot = await asyncio.wait_for(anext(fresh.messages), 3)
                assert snapshot.retain
                assert json.loads(snapshot.payload)["session_id"] == session_id
            assert await daemon.stop() == 0
            await observed.wait(
                lambda data: (
                    data.latest["tuya/bridge/availability"].payload == b"offline"
                )
            )
            assert observed.latest["tuya/bed/availability"].payload == b"offline"
            text = daemon.log_path.read_text()
            assert all(secret not in text for secret in secret_env.values())
            assert any(event["event"] == "starting" for event in daemon.events)
        finally:
            await finish(daemon, consume)


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
async def test_process_retained_commands_are_rejected_and_kill_triggers_will(
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await broker.ready()
    daemon = configure_process(config_path, config_data, secret_env, broker, simulator)
    observed = Observed()
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as subscriber:
        await subscriber.publish(
            "tuya/bed/side_a/set",
            '{"request_id":"old","power":true}',
            qos=1,
            retain=True,
        )
        await subscriber.subscribe("tuya/#", qos=1)
        consume = asyncio.create_task(observed.consume(subscriber))
        try:
            daemon.start()
            await ready(observed)
            old_session = observed.payload("tuya/bridge/heartbeat")["session_id"]
            assert not simulator.writes
            await subscriber.publish(
                "tuya/bed/side_a/set",
                '{"request_id":"live","power":true}',
                qos=0,
                retain=True,
            )
            await observed.wait(lambda data: "tuya/bed/side_a/result" in data.latest)
            assert observed.payload("tuya/bed/side_a/result") == {
                "request_id": "live",
                "outcome": "rejected",
                "error_code": "retained_command",
            }
            assert not simulator.writes
            assert await daemon.stop(abrupt=True) == -signal.SIGKILL
            await observed.wait(
                lambda data: (
                    data.latest["tuya/bridge/availability"].payload == b"offline"
                )
            )
            daemon.start()
            await observed.wait(
                lambda data: (
                    data.payload("tuya/bridge/heartbeat")["session_id"] != old_session
                )
            )
            await ready(observed)
            assert not simulator.writes
            assert observed.payload("tuya/bed/side_a")["power"] is False
        finally:
            await finish(daemon, consume)


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
async def test_process_device_failure_is_unconfirmed_and_never_replayed(
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await broker.ready()
    daemon = configure_process(config_path, config_data, secret_env, broker, simulator)
    observed = Observed()
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as subscriber:
        await subscriber.subscribe("tuya/#", qos=1)
        consume = asyncio.create_task(observed.consume(subscriber))
        try:
            daemon.start()
            await ready(observed)
            simulator.mode = "close"
            await subscriber.publish(
                "tuya/bed/side_a/set",
                '{"request_id":"uncertain","power":true,"heat_level":9}',
                qos=0,
            )
            await observed.wait(lambda data: "tuya/bed/side_a/result" in data.latest)
            result = observed.payload("tuya/bed/side_a/result")
            assert result["outcome"] == "unconfirmed"
            assert result["request_id"] == "uncertain"
            simulator.mode = "normal"
            await observed.wait(
                lambda data: (
                    data.payload("tuya/bed/side_a")["heat_level"] == 9
                    and data.latest["tuya/bed/availability"].payload == b"online"
                )
            )
            assert simulator.writes == [{"14": True, "20": "level_9"}]
            assert simulator.mock.server.connections >= 2
        finally:
            await finish(daemon, consume)


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
async def test_process_starts_without_broker_and_recovers(
    tmp_path: Path,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        broker = Broker(tmp_path, port=reserved.getsockname()[1])
        daemon = configure_process(
            config_path, config_data, secret_env, broker, simulator
        )
        daemon.start()
        try:
            await daemon.wait_event("mqtt_disconnected")
            diagnostic = await http_snapshot(config_data)
            assert diagnostic["health"] == (200, {"healthy": True})
            assert diagnostic["readiness"][0] == 503
            assert "mqtt_disconnected" in diagnostic["readiness"][1]["reasons"]
        except BaseException:
            await daemon.stop()
            raise
    observed = Observed()
    try:
        broker.start()
        await broker.ready()
        async with aiomqtt.Client(
            "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
        ) as subscriber:
            await subscriber.subscribe("tuya/#", qos=1)
            consume = asyncio.create_task(observed.consume(subscriber))
            try:
                await ready(observed)
                assert daemon.process is not None
                assert daemon.process.poll() is None
            finally:
                assert await finish(daemon, consume) == 0

    finally:
        await daemon.stop()
        broker.stop()


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
async def test_broker_restart_requires_fresh_session_and_does_not_replay_commands(
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await broker.ready()
    daemon = configure_process(config_path, config_data, secret_env, broker, simulator)
    observed = Observed()
    try:
        async with aiomqtt.Client(
            "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
        ) as subscriber:
            await subscriber.publish(
                "tuya/bed/side_a/set",
                '{"request_id":"old","power":true}',
                qos=1,
                retain=True,
            )
            await subscriber.subscribe("tuya/#", qos=1)
            consume = asyncio.create_task(observed.consume(subscriber))
            try:
                daemon.start()
                await ready(observed)
                old_session = observed.payload("tuya/bridge/heartbeat")["session_id"]
                assert daemon.process is not None
                daemon.process.send_signal(signal.SIGSTOP)
                broker.stop()
                with pytest.raises(aiomqtt.MqttError):
                    await asyncio.wait_for(consume, 3)
            finally:
                consume.cancel()
                with suppress(asyncio.CancelledError, aiomqtt.MqttError):
                    await consume
        broker.start()
        await broker.ready()
        restored = Observed()
        async with aiomqtt.Client(
            "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
        ) as subscriber:
            await subscriber.subscribe("tuya/#", qos=1)
            consume = asyncio.create_task(restored.consume(subscriber))
            try:
                await restored.wait(lambda data: "tuya/bed/side_a" in data.latest)
                assert restored.payload("tuya/bed/side_a")["session_id"] == old_session
                assert "tuya/bridge/heartbeat" not in restored.latest
                assert not simulator.writes
                assert daemon.process is not None
                daemon.process.send_signal(signal.SIGCONT)
                await ready(restored)
                assert (
                    restored.payload("tuya/bridge/heartbeat")["session_id"]
                    != old_session
                )
                assert restored.payload("tuya/bed/side_a")["power"] is False
                assert not simulator.writes
                assert await daemon.stop() == 0
            finally:
                await finish(daemon, consume)
    finally:
        await daemon.stop()


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
async def test_process_starts_with_missing_controller_and_rejects_commands(
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await broker.ready()
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        daemon = configure_process(
            config_path, config_data, secret_env, broker, simulator
        )
        config_data["controllers"][0]["port"] = reserved.getsockname()[1]
        await asyncio.to_thread(config_path.write_text, json.dumps(config_data))
        observed = Observed()
        async with aiomqtt.Client(
            "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
        ) as subscriber:
            await subscriber.subscribe("tuya/#", qos=1)
            consume = asyncio.create_task(observed.consume(subscriber))
            try:
                daemon.start()
                await observed.wait(
                    lambda data: (
                        "tuya/bridge/heartbeat" in data.latest
                        and "tuya/bed/availability" in data.latest
                        and "tuya/bed/side_a" in data.latest
                    )
                )
                assert observed.latest["tuya/bridge/availability"].payload == b"online"
                assert observed.latest["tuya/bed/availability"].payload == b"offline"
                assert observed.payload("tuya/bed/side_a")["power"] is None
                await subscriber.publish(
                    "tuya/bed/side_a/set",
                    '{"request_id":"unavailable","power":true}',
                    qos=0,
                )
                await observed.wait(
                    lambda data: "tuya/bed/side_a/result" in data.latest
                )
                assert (
                    observed.payload("tuya/bed/side_a/result")["error_code"]
                    == "unavailable"
                )
                assert not simulator.writes
                assert await daemon.stop() == 0
            finally:
                await finish(daemon, consume)


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
@pytest.mark.parametrize("stalled_version", ["3.4", "3.5"])
async def test_healthy_controller_serves_with_stalled_peer_and_shutdown_is_bounded(
    stalled_version: str,
    broker: Broker,
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
) -> None:
    await broker.ready()
    with MockDevice(
        local_key=secret_env["SUNBEAM_LOCAL_KEY"],
        version=stalled_version,
        go_dark_after=0,
    ) as stalled:
        daemon = configure_process(
            config_path, config_data, secret_env, broker, simulator
        )
        config_data["controllers"].append(
            config_data["controllers"][0]
            | {
                "name": "stalled",
                "device_id": "stalled-test",
                "port": stalled.port,
                "protocol_version": stalled_version,
            }
        )
        await asyncio.to_thread(config_path.write_text, json.dumps(config_data))
        observed = Observed()
        async with aiomqtt.Client(
            "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
        ) as subscriber:
            await subscriber.subscribe("tuya/#", qos=1)
            consume = asyncio.create_task(observed.consume(subscriber))
            try:
                daemon.start()
                await ready(observed)
                await daemon.wait_event("controller_disconnected")
                diagnostic = await http_snapshot(config_data)
                assert diagnostic["health"] == (200, {"healthy": True})
                assert diagnostic["readiness"][0] == 503
                assert (
                    "controller_unavailable:stalled"
                    in diagnostic["readiness"][1]["reasons"]
                )
                assert (
                    observed.latest["tuya/stalled/availability"].payload == b"offline"
                )
                await subscriber.publish(
                    "tuya/bed/side_a/set",
                    '{"request_id":"healthy","power":true}',
                    qos=0,
                )
                await observed.wait(
                    lambda data: "tuya/bed/side_a/result" in data.latest
                )
                assert (
                    observed.payload("tuya/bed/side_a/result")["outcome"] == "confirmed"
                )
                assert simulator.writes == [{"14": True}]
                assert stalled.server.connections >= 1
                assert await daemon.stop() == 0
            finally:
                await finish(daemon, consume)
