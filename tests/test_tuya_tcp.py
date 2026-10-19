import json
import socket
import threading
from contextlib import suppress
from time import monotonic
lazy from collections.abc import Callable, Iterator
lazy from pathlib import Path
lazy from typing import Any

import pytest
import tinytuya
from tuyamock import MockDevice

from tuya_mqtt.__main__ import main
from tuya_mqtt.controller import ControllerCore, ControllerWorker, Mailbox
from tuya_mqtt.domain import BridgeError, Completion, Outcome
from tuya_mqtt.profiles.sunbeam import parse_command
from tuya_mqtt.tuya_client import TinyTuyaClient
lazy from tuya_mqtt.config import ControllerConfig

from .controller_fakes import Log
from .tcp import BASELINE, KEY
lazy from .tcp import Simulator

pytestmark = pytest.mark.integration

VERSIONS = ["3.3", "3.4", "3.5"]
COUNTDOWN_DPS = ("28", "29", "102", "103")


def countdown_pushes(count: int) -> list[dict[str, object]]:
    return [{COUNTDOWN_DPS[number % 4]: 9000 - number} for number in range(count)]


PUSH_SETS = [
    *(
        pytest.param(countdown_pushes(count), id=f"{count}-pushes")
        for count in (0, 1, 2, 4, 8)
    ),
    pytest.param([{**BASELINE, "14": True, "20": "level_9"}], id="complete-stale-push"),
]


def connected_to(
    simulator: Simulator,
    controller_config: ControllerConfig,
    request_timeout: float,
    receive_slice: float = 0.25,
) -> TinyTuyaClient:
    config = controller_config.model_copy(
        update={"ip": "127.0.0.1", "protocol_version": simulator.mock.config.version}
    )
    return TinyTuyaClient(
        config,
        KEY,
        port=simulator.mock.port,
        request_timeout=request_timeout,
        receive_slice=receive_slice,
    )


@pytest.fixture
def tcp_client(
    simulator: Simulator, controller_config: ControllerConfig
) -> Iterator[TinyTuyaClient]:
    client = connected_to(simulator, controller_config, 0.2)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def patient_client(
    simulator: Simulator, controller_config: ControllerConfig
) -> Iterator[TinyTuyaClient]:
    client = connected_to(simulator, controller_config, 10, 0.05)
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


@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
@pytest.mark.parametrize("pushes", PUSH_SETS)
def test_connect_returns_the_reply_after_pushes(
    pushes: list[dict[str, object]],
    simulator: Simulator,
    patient_client: TinyTuyaClient,
) -> None:
    simulator.preludes.append(pushes)
    assert patient_client.connect() == BASELINE
    assert simulator.pushed == pushes
    assert patient_client.receive() is None
    assert simulator.mock.server.connections == 1


@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
@pytest.mark.parametrize("pushes", PUSH_SETS)
def test_status_returns_the_reply_after_pushes(
    pushes: list[dict[str, object]],
    simulator: Simulator,
    patient_client: TinyTuyaClient,
) -> None:
    assert patient_client.connect() == BASELINE
    simulator.preludes.append(pushes)
    assert patient_client.status() == BASELINE
    assert simulator.pushed == pushes
    assert patient_client.receive() is None
    assert simulator.mock.server.connections == 1


@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
def test_status_skips_a_write_response_that_is_still_queued(
    simulator: Simulator, patient_client: TinyTuyaClient
) -> None:
    assert patient_client.connect() == BASELINE
    patient_client.write({"20": "level_7", "14": True})
    assert patient_client.status() == simulator.mock.dps
    assert simulator.mock.dps["20"] == "level_7"
    assert patient_client.receive() is None


@pytest.fixture
def bounded_receives(monkeypatch: pytest.MonkeyPatch) -> None:
    receive = tinytuya.Device.receive
    reads = 0

    def bounded(device: tinytuya.Device) -> object:
        nonlocal reads
        reads += 1
        if reads > 20:
            pytest.fail("the reply wait outlived its deadline")
        return receive(device)

    monkeypatch.setattr(tinytuya.Device, "receive", bounded)


