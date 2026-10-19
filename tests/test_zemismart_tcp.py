from time import monotonic
lazy from collections.abc import Iterator

import pytest

from tuya_mqtt.controller import ControllerWorker
from tuya_mqtt.domain import CurtainState, Outcome
from tuya_mqtt.profiles.zemismart import PROFILE
from tuya_mqtt.tuya_client import TinyTuyaClient
lazy from tuya_mqtt.config import ControllerConfig

from .controller_fakes import Log
from .tcp import KEY
from .test_tuya_tcp import wait_worker
from .test_zemismart import OBSERVED
lazy from .tcp import Simulator

pytestmark = pytest.mark.integration


@pytest.fixture
def motor(
    simulator: Simulator, zemismart_config: ControllerConfig
) -> Iterator[ControllerWorker]:
    simulator.mock.dps.clear()
    simulator.mock.dps.update(OBSERVED["restored_report"])
    simulator.mode = "curtain"
    config = zemismart_config.model_copy(update={"ip": "127.0.0.1"})
    worker = ControllerWorker(
        config,
        lambda: TinyTuyaClient(config, KEY, port=simulator.mock.port),
        logger=Log(),
    )
    worker.set_session("s")
    worker.start()
    try:
        wait_worker(lambda: worker.snapshot.online, worker)
        yield worker
    finally:
        worker.stop()
        worker.join()


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
def test_real_adapter_confirms_ack_and_observes_completion(
    simulator: Simulator, motor: ControllerWorker
) -> None:
    assert motor.snapshot.zones == (CurtainState(54, 54),)
    started = monotonic()
    assert (
        motor.submit(0, PROFILE.parse_command(b'{"target_position":80}', "r"), "s")
        is None
    )
    wait_worker(lambda: motor.snapshot.last_completion is not None, motor)
    assert motor.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert monotonic() - started < 5
    assert motor.snapshot.zones == (CurtainState(54, 80),)
    motor.refresh()
    wait_worker(lambda: motor.snapshot.refresh_revision >= 2, motor)
    assert motor.snapshot.zones == (CurtainState(54, 80),)
    motor.set_session("new")
    wait_worker(
        lambda: motor.snapshot.session_id == "new" and motor.snapshot.online, motor
    )
    assert motor.snapshot.zones == (CurtainState(54, 54),)
    simulator.mock.dps["101"] = 20
    assert simulator.mock.push({"101": 20})
    wait_worker(lambda: motor.snapshot.zones == (CurtainState(80, 80),), motor)
    assert simulator.writes == [{"101": 20}]


@pytest.mark.parametrize("simulator", ["3.3"], indirect=True)
def test_echoed_position_does_not_replace_missing_control_ack(
    simulator: Simulator, motor: ControllerWorker
) -> None:
    simulator.mode = "drop_reply"
    assert (
        motor.submit(0, PROFILE.parse_command(b'{"target_position":80}', "r"), "s")
        is None
    )
    assert simulator.written.wait(2)
    wait_worker(lambda: motor.snapshot.zones == (CurtainState(80, 80),), motor)
    assert motor.snapshot.last_completion is None
    motor.stop()
    motor.join()
    assert motor.drain_results()[0].result.outcome is Outcome.UNCONFIRMED
    assert simulator.writes == [{"101": 20}]
