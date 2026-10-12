import asyncio
import json
from dataclasses import replace
from threading import get_ident
from uuid import UUID
lazy from pathlib import Path

import pytest

from tuya_mqtt.bridge import Bridge, online, random_id
from tuya_mqtt.config import ResolvedConfig, load_config
from tuya_mqtt.domain import (
    CommandResult,
    Completion,
    ConnectionState,
    ControllerSnapshot,
    ErrorCode,
    Outcome,
    ZoneState,
)
from tuya_mqtt.mqtt_client import Message, MQTTFailureError

from .bridge_fakes import BASELINE, MQTT, Scheduler, Worker, eventually
from .controller_fakes import Log


@pytest.fixture
def resolved(config_path: Path, secret_env: dict[str, str]) -> ResolvedConfig:
    return load_config(config_path, env=secret_env)


@pytest.fixture
def rig(resolved: ResolvedConfig) -> tuple[Bridge, Worker, MQTT, Scheduler]:
    scheduler = Scheduler()
    worker, mqtt = Worker(scheduler), MQTT()
    bridge = Bridge(
        resolved,
        (worker,),
        lambda: mqtt,
        clock=scheduler,
        sleep=scheduler.sleep,
        jitter=lambda: 0,
        logger=Log(),
    )
    return bridge, worker, mqtt, scheduler


def publications(mqtt: MQTT, topic: str) -> list[tuple[str, str, int, bool]]:
    return [item for item in mqtt.publications if item[0] == topic]


async def start(bridge: Bridge, mqtt: MQTT) -> asyncio.Task[None]:
    task = asyncio.create_task(bridge.run())
    await eventually(
        lambda: bridge.connected and bool(publications(mqtt, "tuya/bridge/heartbeat"))
    )
    return task


async def stop(bridge: Bridge, task: asyncio.Task[None]) -> None:
    bridge.stop()
    await asyncio.wait_for(task, 3)


def test_identity_and_freshness_requirements(resolved: ResolvedConfig) -> None:
    assert UUID(random_id()).version == 4
    with pytest.raises(ValueError, match="one worker"):
        Bridge(resolved, (), MQTT)
    view = ControllerSnapshot(
        connection=ConnectionState.ONLINE,
        session_id="s",
        zones=(BASELINE, BASELINE),
        synchronized_at=0,
    )
    assert online(view, "s", 89)
    assert not online(view, "s", 90)
    assert not online(view, None, 0)
    assert not online(view, "other", 0)
    assert not online(replace(view, connection=ConnectionState.DISCONNECTED), "s", 0)
    assert not online(replace(view, zones=(ZoneState(), BASELINE)), "s", 0)
    assert not online(replace(view, synchronized_at=None), "s", 0)


async def test_lifecycle_dedup_refresh_heartbeat_and_shutdown(
    rig: tuple[Bridge, Worker, MQTT, Scheduler],
) -> None:
    bridge, worker, mqtt, scheduler = rig
    task = await start(bridge, mqtt)
    try:
        assert mqtt.subscriptions == [
            "tuya/bed/side_a/set",
            "tuya/bed/side_a/get",
            "tuya/bed/side_b/set",
            "tuya/bed/side_b/get",
        ]
        assert mqtt.publications[:2] == [
            ("tuya/bed/availability", "offline", 1, True),
            ("tuya/bridge/availability", "offline", 1, True),
        ]
        first = publications(mqtt, "tuya/bed/side_a")
        assert len(first) == 1
        assert json.loads(first[0][1]) == BASELINE.payload(bridge.session_id)
        assert first[0][2:] == (1, True)
        assert bridge.running
        assert bridge.heartbeat_at == 0
        scheduler.advance(0.05)
        await eventually(lambda: len(scheduler.delays) >= 5)
        assert len(publications(mqtt, "tuya/bed/side_a")) == 1
        worker.view = replace(
            worker.view, zones=(replace(BASELINE, heat_level=7), BASELINE)
        )
        scheduler.advance(0.05)
        await eventually(lambda: len(publications(mqtt, "tuya/bed/side_a")) == 2)
        assert len(publications(mqtt, "tuya/bed/side_b")) == 1
        mqtt.incoming.put_nowait(Message("tuya/bed/side_a/get", b"", retained=False))
        await eventually(lambda: worker.refreshes == 1)
        scheduler.advance(0.05)
        await eventually(lambda: len(publications(mqtt, "tuya/bed/side_b")) == 2)
        scheduler.advance(15)
        await eventually(lambda: len(publications(mqtt, "tuya/bridge/heartbeat")) == 2)
        heartbeat = publications(mqtt, "tuya/bridge/heartbeat")[-1]
        assert json.loads(heartbeat[1]) == {"session_id": bridge.session_id}
        assert heartbeat[2:] == (0, False)
    finally:
        await stop(bridge, task)
    assert mqtt.publications[-2:] == [
        ("tuya/bed/availability", "offline", 1, True),
        ("tuya/bridge/availability", "offline", 1, True),
    ]
    assert mqtt.exited == 1
    assert worker.joined == 1
    assert get_ident() not in worker.join_threads
    assert not bridge.running


