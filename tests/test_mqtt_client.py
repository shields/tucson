import asyncio
import json
from dataclasses import replace
from typing import Self
lazy from collections.abc import AsyncIterator
lazy from pathlib import Path

import aiomqtt
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
lazy from paho.mqtt.subscribeoptions import SubscribeOptions

import tuya_mqtt.mqtt_client as adapter
from tuya_mqtt.config import Config, ResolvedConfig, load_config
from tuya_mqtt.domain import BridgeError


class RawFake:
    def __init__(self) -> None:
        self.failure: BaseException | None = None
        self.closed = 0
        self.reasons: list[int] = [0]
        self.options: SubscribeOptions | None = None

    async def __aenter__(self) -> Self:
        if self.failure is not None:
            raise self.failure
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.failure is not None:
            raise self.failure

    def close_transport(self) -> None:
        self.closed += 1

    def check_connection(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def subscribe(self, _topic: str, *, options: SubscribeOptions) -> list[int]:
        self.options = options
        if self.failure is not None:
            raise self.failure
        return self.reasons

    async def publish_checked(self, *_args: object, **_kwargs: object) -> None:
        if self.failure is not None:
            raise self.failure

    @property
    def messages(self) -> AsyncIterator[aiomqtt.Message]:
        return self.stream()

    async def stream(self) -> AsyncIterator[aiomqtt.Message]:
        if self.failure is not None:
            raise self.failure
        yield aiomqtt.Message(
            "tuya/bed/side_a/set", b"{}", qos=0, retain=True, mid=1, properties=None
        )


@pytest.fixture
def resolved(config_path: Path, secret_env: dict[str, str]) -> ResolvedConfig:
    return load_config(config_path, env=secret_env)


@pytest.fixture
def client_fake(
    resolved: ResolvedConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[adapter.Client, RawFake]:
    raw = RawFake()
    monkeypatch.setattr(adapter, "VerifiedClient", lambda *_args: raw)
    return adapter.Client(resolved, None), raw


async def test_wrapper_success_and_subscription_options(
    client_fake: tuple[adapter.Client, RawFake],
) -> None:
    client, raw = client_fake
    async with client as active:
        assert active is client
        await client.subscribe("tuya/bed/side_a/set")
        assert raw.options is not None
        assert raw.options.retainHandling == 2
        assert raw.options.retainAsPublished is True
        assert raw.options.QoS == 0
        await client.publish("tuya/bed/side_a", "{}", qos=1, retain=True)
        messages = [item async for item in client.messages]
        assert messages == [
            adapter.Message("tuya/bed/side_a/set", b"{}", retained=True)
        ]
    assert raw.closed == 1


@pytest.mark.parametrize(
    "failure", [aiomqtt.MqttError("private"), asyncio.CancelledError()]
)
async def test_enter_failure_cleans_transport(
    failure: BaseException, client_fake: tuple[adapter.Client, RawFake]
) -> None:
    client, raw = client_fake
    raw.failure = failure
    expected = (
        asyncio.CancelledError
        if isinstance(failure, asyncio.CancelledError)
        else adapter.MQTTFailureError
    )
    with pytest.raises(expected):
        await client.__aenter__()
    assert raw.closed == 1


@pytest.mark.parametrize("operation", ["subscribe", "publish", "messages", "exit"])
async def test_network_failures_are_sanitized(
    operation: str, client_fake: tuple[adapter.Client, RawFake]
) -> None:
    client, raw = client_fake
    raw.failure = aiomqtt.MqttError("credential-bearing object")

    async def invoke() -> None:
        if operation == "subscribe":
            await client.subscribe("topic")
        elif operation == "publish":
            await client.publish("topic", "body", qos=1, retain=True)
        elif operation == "messages":
            await anext(client.messages)
        else:
            await client.__aexit__(None, None, None)

    with pytest.raises(adapter.MQTTFailureError) as caught:
        await invoke()
    assert "credential" not in str(caught.value)


async def test_cleanup_failure_preserves_application_failure(
    client_fake: tuple[adapter.Client, RawFake],
) -> None:
    client, raw = client_fake
    raw.failure = aiomqtt.MqttError("disconnect failed")
    original = ValueError("application failure")
    with pytest.raises(ExceptionGroup) as caught:
        await client.__aexit__(ValueError, original, None)
    assert caught.value.exceptions[0] is original
    assert raw.closed == 1
    with pytest.raises(ExceptionGroup) as network:
        await client.__aexit__(
            adapter.MQTTFailureError, adapter.MQTTFailureError("network"), None
        )
    assert all(
        isinstance(error, adapter.MQTTFailureError)
        for error in network.value.exceptions
    )
    assert str(network.value.exceptions[0]) == "network"


@pytest.mark.parametrize("reasons", [[], [128], [0, 0], [1]])
async def test_suback_failure(
    client_fake: tuple[adapter.Client, RawFake], reasons: list[int]
) -> None:
    client, raw = client_fake
    raw.reasons = reasons
    with pytest.raises(adapter.MQTTFailureError, match="subscription rejected"):
        await client.subscribe("topic")


async def test_negative_puback_and_serial_publication(
    resolved: ResolvedConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = adapter.VerifiedClient(resolved, None)
    active = 0
    maximum = 0

    async def publish(
        _self: object, _topic: str, _payload: str, **_kwargs: object
    ) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        reason = ReasonCode(PacketTypes.PUBACK, identifier=135)
        client._on_publish(
            client._client, None, 1, reason, Properties(PacketTypes.PUBACK)
        )
        active -= 1

    monkeypatch.setattr(aiomqtt.Client, "publish", publish)
    results = await asyncio.gather(
        client.publish_checked("t", "p", qos=1, retain=True),
        client.publish_checked("t", "p", qos=1, retain=True),
        return_exceptions=True,
    )
    assert maximum == 1
    assert all(isinstance(error, aiomqtt.MqttError) for error in results)
    first_error = client._disconnected.exception()
    client._on_publish(
        client._client,
        None,
        1,
        ReasonCode(PacketTypes.PUBACK, identifier=135),
        Properties(PacketTypes.PUBACK),
    )
    assert client._disconnected.exception() is first_error
    client.close_transport()
    client._disconnected = asyncio.Future()
    client._disconnected.set_result(None)
    client.close_transport()
    client._disconnected = asyncio.Future()
    client._disconnected.cancel()
    client.close_transport()


async def test_buffered_command_cannot_escape_a_failed_connection(
    resolved: ResolvedConfig,
) -> None:
    client = adapter.Client(resolved, None)
    raw = client._client
    raw._queue.put_nowait(
        aiomqtt.Message(
            "tuya/bed/side_a/set", b"{}", qos=0, retain=False, mid=1, properties=None
        )
    )
    raw._disconnected.set_exception(aiomqtt.MqttError("connection failed"))
    try:
        with pytest.raises(adapter.MQTTFailureError, match="message stream failed"):
            await anext(client.messages)
    finally:
        raw.close_transport()


def test_tls_defaults_and_sanitized_file_errors(resolved: ResolvedConfig) -> None:
    assert adapter.tls_context(resolved) is None
    data = resolved.config.model_dump(mode="json")
    data["mqtt"]["tls"] = {}
    config = Config.model_validate_json(json.dumps(data))
    context = adapter.tls_context(replace(resolved, config=config))
    assert context.check_hostname is True
    data["mqtt"]["tls"] = {"ca_file": "/missing/credential.pem"}
    config = Config.model_validate_json(json.dumps(data))
    with pytest.raises(BridgeError, match="invalid TLS configuration"):
        adapter.tls_context(replace(resolved, config=config))


def test_tls_client_certificate_pair(
    resolved: ResolvedConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = resolved.config.model_dump(mode="json")
    data["mqtt"]["tls"] = {"cert_file": "cert.pem", "key_file": "key.pem"}
    config = Config.model_validate_json(json.dumps(data))
    certificates: list[tuple[str, str]] = []

    class Context:
        def load_cert_chain(self, cert: str, key: str) -> None:
            certificates.append((cert, key))

    monkeypatch.setattr(
        adapter.ssl, "create_default_context", lambda **_kwargs: Context()
    )
    adapter.tls_context(replace(resolved, config=config))
    assert certificates == [("cert.pem", "key.pem")]


def test_strict_incoming_queue_never_silently_discards() -> None:
    queue = adapter.StrictQueue(maxsize=1)
    message = aiomqtt.Message(
        "topic", b"body", qos=0, retain=False, mid=1, properties=None
    )
    queue.put_nowait(message)
    with pytest.raises(aiomqtt.MqttError, match="queue full"):
        queue.put_nowait(message)
    assert queue.qsize() == 1