@pytest.mark.usefixtures("bounded_receives")
@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
@pytest.mark.parametrize("operation", ["connect", "status"])
def test_pushes_without_a_reply_fail_at_the_deadline(
    operation: str, simulator: Simulator, tcp_client: TinyTuyaClient
) -> None:
    if operation == "status":
        tcp_client.connect()
    simulator.mode = "drop_query_reply"
    pushes = countdown_pushes(2)
    simulator.preludes.append(pushes)
    with pytest.raises(BridgeError, match="status report missing"):
        getattr(tcp_client, operation)()
    assert simulator.pushed == pushes


@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
@pytest.mark.parametrize("operation", ["connect", "status"])
def test_a_reply_arriving_in_pieces_is_not_discarded(
    operation: str, simulator: Simulator, patient_client: TinyTuyaClient
) -> None:
    if operation == "status":
        patient_client.connect()
    pushes = countdown_pushes(1)
    simulator.preludes.append(pushes)
    simulator.split_gap = 0.3
    assert getattr(patient_client, operation)() == BASELINE
    assert simulator.pushed == pushes
    assert patient_client.receive() is None
    assert simulator.mock.server.connections == 1


@pytest.fixture
def reset_when_waiting(simulator: Simulator, monkeypatch: pytest.MonkeyPatch) -> None:
    receive = tinytuya.Device.receive
    pending = True

    def reset_then_receive(device: tinytuya.Device) -> object:
        nonlocal pending
        if pending:
            pending = False
            simulator.reset()
            simulator.mode = "normal"
        return receive(device)

    monkeypatch.setattr(tinytuya.Device, "receive", reset_then_receive)


@pytest.mark.usefixtures("reset_when_waiting")
@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
def test_connect_recovers_from_a_reset_while_waiting_for_the_reply(
    simulator: Simulator, patient_client: TinyTuyaClient
) -> None:
    pushes = countdown_pushes(1)
    simulator.preludes.append(pushes)
    simulator.mode = "drop_query_reply"
    assert patient_client.connect() == BASELINE
    assert simulator.pushed == pushes
    assert simulator.mock.server.connections == 2
    assert patient_client.receive() is None


@pytest.mark.usefixtures("reset_when_waiting")
@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
def test_status_surfaces_a_reset_while_waiting_for_the_reply(
    simulator: Simulator, patient_client: TinyTuyaClient
) -> None:
    patient_client.connect()
    simulator.preludes.append(countdown_pushes(1))
    simulator.mode = "drop_query_reply"
    with pytest.raises(BridgeError, match="device operation failed"):
        patient_client.status()
    with pytest.raises(BridgeError, match="not synchronized"):
        patient_client.status()
    assert simulator.mock.server.connections == 1


@pytest.mark.parametrize("simulator", ["3.3", "3.4"], indirect=True)
@pytest.mark.parametrize("count", [1, 2])
def test_device22_is_detected_behind_pushes(
    count: int, simulator: Simulator, patient_client: TinyTuyaClient
) -> None:
    simulator.mock.config.dev22 = True
    pushes = countdown_pushes(count)
    simulator.preludes.append(pushes)
    assert patient_client.connect() == BASELINE
    assert simulator.pushed == pushes
    assert simulator.rejected_queries == 1
    assert simulator.mock.server.connections == 1
    assert patient_client.status() == BASELINE


@pytest.mark.parametrize("simulator", ["3.3", "3.4"], indirect=True)
@pytest.mark.parametrize("seqno_mode", ["zero", "global"])
def test_connect_returns_the_reply_when_the_device_does_not_echo_the_sequence(
    seqno_mode: str, simulator: Simulator, patient_client: TinyTuyaClient
) -> None:
    simulator.mock.config.seqno_mode = seqno_mode
    pushes = countdown_pushes(2)
    simulator.preludes.append(pushes)
    assert patient_client.connect() == BASELINE
    assert simulator.pushed == pushes