async def test_pending_duplicate_conflict_terminal_replay_and_zone_scope(
    rig: tuple[Bridge, Worker, MQTT, Scheduler],
) -> None:
    bridge, worker, mqtt, scheduler = rig
    worker.auto_confirm = False
    task = await start(bridge, mqtt)
    body = b'{"request_id":"r","power":true}'
    try:
        mqtt.incoming.put_nowait(Message("tuya/bed/side_a/set", body, retained=False))
        mqtt.incoming.put_nowait(
            Message(
                "tuya/bed/side_a/set",
                b'{"power":true,"request_id":"r"}',
                retained=False,
            )
        )
        await eventually(lambda: mqtt.delivered == 2)
        assert len(worker.requests) == 1
        assert not publications(mqtt, "tuya/bed/side_a/result")
        mqtt.incoming.put_nowait(
            Message(
                "tuya/bed/side_a/set",
                b'{"request_id":"r","power":false}',
                retained=False,
            )
        )
        await eventually(lambda: len(publications(mqtt, "tuya/bed/side_a/result")) == 1)
        assert (
            json.loads(publications(mqtt, "tuya/bed/side_a/result")[0][1])["error_code"]
            == "duplicate_id"
        )
        worker.confirm(worker.requests[0])
        scheduler.advance(0.05)
        await eventually(lambda: len(publications(mqtt, "tuya/bed/side_a/result")) == 2)
        mqtt.incoming.put_nowait(Message("tuya/bed/side_a/set", body, retained=False))
        await eventually(lambda: len(publications(mqtt, "tuya/bed/side_a/result")) == 3)
        assert len(worker.requests) == 1
        assert publications(mqtt, "tuya/bed/side_a/result")[-1][2:] == (0, False)
        assert bridge.outcomes[0, 0, Outcome.CONFIRMED, None] == 1
        mqtt.incoming.put_nowait(Message("tuya/bed/side_b/set", body, retained=False))
        await eventually(lambda: len(worker.requests) == 2)
        assert worker.requests[-1].zone == 1
    finally:
        await stop(bridge, task)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"no JSON",
        b"[]",
        b'{"request_id":"r"}',
        b'{"request_id":"r","watts":2}',
        b'{"heat_level":NaN}',
        b'{"power":true,"power":false}',
        b'{"power":true,"request_id":1}',
        b'{"power":true,"request_id":""}',
        b'{"power":true,"request_id":"\\u0000"}',
        b'{"power":true,"request_id":"' + b"x" * 129 + b'"}',
        b"x" * 4097,
    ],
)
async def test_invalid_commands_cannot_reach_worker(
    payload: bytes, rig: tuple[Bridge, Worker, MQTT, Scheduler]
) -> None:
    bridge, worker, mqtt, _scheduler = rig
    task = await start(bridge, mqtt)
    try:
        mqtt.incoming.put_nowait(
            Message("tuya/bed/side_a/set", payload, retained=False)
        )
        await eventually(lambda: bool(publications(mqtt, "tuya/bed/side_a/result")))
        result = json.loads(publications(mqtt, "tuya/bed/side_a/result")[0][1])
        assert result["outcome"] == "rejected"
        assert result["error_code"] == "invalid_command"
        assert not worker.requests
    finally:
        await stop(bridge, task)


