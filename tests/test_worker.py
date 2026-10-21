# Copyright © 2026 Michael Shields
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
from time import monotonic
lazy from collections.abc import Callable

import pytest

from tuya_mqtt.controller import ControllerWorker, WorkerFailureError
from tuya_mqtt.domain import Completion, ConnectionState, ErrorCode, Outcome
from tuya_mqtt.profiles.sunbeam import parse_command
lazy from tuya_mqtt.config import ControllerConfig

from .controller_fakes import Clock, Device, Log


def eventually(predicate: Callable[[], bool], worker: ControllerWorker) -> None:
    deadline = monotonic() + 3
    pause = threading.Event()
    while not predicate():
        worker.check()
        assert monotonic() < deadline
        pause.wait(0.005)


def test_one_owner_thread_for_factory_and_both_zones(
    controller_config: ControllerConfig,
) -> None:
    config = controller_config.model_copy(update={"write_mode": "multi_dp"})
    clock = Clock()
    device = Device(clock)
    factory_ids: set[int] = set()
    pause = threading.Event()
    device.receive_hook = lambda: pause.wait(0.01)

    def factory() -> Device:
        factory_ids.add(threading.get_ident())
        return device

    worker = ControllerWorker(config, factory, clock=clock, logger=Log())
    worker.join()
    assert (
        worker.submit(0, parse_command(b'{"power":true}'), "session").error_code
        is ErrorCode.UNAVAILABLE
    )
    worker.set_session("session")
    worker.start()
    try:
        eventually(lambda: worker.snapshot.online, worker)
        assert worker.queue_depth == 0
        assert (
            worker.submit(
                0, parse_command(b'{"request_id":"a","power":true}'), "session"
            )
            is None
        )
        assert (
            worker.submit(
                1, parse_command(b'{"request_id":"b","preheat":true}'), "session"
            )
            is None
        )
        completed: list[Completion] = []

        def finished() -> bool:
            completed.extend(worker.drain_results())
            return len(completed) == 2

        eventually(finished, worker)
        assert [item.result.request_id for item in completed] == ["a", "b"]
        assert all(item.result.outcome is Outcome.CONFIRMED for item in completed)
        assert device.writes == [{"14": True}, {"25": True}]
        assert worker.snapshot.endpoints[0].power is True
        assert worker.snapshot.endpoints[1].preheat is True
        revision = worker.snapshot.refresh_revision
        worker.refresh()
        eventually(lambda: worker.snapshot.refresh_revision > revision, worker)
        with pytest.raises(WorkerFailureError, match="already started"):
            worker.start()
    finally:
        worker.stop()
        worker.join(2)
    assert device.owner_ids == factory_ids
    assert len(factory_ids) == 1
    assert threading.get_ident() not in factory_ids
    assert device.calls[-1][0] == "close"
    assert worker.snapshot.connection is ConnectionState.STOPPING


@pytest.mark.parametrize("mode", ["disabled", "multi_dp"])
def test_admission_session_shutdown_and_queue_bounds(
    mode: str, controller_config: ControllerConfig
) -> None:
    config = controller_config.model_copy(update={"write_mode": mode})
    clock = Clock()
    device = Device(clock)
    parked, release = threading.Event(), threading.Event()

    def receive() -> None:
        parked.set()
        assert release.wait(3)

    device.receive_hook = receive
    worker = ControllerWorker(config, lambda: device, clock=clock)
    worker.set_session("session")
    worker.start()
    try:
        assert parked.wait(3)
        command = parse_command(b'{"power":true}')
        if mode == "disabled":
            assert (
                worker.submit(0, command, "session").error_code
                is ErrorCode.WRITE_DISABLED
            )
        else:
            for _ in range(16):
                assert worker.submit(0, command, "session") is None
            assert worker.queue_depth == 16
            assert (
                worker.submit(0, command, "session").error_code is ErrorCode.QUEUE_FULL
            )
        worker.set_session("new-session")
        assert worker.submit(0, command, "session").error_code is ErrorCode.UNAVAILABLE
        assert (
            worker.submit(0, command, "new-session").error_code is ErrorCode.UNAVAILABLE
        )
        worker.stop()
        assert (
            worker.submit(0, command, "new-session").error_code is ErrorCode.UNAVAILABLE
        )
    finally:
        worker.stop()
        release.set()
        worker.join(2)
    assert not device.writes
    assert all(
        item.result.outcome is Outcome.REJECTED for item in worker.drain_results()
    )


def test_unexpected_worker_failure_is_fatal_and_sanitized(
    controller_config: ControllerConfig,
) -> None:
    def factory() -> Device:
        message = "unexpected failure containing a secret"
        raise ValueError(message)

    worker = ControllerWorker(controller_config, factory, logger=Log())
    worker.start()
    with pytest.raises(WorkerFailureError, match="controller worker failed") as caught:
        worker.join(2)
    assert "secret" not in str(caught.value)
    with pytest.raises(WorkerFailureError, match="controller worker failed"):
        worker.check()


def test_close_failure_is_fatal(controller_config: ControllerConfig) -> None:
    device = Device(Clock())
    device.close_error = RuntimeError("unexpected close failure")
    entered, release = threading.Event(), threading.Event()

    def receive() -> None:
        entered.set()
        assert release.wait(3)

    device.receive_hook = receive
    worker = ControllerWorker(
        controller_config, lambda: device, clock=device.clock, logger=Log()
    )
    worker.start()
    try:
        assert entered.wait(3)
        worker.stop()
        release.set()
        with pytest.raises(WorkerFailureError, match="controller worker failed"):
            worker.join(2)
    finally:
        release.set()


def test_shutdown_bound_violation_is_fatal(controller_config: ControllerConfig) -> None:
    entered, release = threading.Event(), threading.Event()

    class StalledDevice(Device):
        def connect(self) -> dict[str, object]:
            entered.set()
            assert release.wait(3)
            return super().connect()

    device = StalledDevice(Clock())
    worker = ControllerWorker(
        controller_config, lambda: device, clock=device.clock, logger=Log()
    )
    worker.start()
    try:
        assert entered.wait(3)
        worker.stop()
        with pytest.raises(WorkerFailureError, match="exceeded shutdown bound"):
            worker.join(0)
    finally:
        release.set()
        worker.join(2)
    assert device.calls[-1][0] == "close"


def test_receipt_time_prevents_a_delayed_command_start(
    controller_config: ControllerConfig,
) -> None:
    config = controller_config.model_copy(update={"write_mode": "multi_dp"})
    clock = Clock(now=10)
    device = Device(clock)
    pause = threading.Event()
    device.receive_hook = lambda: pause.wait(0.01)
    worker = ControllerWorker(config, lambda: device, clock=clock, logger=Log())
    worker.set_session("session")
    worker.start()
    completed: list[Completion] = []

    def finished() -> bool:
        completed.extend(worker.drain_results())
        return bool(completed)

    try:
        eventually(lambda: worker.snapshot.online, worker)
        command = parse_command(b'{"request_id":"delayed","power":true}')
        assert worker.submit(0, command, "session", received_at=0) is None
        eventually(finished, worker)
        assert completed[0].result.outcome is Outcome.REJECTED
        assert completed[0].result.error_code is ErrorCode.EXPIRED
        assert completed[0].request.received_at == 0
        assert not device.writes
    finally:
        worker.stop()
        worker.join(2)
