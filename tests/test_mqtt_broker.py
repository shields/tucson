import asyncio
import json
import threading
from dataclasses import replace
lazy from pathlib import Path

import aiomqtt
import pytest

from tuya_mqtt.config import Config, ResolvedConfig, load_config
from tuya_mqtt.mqtt_client import Client, MQTTFailureError

from .broker import Broker

pytestmark = pytest.mark.integration


def settings(
    config_path: Path, secret_env: dict[str, str], port: int
) -> ResolvedConfig:
    resolved = load_config(config_path, env=secret_env)
    data = resolved.config.model_dump(mode="json")
    data["mqtt"] |= {"host": "127.0.0.1", "port": port, "username": "bridge"}
    return replace(resolved, config=Config.model_validate_json(json.dumps(data)))


async def test_real_subscription_retention_and_publication(
    broker: Broker, config_path: Path, secret_env: dict[str, str]
) -> None:
    await broker.ready()
    resolved = settings(config_path, secret_env, broker.port)
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as publisher:
        await publisher.publish(
            "tuya/bed/side_a/set", "old retained command", retain=True, qos=1
        )
        async with Client(resolved, None) as client:
            await client.subscribe("tuya/bed/side_a/set")
            messages = client.messages
            await publisher.publish("tuya/bed/side_a/set", "live", qos=0)
            message = await asyncio.wait_for(anext(messages), 3)
            assert message.payload == b"live"
            assert not message.retained
            assert message.received_at is not None
            await publisher.publish(
                "tuya/bed/side_a/set", "retained live", retain=True, qos=0
            )
            message = await asyncio.wait_for(anext(messages), 3)
            assert message.payload == b"retained live"
            assert message.retained
            await publisher.publish("tuya/bed/side_a/set", b"", qos=0)
            message = await asyncio.wait_for(anext(messages), 3)
            assert message.payload == b""
            await client.publish(
                "tuya/bed/side_a", '{"session_id":"test-session"}', qos=1, retain=True
            )
            async with aiomqtt.Client(
                "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
            ) as fresh:
                await fresh.subscribe("tuya/bed/side_a", qos=1)
                state = await asyncio.wait_for(anext(fresh.messages), 3)
                assert state.retain
                assert json.loads(state.payload)["session_id"] == "test-session"


async def test_real_negative_puback_fails_and_closes(
    tmp_path: Path, config_path: Path, secret_env: dict[str, str]
) -> None:
    acl = tmp_path / "acl"
    acl.write_text(
        "topic read tuya/bridge/availability\nuser bridge\n"
        "topic readwrite allowed/#\ntopic write tuya/bridge/availability\n"
    )
    acl.chmod(0o600)
    service = Broker(tmp_path, extra=f"acl_file {acl}\n")
    try:
        service.start()
        await service.ready()
        resolved = settings(config_path, secret_env, service.port)
        async with aiomqtt.Client(
            "127.0.0.1", service.port, protocol=aiomqtt.ProtocolVersion.V5
        ) as observer:
            await observer.subscribe("tuya/bridge/availability", qos=1)
            async with Client(resolved, None) as client:
                await client.publish("allowed/state", "permitted", qos=1, retain=True)
                with pytest.raises(MQTTFailureError, match="publication failed"):
                    await client.publish("denied/state", "denied", qos=1, retain=True)
                message = await asyncio.wait_for(anext(observer.messages), 3)
                assert message.payload == b"offline"
    finally:
        service.stop()


async def test_real_broker_restart_preserves_only_retained_data(
    broker: Broker, config_path: Path, secret_env: dict[str, str]
) -> None:
    await broker.ready()
    resolved = settings(config_path, secret_env, broker.port)
    async with Client(resolved, None) as client:
        await client.publish(
            "tuya/bed/side_a", '{"session_id":"old"}', qos=1, retain=True
        )
        await client.publish(
            "tuya/bridge/heartbeat", '{"session_id":"old"}', qos=0, retain=False
        )
    broker.stop()
    broker.start()
    await broker.ready()
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as subscriber:
        await subscriber.subscribe("tuya/#", qos=1)
        message = await asyncio.wait_for(anext(subscriber.messages), 3)
        assert str(message.topic) == "tuya/bed/side_a"
        assert json.loads(message.payload)["session_id"] == "old"
        async with Client(resolved, None) as current:
            await current.publish(
                "tuya/bridge/heartbeat", '{"session_id":"new"}', qos=0, retain=False
            )
            message = await asyncio.wait_for(anext(subscriber.messages), 3)
            assert str(message.topic) == "tuya/bridge/heartbeat"
            assert json.loads(message.payload)["session_id"] == "new"


async def test_cancelled_connect_closes_a_socket_opened_later(
    broker: Broker,
    config_path: Path,
    secret_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await broker.ready()
    connection = Client(settings(config_path, secret_env, broker.port), None)
    raw = connection._client
    original = raw._client_connect
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    errors: list[dict[str, object]] = []

    def delayed_connect() -> None:
        try:
            started.set()
            if not release.wait(3):
                message = "connect gate timed out"
                raise RuntimeError(message)
            original()
        finally:
            finished.set()

    monkeypatch.setattr(raw, "_client_connect", delayed_connect)
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    entered = asyncio.create_task(connection.__aenter__())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        entered.cancel()
        with pytest.raises(asyncio.CancelledError):
            await entered
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        assert raw._client.socket() is None
        assert errors == []
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 3)
        raw.close_transport()
        loop.set_exception_handler(original_handler)


async def test_real_incoming_overflow_fails_and_triggers_last_will(
    broker: Broker, config_path: Path, secret_env: dict[str, str]
) -> None:
    await broker.ready()
    resolved = settings(config_path, secret_env, broker.port)
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as publisher:
        await publisher.subscribe("tuya/bridge/availability", qos=1)
        async with Client(resolved, None) as client:
            await client.subscribe("tuya/bed/side_a/set")
            for _ in range(257):
                await publisher.publish("tuya/bed/side_a/set", "{}", qos=0)
            message = await asyncio.wait_for(anext(publisher.messages), 3)
            assert message.payload == b"offline"
            with pytest.raises(MQTTFailureError, match="message stream failed"):
                await anext(client.messages)


async def test_application_failure_aborts_transport_and_triggers_last_will(
    broker: Broker, config_path: Path, secret_env: dict[str, str]
) -> None:
    await broker.ready()
    resolved = settings(config_path, secret_env, broker.port)
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as observer:
        await observer.subscribe("tuya/bridge/availability", qos=1)
        failure = ValueError("application failure")
        with pytest.raises(ValueError, match="application failure") as caught:
            async with Client(resolved, None):
                raise failure
        assert caught.value is failure
        message = await asyncio.wait_for(anext(observer.messages), 3)
        assert message.payload == b"offline"