@pytest.mark.parametrize(
    ("topic", "payload", "retained", "error"),
    [
        (
            "tuya/bed/side_a/set",
            b'{"request_id":"r","power":true}',
            True,
            "retained_command",
        ),
        ("tuya/bed/side_a/get", b"", True, "retained_command"),
        ("tuya/bed/side_a/get", b"nonempty", False, "invalid_command"),
    ],
)
async def test_retained_and_nonempty_get_are_rejected(
    topic: str,
    payload: bytes,
    error: str,
    rig: tuple[Bridge, Worker, MQTT, Scheduler],
    *,
    retained: bool,
) -> None:
    bridge, worker, mqtt, _scheduler = rig
    task = await start(bridge, mqtt)
    try:
        mqtt.incoming.put_nowait(Message(topic, payload, retained=retained))
        await eventually(lambda: bool(publications(mqtt, "tuya/bed/side_a/result")))
        result = json.loads(publications(mqtt, "tuya/bed/side_a/result")[0][1])
        assert result["error_code"] == error
        assert not worker.requests
        assert not worker.refreshes
    finally:
        await stop(bridge, task)


@pytest.mark.parametrize("error", [ErrorCode.WRITE_DISABLED, ErrorCode.QUEUE_FULL])
async def test_worker_admission_errors_are_terminal(
    error: ErrorCode, rig: tuple[Bridge, Worker, MQTT, Scheduler]
) -> None:
    bridge, worker, mqtt, _scheduler = rig
    worker.admission_error = error
    task = await start(bridge, mqtt)
    try:
        await bridge.handle(mqtt, Message("unknown/topic", b"", retained=False))
        await bridge.handle(
            mqtt,
            Message(
                "tuya/bed/side_a/set",
                b'{"request_id":"r","power":true}',
                retained=False,
            ),
        )
        result = json.loads(publications(mqtt, "tuya/bed/side_a/result")[0][1])
        assert result["error_code"] == error
        assert not worker.requests
    finally:
        await stop(bridge, task)


async def test_input_queue_time_counts_toward_deadline(
    rig: tuple[Bridge, Worker, MQTT, Scheduler],
) -> None:
    bridge, worker, mqtt, scheduler = rig
    task = await start(bridge, mqtt)
    try:
        scheduler.advance(3)
        await bridge.handle(
            mqtt,
            Message(
                "tuya/bed/side_a/set",
                b'{"request_id":"expired","power":true}',
                retained=False,
                received_at=0,
            ),
        )
        assert (
            json.loads(publications(mqtt, "tuya/bed/side_a/result")[0][1])["error_code"]
            == "expired"
        )
        assert not worker.requests
        await bridge.handle(
            mqtt,
            Message(
                "tuya/bed/side_b/set",
                b'{"request_id":"fresh","power":true}',
                retained=False,
                received_at=2,
            ),
        )
        assert worker.requests[-1].received_at == 2
        assert worker.requests[-1].deadline == 10
    finally:
        await stop(bridge, task)


