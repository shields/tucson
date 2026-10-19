from itertools import count

import pytest

from tuya_mqtt.domain import BridgeError, ErrorCode, Value
from tuya_mqtt.profiles.sunbeam import REQUIRED_DPS
from tuya_mqtt.tuya_client import TinyTuyaClient, sanitized_dps
lazy from tuya_mqtt.config import ControllerConfig
lazy from tuya_mqtt.tuya_client import RawHeader, RawMessage


class RawFake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.response: object = {"dps": {"14": True}}
        self.failure: Exception | None = None
        self.closed = 0
        self.raw_sent: RawHeader | None = None
        self.raw_recv: list[RawMessage] = []

    def set_socketPersistent(self, *, persist: bool) -> None:
        self.calls.append(("persistent", persist))

    def set_socketRetryLimit(self, limit: int) -> None:
        self.calls.append(("retry_limit", limit))

    def set_socketRetryDelay(self, delay: float) -> None:
        self.calls.append(("retry_delay", delay))

    def set_socketTimeout(self, seconds: float) -> None:
        self.calls.append(("timeout", seconds))

    def set_retry(self, *, retry: bool) -> None:
        self.calls.append(("retry", retry))

    def set_sendWait(self, seconds: float) -> None:
        self.calls.append(("send_wait", seconds))

    def set_dpsUsed(self, dps: dict[str, None]) -> None:
        self.calls.append(("request_dps", dps))

    def status(self) -> object:
        self.calls.append(("status", None))
        if self.failure is not None:
            raise self.failure
        return self.response

    def receive(self) -> object:
        return self.status()

    def heartbeat(self, *, nowait: bool) -> object:
        self.calls.append(("heartbeat", nowait))
        return self.status()

    def generate_payload(self, command: int, data: dict[str, Value | str]) -> object:
        self.calls.append(("generate", command))
        return data

    def send(self, payload: object) -> object:
        self.calls.append(("send", payload))
        return self.status()

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def fake_adapter(controller_config: ControllerConfig) -> tuple[TinyTuyaClient, RawFake]:
    raw = RawFake()
    ticks = count(step=0.5)
    client = TinyTuyaClient(
        controller_config,
        "test-key-16-byte",
        factory=lambda *_args, **_kwargs: raw,
        clock=lambda: next(ticks),
    )
    return client, raw


def test_public_controls_and_send_only_write(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    assert client.connect() == {"14": True}
    assert raw.calls == [
        ("persistent", True),
        ("retry_limit", 1),
        ("retry_delay", 0),
        ("timeout", 2.0),
        ("send_wait", 0),
        ("retry", False),
        ("request_dps", dict.fromkeys(REQUIRED_DPS)),
        ("status", None),
        ("request_dps", dict.fromkeys(REQUIRED_DPS)),
        ("status", None),
        ("retry_limit", 0),
    ]
    assert client.status() == {"14": True}
    assert client.receive() == {"14": True}
    assert ("timeout", 0.25) in raw.calls
    raw.response = None
    assert client.heartbeat() is None
    assert ("heartbeat", True) in raw.calls
    assert client.write({"20": "level_4", "14": True}) is None
    assert ("send", {"20": "level_4", "14": True}) in raw.calls
    client.close()
    client.close()
    assert raw.closed == 1


@pytest.mark.parametrize(("missing", "queries"), [("13", 1), ("28", 2)])
def test_only_the_fault_dp_may_be_missing_from_a_complete_status(
    fake_adapter: tuple[TinyTuyaClient, RawFake], missing: str, queries: int
) -> None:
    client, raw = fake_adapter
    report = {dp: 0 for dp in REQUIRED_DPS if dp != missing}
    raw.response = {"dps": report}
    assert client.connect() == report
    assert [name for name, _value in raw.calls].count("status") == queries
    assert raw.calls.count(("request_dps", dict.fromkeys(REQUIRED_DPS))) == queries


def test_unavailable_and_new_device_connection(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    with pytest.raises(BridgeError) as caught:
        client.write({"14": True})
    assert caught.value.code == ErrorCode.UNAVAILABLE
    assert not raw.calls
    client.connect()
    client.connect()
    assert raw.closed == 1
    client.close()
    assert raw.closed == 2


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"Err": "secret"},
        "secret",
        {"dps": []},
        {"dps": {1: True}},
        {"dps": {"id": "secret"}},
    ],
)
def test_connect_failure_is_sanitized(
    response: object, fake_adapter: tuple[TinyTuyaClient, RawFake]
) -> None:
    client, raw = fake_adapter
    raw.response = response
    with pytest.raises(BridgeError) as caught:
        client.connect()
    assert caught.value.code == ErrorCode.CONNECTION_LOST
    assert "secret" not in str(caught.value)
    assert raw.closed == 1


@pytest.mark.parametrize("exception", [OSError("secret"), ValueError("secret")])
def test_connection_exception_is_sanitized(
    exception: Exception, fake_adapter: tuple[TinyTuyaClient, RawFake]
) -> None:
    client, raw = fake_adapter
    raw.failure = exception
    with pytest.raises(BridgeError, match="device synchronization failed"):
        client.connect()
    assert raw.closed == 1


@pytest.mark.parametrize("exception", [OSError("secret"), ValueError("secret")])
def test_operation_exception_invalidates_connection(
    exception: Exception, fake_adapter: tuple[TinyTuyaClient, RawFake]
) -> None:
    client, raw = fake_adapter
    client.connect()
    raw.failure = exception
    with pytest.raises(BridgeError, match="device connection lost"):
        client.write({"14": True})
    with pytest.raises(BridgeError, match="not synchronized"):
        client.write({"14": True})


@pytest.mark.parametrize("response", [None, {}])
def test_status_requires_a_report(
    response: object, fake_adapter: tuple[TinyTuyaClient, RawFake]
) -> None:
    client, raw = fake_adapter
    client.connect()
    raw.response = response
    assert client.receive() is None
    with pytest.raises(BridgeError, match="status report missing"):
        client.status()
    with pytest.raises(BridgeError, match="not synchronized"):
        client.status()


@pytest.mark.parametrize(
    "response", [{"Err": "secret"}, {"Error": "secret"}, {"dps": "secret"}, 1]
)
def test_invalid_response_invalidates_connection(
    response: object, fake_adapter: tuple[TinyTuyaClient, RawFake]
) -> None:
    client, raw = fake_adapter
    client.connect()
    raw.response = response
    with pytest.raises(BridgeError):
        client.receive()
    with pytest.raises(BridgeError, match="not synchronized"):
        client.heartbeat()


def test_unexpected_exception_surfaces(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    client.connect()
    raw.failure = RuntimeError("application defect")
    with pytest.raises(RuntimeError, match="application defect"):
        client.status()


def test_sanitized_inspection() -> None:
    assert sanitized_dps(
        {"14": True, "999": {"secret": ["prefixsecret", None, 3]}}, ("secret",)
    ) == {"14": True, "999": {"[redacted]": ["prefix[redacted]", None, 3]}}
