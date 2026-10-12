import socket
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
lazy from collections.abc import Iterator

import pytest
from tuyamock import MockDevice, Session

from tuya_mqtt.domain import BridgeError
from tuya_mqtt.profiles.sunbeam import REQUIRED_DPS
from tuya_mqtt.tuya_client import TinyTuyaClient
lazy from tuya_mqtt.config import ControllerConfig

pytestmark = pytest.mark.integration
KEY = "test-key-16-byte"
BASELINE = dict(
    zip(REQUIRED_DPS, [False, "level_4", False, "1h", 3600, 0] * 2, strict=True)
)


@dataclass
class Simulator:
    mock: MockDevice
    writes: list[dict[str, object]] = field(default_factory=list)
    written: threading.Event = field(default_factory=threading.Event)
    mode: str = "normal"


@pytest.fixture
def simulator(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Simulator]:
    mock = MockDevice(local_key=KEY, version=request.param, dps=BASELINE.copy())
    simulation = Simulator(mock)
    original = Session._handle_control

    def handle(session: Session, message: Any) -> bytes | None:
        values = {
            key: value
            for key, value in session._parse_dps(message).items()
            if value is not None
        }
        response = original(session, message)
        if values:
            simulation.writes.append(values)
            if simulation.mode == "close":
                mock.server._client.shutdown(socket.SHUT_RDWR)
                response = None
            elif simulation.mode == "empty_ack":
                response = session.profile.pack_response(session, message.cmd, b"")
            elif simulation.mode == "drop_reply":
                response = None
            simulation.written.set()
        return response

    monkeypatch.setattr(Session, "_handle_control", handle)
    mock.start()
    thread = mock._thread
    try:
        yield simulation
    finally:
        mock.stop()
        assert not thread.is_alive()


@pytest.fixture
def tcp_client(
    simulator: Simulator, controller_config: ControllerConfig
) -> Iterator[TinyTuyaClient]:
    config = controller_config.model_copy(
        update={"ip": "127.0.0.1", "protocol_version": simulator.mock.config.version}
    )
    client = TinyTuyaClient(
        config, KEY, port=simulator.mock.port, request_timeout=0.2, receive_slice=0.25
    )
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
def test_real_library_all_methods_and_both_zones(
    simulator: Simulator, tcp_client: TinyTuyaClient
) -> None:
    assert tcp_client.connect() == BASELINE
    assert tcp_client.receive() is None
    for dps in (
        {"20": "level_6", "14": True},
        {"21": "level_8", "15": True},
        {"24": True},
        {"25": False},
        {"26": "Stay_On"},
        {"27": "30m"},
    ):
        simulator.written.clear()
        assert tcp_client.write(dps) is None
        assert simulator.written.wait(2)
        assert tcp_client.receive() == dps
    assert len(simulator.writes) == 6
    assert simulator.writes[0] == {"20": "level_6", "14": True}
    tcp_client.heartbeat()
    assert tcp_client.receive() is None
    assert tcp_client.status() == simulator.mock.dps
    assert simulator.mock.push({"14": False, "20": "level_4"})
    assert tcp_client.receive() == {"14": False, "20": "level_4"}
    assert simulator.mock.server.connections == 1


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
@pytest.mark.parametrize("mode", ["empty_ack", "drop_reply", "close", "go_dark"])
def test_single_write_attempt_under_transport_faults(
    mode: str, simulator: Simulator, tcp_client: TinyTuyaClient
) -> None:
    tcp_client.connect()
    simulator.mode = mode
    if mode == "go_dark":
        simulator.mock.server.go_dark_after = simulator.mock.server._fault_resp_count
    tcp_client.write({"20": "level_7", "14": True})
    assert simulator.written.wait(2)
    assert simulator.writes == [{"20": "level_7", "14": True}]
    assert simulator.mock.dps["20"] == "level_7"
    if mode in ("close", "go_dark"):
        with pytest.raises(BridgeError):
            tcp_client.status()
        with pytest.raises(BridgeError):
            tcp_client.write({"14": True})
        assert simulator.mock.server.connections == 1
        simulator.mode = "normal"
        simulator.mock.server.go_dark_after = None
        assert tcp_client.connect()["20"] == "level_7"
    else:
        if mode == "empty_ack":
            assert tcp_client.receive() is None
        assert tcp_client.status()["20"] == "level_7"
    assert simulator.writes == [{"20": "level_7", "14": True}]


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
def test_connection_closed_before_write(
    simulator: Simulator, tcp_client: TinyTuyaClient
) -> None:
    tcp_client.connect()
    simulator.mock.server._client.shutdown(socket.SHUT_RDWR)
    with suppress(BridgeError):
        tcp_client.write({"14": True})
    with pytest.raises(BridgeError):
        tcp_client.status()
    with pytest.raises(BridgeError):
        tcp_client.write({"14": True})
    assert simulator.writes == []
    assert simulator.mock.server.connections == 1
    assert tcp_client.connect()["14"] is False


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
def test_delayed_write_report_beyond_request_timeout(
    simulator: Simulator, tcp_client: TinyTuyaClient
) -> None:
    tcp_client.connect()
    simulator.mock.server.response_delay = 0.5
    tcp_client.write({"24": True})
    assert simulator.written.wait(2)
    with pytest.raises(BridgeError):
        tcp_client.status()
    assert simulator.writes == [{"24": True}]
    simulator.mock.server.response_delay = 0
    assert tcp_client.connect()["24"] is True
    assert simulator.writes == [{"24": True}]


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
def test_status_handles_a_pending_empty_heartbeat_ack(
    simulator: Simulator, tcp_client: TinyTuyaClient
) -> None:
    tcp_client.connect()
    tcp_client.heartbeat()
    assert tcp_client.status() == BASELINE
    assert simulator.mock.server.connections == 1


@pytest.mark.parametrize("version", ["3.3", "3.4", "3.5"])
def test_wrong_key_is_bounded(
    version: str, controller_config: ControllerConfig
) -> None:
    with MockDevice(local_key=KEY, version=version, dps=BASELINE.copy()) as mock:
        config = controller_config.model_copy(
            update={"ip": "127.0.0.1", "protocol_version": version}
        )
        client = TinyTuyaClient(config, KEY[::-1], port=mock.port, request_timeout=0.05)
        try:
            with pytest.raises(BridgeError, match="synchronization failed"):
                client.connect()
        finally:
            client.close()


@pytest.mark.parametrize("version", ["3.4", "3.5"])
def test_stalled_negotiation(version: str, controller_config: ControllerConfig) -> None:
    with MockDevice(local_key=KEY, version=version, go_dark_after=0) as mock:
        config = controller_config.model_copy(
            update={"ip": "127.0.0.1", "protocol_version": version}
        )
        client = TinyTuyaClient(config, KEY, port=mock.port, request_timeout=0.05)
        try:
            with pytest.raises(BridgeError, match="synchronization failed"):
                client.connect()
            assert mock.server.connections == 1
        finally:
            client.close()


@pytest.mark.parametrize("version", ["3.3", "3.4"])
def test_device22_requests_full_profile(
    version: str, controller_config: ControllerConfig
) -> None:
    with MockDevice(
        local_key=KEY, version=version, dps=BASELINE.copy(), dev22=True
    ) as mock:
        config = controller_config.model_copy(
            update={"ip": "127.0.0.1", "protocol_version": version}
        )
        client = TinyTuyaClient(config, KEY, port=mock.port, request_timeout=0.2)
        try:
            assert client.connect() == BASELINE
            assert mock.server.connections == 1
            assert client.status() == BASELINE
        finally:
            client.close()