async def test_unavailable_and_stale_controllers_reject_ingress(
    rig: tuple[Bridge, Worker, MQTT, Scheduler],
) -> None:
    bridge, worker, mqtt, scheduler = rig
    worker.auto_online = False
    task = await start(bridge, mqtt)
    try:
        await bridge.handle(
            mqtt,
            Message(
                "tuya/bed/side_a/set",
                b'{"request_id":"r","power":true}',
                retained=False,
            ),
        )
        assert (
            json.loads(publications(mqtt, "tuya/bed/side_a/result")[0][1])["error_code"]
            == "unavailable"
        )
        assert json.loads(publications(mqtt, "tuya/bed/side_a")[0][1])["power"] is None
        worker.auto_online = True
        worker.set_session(bridge.session_id)
        scheduler.advance(90)
        await bridge.handle(
            mqtt,
            Message(
                "tuya/bed/side_b/set",
                b'{"request_id":"stale","power":true}',
                retained=False,
            ),
        )
        assert (
            json.loads(publications(mqtt, "tuya/bed/side_b/result")[0][1])["error_code"]
            == "unavailable"
        )
        assert not worker.requests
    finally:
        await stop(bridge, task)


async def test_disconnected_result_is_diagnostic_and_never_replayed(
    resolved: ResolvedConfig,
) -> None:
    scheduler = Scheduler()
    worker = Worker(scheduler, auto_confirm=False)
    first, second = MQTT(), MQTT()
    attempts = iter((first, second))
    bridge = Bridge(
        resolved,
        (worker,),
        lambda: next(attempts),
        clock=scheduler,
        sleep=scheduler.sleep,
        jitter=lambda: 0,
        logger=Log(),
    )
    task = await start(bridge, first)
    try:
        old_session = bridge.session_id
        await bridge.handle(
            first,
            Message(
                "tuya/bed/side_a/set",
                b'{"request_id":"r","power":true}',
                retained=False,
            ),
        )
        await bridge.handle(
            first,
            Message(
                "tuya/bed/side_b/set",
                b'{"request_id":"old-late","power":true}',
                retained=False,
            ),
        )
        first.incoming.put_nowait(MQTTFailureError("lost broker"))
        await eventually(
            lambda: (
                bridge.mqtt_failures == 1
                and not bridge.connected
                and 1 in scheduler.delays
            )
        )
        worker.confirm(worker.requests[0])
        scheduler.advance(0.05)
        await eventually(lambda: bool(bridge.outcomes))
        scheduler.advance(1)
        await eventually(
            lambda: (
                bridge.connected and bool(publications(second, "tuya/bridge/heartbeat"))
            )
        )
        assert bridge.session_id != old_session
        assert worker.sessions[-2:] == [None, bridge.session_id]
        assert not publications(second, "tuya/bed/side_a/result")
        assert (
            json.loads(publications(second, "tuya/bed/side_a")[0][1])["session_id"]
            == bridge.session_id
        )
        assert len(worker.requests) == 2
        worker.completed.append(
            Completion(
                worker.requests[1],
                CommandResult(
                    "old-late", Outcome.UNCONFIRMED, ErrorCode.CONNECTION_LOST
                ),
                scheduler(),
            )
        )
        scheduler.advance(0.05)
        await eventually(lambda: len(bridge.outcomes) == 2)
        assert not publications(second, "tuya/bed/side_b/result")
    finally:
        await stop(bridge, task)


async def test_backoff_is_capped_and_shutdown_interrupts_it(
    resolved: ResolvedConfig,
) -> None:
    scheduler = Scheduler()
    worker = Worker(scheduler)
    mqtt = MQTT(enter_error=MQTTFailureError("absent"))
    bridge = Bridge(
        resolved,
        (worker,),
        lambda: mqtt,
        clock=scheduler,
        sleep=scheduler.sleep,
        logger=Log(),
    )
    task = asyncio.create_task(bridge.run())
    await eventually(
        lambda: (
            bridge.mqtt_failures == 1 and any(delay >= 1 for delay in scheduler.delays)
        )
    )
    await stop(bridge, task)
    assert scheduler.now == 0
    assert worker.joined == 1


