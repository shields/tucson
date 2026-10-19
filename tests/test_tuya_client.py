from collections import deque
from dataclasses import dataclass
from itertools import count
lazy from collections.abc import Callable

import pytest
import tinytuya

from tuya_mqtt.domain import BridgeError, ErrorCode, Value
from tuya_mqtt.profiles.sunbeam import REQUIRED_DPS
from tuya_mqtt.tuya_client import TinyTuyaClient, sanitized_dps
lazy from tuya_mqtt.config import ControllerConfig

from .controller_fakes import BASELINE

READ_LIMIT = 50
QUERY = tinytuya.DP_QUERY
HEARTBEAT = tinytuya.HEART_BEAT
PUSH = tinytuya.STATUS
DEVICE22_REJECTION = tinytuya.error_json(tinytuya.ERR_DEVTYPE)
RESET = tinytuya.error_json(tinytuya.ERR_CONNECT)
REPLY = {"dps": BASELINE}


@dataclass(frozen=True)
class Frame:
    cmd: int = tinytuya.CONTROL
    seqno: int = 2
    retcode: int | None = 0
    crc_good: bool = True


class RawFake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.response: object = {"dps": {"14": True}}
        self.failure: Exception | None = None
        self.closed = 0
        self.raw_sent: Frame | None = None
        self.raw_recv: list[Frame] = []
        self.frames: deque[tuple[int | Frame | None, object]] = deque()
        self.sent_is_unparsed = False
        self.reads = 0
        self.now = 0.0
        self.delays: deque[float] = deque()

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

    def read(self) -> object:
        self.reads += 1
        if self.reads > READ_LIMIT:
            pytest.fail("read past the request deadline")
        if self.delays:
            self.now += self.delays.popleft()
        if self.failure is not None:
            raise self.failure
        if self.frames:
            frame, result = self.frames.popleft()
            if isinstance(frame, int):
                frame = Frame(frame)
            self.raw_recv = [] if frame is None else [frame]
            return result
        self.raw_recv = [Frame(QUERY)]
        return self.response

    def status(self) -> object:
        self.calls.append(("status", None))
        self.raw_sent = None if self.sent_is_unparsed else Frame(QUERY)
        return self.read()

    def receive(self) -> object:
        self.calls.append(("receive", None))
        return self.read()

    def heartbeat(self, *, nowait: bool) -> object:
        self.calls.append(("heartbeat", nowait))
        return self.read()

    def generate_payload(self, command: int, data: dict[str, Value | str]) -> object:
        self.calls.append(("generate", command))
        return data

    def send(self, payload: object) -> object:
        self.calls.append(("send", payload))
        return self.read()

    def clock(self) -> float:
        return self.now

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
        ("send_wait", 0),
        ("retry", False),
        ("request_dps", dict.fromkeys(REQUIRED_DPS)),
        ("timeout", 2.0),
        ("status", None),
        ("request_dps", dict.fromkeys(REQUIRED_DPS)),
        ("timeout", 2.0),
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


def pushes(count: int) -> list[tuple[int | None, object]]:
    return [(PUSH, {"dps": {"29": 600 - number}}) for number in range(count)]