@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
def test_inspect_prints_the_reply_after_pushes(
    simulator: Simulator,
    config_path: Path,
    config_data: dict[str, Any],
    secret_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_data["controllers"][0] |= {
        "ip": "127.0.0.1",
        "port": simulator.mock.port,
        "protocol_version": simulator.mock.config.version,
    }
    config_path.write_text(json.dumps(config_data))
    for name, value in secret_env.items():
        monkeypatch.setenv(name, value)
    pushes = countdown_pushes(4)
    simulator.preludes.append(pushes)
    status = main(["inspect", "--config", str(config_path), "--controller", "bed"])
    assert status == 0
    assert json.loads(capsys.readouterr().out) == BASELINE
    assert simulator.pushed == pushes


def wait_worker(predicate: Callable[[], bool], worker: ControllerWorker) -> None:
    deadline = monotonic() + 3
    pause = threading.Event()
    while not predicate():
        worker.check()
        assert monotonic() < deadline
        pause.wait(0.01)


def collect_completion(worker: ControllerWorker) -> Completion:
    results: list[Completion] = []

    def completed() -> bool:
        results.extend(worker.drain_results())
        return bool(results)

    wait_worker(completed, worker)
    assert len(results) == 1
    return results[0]


def worker_for(
    simulator: Simulator, config: ControllerConfig, request_timeout: float = 0.2
) -> ControllerWorker:
    config = config.model_copy(
        update={
            "ip": "127.0.0.1",
            "protocol_version": simulator.mock.config.version,
            "write_mode": "multi_dp",
        }
    )
    worker = ControllerWorker(
        config,
        lambda: TinyTuyaClient(
            config, KEY, port=simulator.mock.port, request_timeout=request_timeout
        ),
        logger=Log(),
    )
    worker.set_session("session")
    worker.start()
    return worker


@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
def test_controller_core_is_online_after_its_first_tick_despite_pushes(
    simulator: Simulator, controller_config: ControllerConfig
) -> None:
    config = controller_config.model_copy(
        update={"ip": "127.0.0.1", "protocol_version": simulator.mock.config.version}
    )
    mailbox = Mailbox()
    core = ControllerCore(
        config,
        lambda: TinyTuyaClient(
            config, KEY, port=simulator.mock.port, request_timeout=10
        ),
        mailbox,
        logger=Log(),
    )
    mailbox.set_session("session")
    pushes = countdown_pushes(4)
    simulator.preludes.append(pushes)
    try:
        core.tick()
        assert core.view.online
        assert core.view.last_full_status is not None
        assert simulator.pushed == pushes
        assert simulator.mock.server.connections == 1
    finally:
        core.stop()


@pytest.mark.parametrize("simulator", VERSIONS, indirect=True)
def test_worker_goes_online_despite_pushes_ahead_of_the_first_reply(
    simulator: Simulator, controller_config: ControllerConfig
) -> None:
    pushes = countdown_pushes(4)
    simulator.preludes.append(pushes)
    worker = worker_for(simulator, controller_config, request_timeout=10)
    try:
        wait_worker(lambda: worker.snapshot.online, worker)
        assert simulator.pushed == pushes
        assert simulator.mock.server.connections == 1
    finally:
        worker.stop()
        worker.join(3)


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
def test_worker_with_real_adapter_and_both_zones(
    simulator: Simulator, controller_config: ControllerConfig
) -> None:
    worker = worker_for(simulator, controller_config)
    try:
        wait_worker(lambda: worker.snapshot.online, worker)
        assert (
            worker.submit(
                0,
                parse_command(b'{"request_id":"zone-a","power":true,"heat_level":6}'),
                "session",
            )
            is None
        )
        first = collect_completion(worker)
        assert first.result.outcome is Outcome.CONFIRMED
        assert (
            worker.submit(
                1, parse_command(b'{"request_id":"zone-b","preheat":true}'), "session"
            )
            is None
        )
        assert collect_completion(worker).result.outcome is Outcome.CONFIRMED
        assert simulator.writes == [{"14": True, "20": "level_6"}, {"25": True}]
        physical_change = {"14": False, "20": "level_4"}
        simulator.mock.dps.update(physical_change)
        assert simulator.mock.push(physical_change)
        wait_worker(lambda: worker.snapshot.zones[0].power is False, worker)
        assert worker.snapshot.zones[0].heat_level == 4
        assert worker.snapshot.zones[1].preheat is True
        revision = worker.snapshot.refresh_revision
        worker.refresh()
        wait_worker(lambda: worker.snapshot.refresh_revision > revision, worker)
        assert simulator.mock.server.connections == 1
    finally:
        worker.stop()
        worker.join(3)


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
def test_worker_write_failure_reconnects_without_replay(
    simulator: Simulator, controller_config: ControllerConfig
) -> None:
    worker = worker_for(simulator, controller_config)
    try:
        wait_worker(lambda: worker.snapshot.online, worker)
        simulator.mode = "close"
        assert (
            worker.submit(0, parse_command(b'{"power":true,"heat_level":6}'), "session")
            is None
        )
        result = collect_completion(worker)
        assert result.result.outcome is Outcome.UNCONFIRMED
        assert simulator.writes == [{"14": True, "20": "level_6"}]
        wait_worker(
            lambda: worker.snapshot.online and worker.snapshot.generation >= 2, worker
        )
        assert worker.snapshot.zones[0].heat_level == 6
        assert worker.snapshot.zones[0].power is True
        assert simulator.writes == [{"14": True, "20": "level_6"}]
    finally:
        worker.stop()
        worker.join(3)


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
def test_worker_mqtt_session_change_uses_fresh_device_connection(
    simulator: Simulator, controller_config: ControllerConfig
) -> None:
    worker = worker_for(simulator, controller_config)
    try:
        wait_worker(lambda: worker.snapshot.online, worker)
        assert simulator.mock.push(BASELINE.copy())
        simulator.mock.dps["14"] = True
        worker.set_session("new-session")
        wait_worker(
            lambda: (
                worker.snapshot.online and worker.snapshot.session_id == "new-session"
            ),
            worker,
        )
        assert worker.snapshot.generation == 2
        assert worker.snapshot.zones[0].power is True
        assert simulator.mock.server.connections == 2
        assert simulator.writes == []
    finally:
        worker.stop()
        worker.join(3)


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
def test_stalled_negotiation_does_not_block_other_worker(
    simulator: Simulator, controller_config: ControllerConfig
) -> None:
    with MockDevice(local_key=KEY, version="3.5", go_dark_after=0) as stalled:
        config = controller_config.model_copy(
            update={"ip": "127.0.0.1", "protocol_version": "3.5"}
        )
        offline = ControllerWorker(
            config,
            lambda: TinyTuyaClient(
                config, KEY, port=stalled.port, request_timeout=0.05
            ),
            logger=Log(),
        )
        offline.set_session("session")
        offline.start()
        healthy = worker_for(simulator, controller_config)
        try:
            wait_worker(lambda: healthy.snapshot.online, healthy)
            assert (
                healthy.submit(0, parse_command(b'{"power":true}'), "session") is None
            )
            assert collect_completion(healthy).result.outcome is Outcome.CONFIRMED
            assert not offline.snapshot.online
            offline.check()
        finally:
            offline.stop()
            healthy.stop()
            offline.join(3)
            healthy.join(3)


@pytest.mark.parametrize("version", ["3.4", "3.5"])
def test_worker_shutdown_during_stalled_negotiation_with_default_timeouts(
    version: str, controller_config: ControllerConfig
) -> None:
    with MockDevice(local_key=KEY, version=version, go_dark_after=0) as stalled:
        config = controller_config.model_copy(
            update={"ip": "127.0.0.1", "protocol_version": version}
        )
        worker = ControllerWorker(
            config, lambda: TinyTuyaClient(config, KEY, port=stalled.port), logger=Log()
        )
        worker.start()
        try:
            wait_worker(lambda: stalled.server._fault_dark, worker)
            assert (
                worker.submit(0, parse_command(b'{"power":true}'), "session").outcome
                is Outcome.REJECTED
            )
        finally:
            worker.stop()
            worker.join()
        assert not worker.snapshot.online