@pytest.mark.parametrize("failure", ["start", "check", "join", "publish"])
async def test_unexpected_failures_terminate_and_join_workers(
    failure: str, resolved: ResolvedConfig
) -> None:
    scheduler = Scheduler()
    worker, mqtt = Worker(scheduler), MQTT()
    error = RuntimeError("unexpected")
    if failure == "publish":
        mqtt.publish_error = error
    else:
        setattr(worker, f"{failure}_error", error)
    bridge = Bridge(
        resolved,
        (worker,),
        lambda: mqtt,
        clock=scheduler,
        sleep=scheduler.sleep,
        logger=Log(),
    )
    task = asyncio.create_task(bridge.run())
    if failure == "join":
        await eventually(lambda: bridge.connected)
        bridge.stop()
    with pytest.raises((RuntimeError, ExceptionGroup)):
        await asyncio.wait_for(task, 3)
    assert worker.joined == 1
    assert not bridge.running
    assert bridge.mqtt_failures == 0


async def test_command_stream_ending_is_a_connection_failure(
    rig: tuple[Bridge, Worker, MQTT, Scheduler],
) -> None:
    bridge, worker, mqtt, scheduler = rig

    def disconnected_before_teardown() -> None:
        assert not bridge.connected
        assert worker.sessions[-1] is None

    mqtt.exit_hook = disconnected_before_teardown
    task = await start(bridge, mqtt)
    try:
        mqtt.incoming.put_nowait(None)
        await eventually(lambda: bridge.mqtt_failures == 1 and 1 in scheduler.delays)
        assert not bridge.connected
        assert bridge.last_error is ErrorCode.MQTT_ERROR
    finally:
        await stop(bridge, task)


async def test_wrong_session_state_and_shutdown_cannot_admit_commands(
    rig: tuple[Bridge, Worker, MQTT, Scheduler],
) -> None:
    bridge, worker, mqtt, scheduler = rig
    task = await start(bridge, mqtt)
    try:
        worker.view = replace(worker.view, session_id="old")
        scheduler.advance(0.05)
        await eventually(lambda: len(publications(mqtt, "tuya/bed/side_a")) == 2)
        assert json.loads(publications(mqtt, "tuya/bed/side_a")[-1][1])["power"] is None
        await bridge.handle(
            mqtt,
            Message(
                "tuya/bed/side_a/set",
                b'{"request_id":"old","power":true}',
                retained=False,
            ),
        )
        worker.set_session(bridge.session_id)
        bridge.stop()
        await bridge.handle(
            mqtt,
            Message(
                "tuya/bed/side_a/set",
                b'{"request_id":"stopped","power":true}',
                retained=False,
            ),
        )
        assert not worker.requests
        assert all(
            json.loads(item[1])["error_code"] == "unavailable"
            for item in publications(mqtt, "tuya/bed/side_a/result")
        )
    finally:
        await stop(bridge, task)


async def test_backoff_caps_jitter_and_resets_only_after_setup(
    resolved: ResolvedConfig,
) -> None:
    worker = Worker(Scheduler())
    delays: list[float] = []
    mqtt = MQTT(enter_error=MQTTFailureError("absent"))
    bridge = Bridge(resolved, (worker,), lambda: mqtt, jitter=lambda: 1, logger=Log())

    async def elapsed(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 8:
            bridge.stop()

    bridge._sleep = elapsed
    await bridge._broker_loop()
    assert delays == [1.25, 2.5, 5, 10, 20, 40, 60, 60]
    assert bridge.mqtt_failures == 8

    first = MQTT(subscribe_error=MQTTFailureError("denied subscription"))
    second = MQTT()
    second.incoming.put_nowait(None)
    attempts = iter((first, second, mqtt, mqtt))
    delays.clear()
    bridge = Bridge(
        resolved, (worker,), lambda: next(attempts), jitter=lambda: 0, logger=Log()
    )

    async def elapsed_after_setup(delay: float) -> None:
        if delay in (0.05, 15):
            await asyncio.Event().wait()
        delays.append(delay)
        if len(delays) == 4:
            bridge.stop()

    bridge._sleep = elapsed_after_setup
    await bridge._broker_loop()
    assert delays == [1, 1, 2, 4]
    assert bridge.mqtt_failures == 4