@pytest.mark.parametrize("operation", ["connect", "status"])
@pytest.mark.parametrize(
    "ahead",
    [
        pytest.param([], id="nothing"),
        pytest.param(pushes(1), id="push"),
        pytest.param(pushes(3), id="three pushes"),
        pytest.param([(PUSH, {"dps": {**BASELINE, "14": True}})], id="complete push"),
        pytest.param([(HEARTBEAT, None)], id="heartbeat ACK"),
        pytest.param([(QUERY, None), (QUERY, {})], id="empty replies"),
        pytest.param([*pushes(1), (None, None)], id="push then silence"),
    ],
)
def test_query_returns_the_reply_that_follows_other_frames(
    operation: str,
    ahead: list[tuple[int | None, object]],
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    if operation == "status":
        raw.response = REPLY
        client.connect()
        raw.calls.clear()
    raw.frames.extend([*ahead, (QUERY, REPLY)])
    assert getattr(client, operation)() == BASELINE
    assert not raw.frames
    assert raw.calls.count(("receive", None)) == len(ahead)
    assert raw.calls.count(("status", None)) == 1


@pytest.mark.parametrize("operation", ["connect", "status"])
def test_query_without_a_reply_fails_at_the_deadline(
    operation: str, fake_adapter: tuple[TinyTuyaClient, RawFake]
) -> None:
    client, raw = fake_adapter
    if operation == "status":
        raw.response = REPLY
        client.connect()
        raw.calls.clear()
    raw.frames.extend([*pushes(100), (QUERY, REPLY)])
    with pytest.raises(BridgeError, match="status report missing"):
        getattr(client, operation)()
    assert raw.calls.count(("receive", None)) == 3
    assert len(raw.frames) == 97
    with pytest.raises(BridgeError, match="not synchronized"):
        client.heartbeat()


def test_reply_waits_last_until_the_deadline(
    controller_config: ControllerConfig,
) -> None:
    raw = RawFake()
    raw.frames.extend([*pushes(100), (QUERY, REPLY)])
    ticks = count(step=0.5)
    client = TinyTuyaClient(
        controller_config,
        "test-key-16-byte",
        request_timeout=1.1,
        factory=lambda *_args, **_kwargs: raw,
        clock=lambda: next(ticks),
    )
    with pytest.raises(BridgeError, match="status report missing"):
        client.connect()
    timeouts = [seconds for name, seconds in raw.calls if name == "timeout"]
    assert timeouts == [1.1, pytest.approx(0.6), pytest.approx(0.1)]


def test_a_slow_connection_does_not_consume_the_time_allowed_for_the_reply(
    controller_config: ControllerConfig,
) -> None:
    raw = RawFake()
    raw.delays.append(10)
    raw.frames.extend([*pushes(1), (QUERY, REPLY)])
    client = TinyTuyaClient(
        controller_config,
        "test-key-16-byte",
        factory=lambda *_args, **_kwargs: raw,
        clock=raw.clock,
    )
    assert client.connect() == BASELINE


def test_status_allows_one_request_timeout_in_all(
    controller_config: ControllerConfig,
) -> None:
    raw = RawFake()
    raw.response = REPLY
    client = TinyTuyaClient(
        controller_config,
        "test-key-16-byte",
        factory=lambda *_args, **_kwargs: raw,
        clock=raw.clock,
    )
    client.connect()
    raw.calls.clear()
    raw.delays.extend([1.5, 0.5])
    raw.frames.extend([*pushes(1), (None, None)])
    with pytest.raises(BridgeError, match="status report missing"):
        client.status()
    timeouts = [seconds for name, seconds in raw.calls if name == "timeout"]
    assert timeouts == [2.0, pytest.approx(0.5)]
    assert raw.now == pytest.approx(2.0)


def test_a_reply_is_not_assumed_when_the_query_is_unknown(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    raw.response = REPLY
    raw.sent_is_unparsed = True
    with pytest.raises(BridgeError, match="status report missing"):
        client.connect()
    assert raw.calls.count(("receive", None)) == 3


def test_device22_rejection_behind_a_push_asks_again(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    raw.frames.extend(
        [
            *pushes(1),
            (QUERY, DEVICE22_REJECTION),
            (QUERY, {"dps": {"14": False}}),
            (QUERY, REPLY),
        ]
    )
    assert client.connect() == BASELINE
    request_dps = ("request_dps", dict.fromkeys(REQUIRED_DPS))
    assert [
        call
        for call in raw.calls
        if call[0] in ("status", "receive", "request_dps", "timeout", "retry_limit")
    ] == [
        ("retry_limit", 1),
        request_dps,
        ("timeout", 2.0),
        ("status", None),
        ("timeout", 1.5),
        ("retry_limit", 0),
        ("receive", None),
        ("retry_limit", 1),
        ("timeout", 2.0),
        ("status", None),
        request_dps,
        ("timeout", 2.0),
        ("status", None),
        ("retry_limit", 0),
    ]


def test_device22_rejections_are_bounded_by_the_deadline(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    raw.frames.extend([*pushes(1), *[(QUERY, DEVICE22_REJECTION)] * 100])
    with pytest.raises(BridgeError, match="status report missing"):
        client.connect()
    assert raw.calls.count(("status", None)) == 3
    assert len(raw.frames) == 97


def waits(raw: RawFake) -> list[tuple[str, object]]:
    return [
        call
        for call in raw.calls
        if call[0] in ("status", "receive", "retry_limit", "timeout")
    ]


def test_connect_asks_again_once_after_a_reset_while_waiting(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    raw.frames.extend([*pushes(1), (None, RESET), (QUERY, REPLY)])
    assert client.connect() == BASELINE
    assert not raw.frames
    assert waits(raw) == [
        ("retry_limit", 1),
        ("timeout", 2.0),
        ("status", None),
        ("timeout", 1.5),
        ("retry_limit", 0),
        ("receive", None),
        ("retry_limit", 1),
        ("timeout", 2.0),
        ("status", None),
        ("retry_limit", 0),
    ]


def test_connect_fails_when_the_connection_is_reset_again_while_waiting(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    raw.frames.extend([*pushes(1), (None, RESET), *pushes(1), (None, RESET)])
    with pytest.raises(BridgeError, match="synchronization failed"):
        client.connect()
    assert raw.calls.count(("status", None)) == 2
    assert raw.calls.count(("receive", None)) == 2
    assert raw.closed == 1


def test_status_surfaces_a_reset_while_waiting(
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    raw.response = REPLY
    client.connect()
    raw.calls.clear()
    raw.frames.extend([*pushes(1), (None, RESET), (QUERY, REPLY)])
    with pytest.raises(BridgeError, match="device operation failed"):
        client.status()
    assert len(raw.frames) == 1
    assert waits(raw) == [
        ("timeout", 2.0),
        ("status", None),
        ("timeout", 1.5),
        ("retry_limit", 0),
        ("receive", None),
        ("retry_limit", 0),
    ]
    with pytest.raises(BridgeError, match="not synchronized"):
        client.heartbeat()


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(TinyTuyaClient.receive, id="receive"),
        pytest.param(TinyTuyaClient.heartbeat, id="heartbeat"),
        pytest.param(lambda client: client.write({"14": True}), id="write"),
    ],
)
@pytest.mark.parametrize(
    "response", [{"Err": "secret"}, {"Error": "secret"}, {"dps": "secret"}, 1]
)
def test_invalid_response_invalidates_connection(
    operation: Callable[[TinyTuyaClient], object],
    response: object,
    fake_adapter: tuple[TinyTuyaClient, RawFake],
) -> None:
    client, raw = fake_adapter
    client.connect()
    raw.response = response
    with pytest.raises(BridgeError):
        operation(client)
    with pytest.raises(BridgeError, match="not synchronized"):
        client.status()


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
