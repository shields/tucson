import json
from typing import Any

import pytest

from tuya_mqtt.config import ControllerConfig
from tuya_mqtt.controller import ControllerCore, Mailbox
from tuya_mqtt.domain import BridgeError, CurtainField, CurtainState, ErrorCode, Outcome
from tuya_mqtt.profiles import PROFILES
from tuya_mqtt.profiles.curtain import parse_command

from .controller_fakes import Clock, Device, Log
from .curtain_profile import BASELINE, PROFILE
from .test_controller import Harness


@pytest.fixture
def curtain_config(
    monkeypatch: pytest.MonkeyPatch, controller_config: ControllerConfig
) -> ControllerConfig:
    monkeypatch.setitem(PROFILES, "test-curtain", PROFILE)
    data = controller_config.model_dump(exclude={"zones"}) | {
        "profile": "test-curtain",
        "write_mode": "multi_dp",
    }
    return ControllerConfig.model_validate(data)


@pytest.mark.parametrize("value", [0, 1, 50, 99, 100])
def test_position_command(value: int) -> None:
    command = parse_command(
        json.dumps({"target_position": value}).encode(), "generated"
    )
    assert command.request_id == "generated"
    assert command.values == ((CurtainField.TARGET_POSITION, value),)
    assert PROFILE.write_dps(command, 0, "multi_dp") == {"202": 100 - value}


@pytest.mark.parametrize(
    "payload",
    [
        b"x",
        b"[]",
        b"{}",
        b'{"stop":false}',
        b'{"stop":1}',
        b'{"target_position":true}',
        b'{"target_position":-1}',
        b'{"target_position":101}',
        b'{"target_position":1.0}',
        b'{"target_position":NaN}',
        b'{"stop":true,"target_position":20}',
        b'{"stop":true,"extra":1}',
        b'{"stop":true,"stop":true}',
        b"x" * 4097,
    ],
)
def test_invalid_commands(payload: bytes) -> None:
    with pytest.raises(BridgeError) as error:
        parse_command(payload)
    assert error.value.code is ErrorCode.INVALID_COMMAND


@pytest.mark.parametrize("identifier", [None, 1, "", "x" * 129, "\n"])
def test_invalid_identifiers(identifier: Any) -> None:
    with pytest.raises(BridgeError):
        parse_command(
            json.dumps({"target_position": 50, "request_id": identifier}).encode()
        )


def test_generated_identifier_and_disabled_writes() -> None:
    command = parse_command(b'{"target_position":50}')
    assert command.request_id
    with pytest.raises(BridgeError):
        PROFILE.write_dps(command, 0, "disabled")


def test_production_rejects_unverified_profiles(
    controller_config: ControllerConfig,
) -> None:
    with pytest.raises(BridgeError):
        ControllerConfig.model_validate(
            controller_config.model_dump() | {"profile": "zemismart-curtain"}
        )


def test_profile_zone_validation(
    curtain_config: ControllerConfig, controller_config: ControllerConfig
) -> None:
    assert curtain_config.zones is None
    with pytest.raises(ValueError, match="zones apply only"):
        ControllerConfig.model_validate(
            curtain_config.model_dump() | {"zones": ("curtain",)}
        )
    with pytest.raises(ValueError, match="zone count"):
        ControllerConfig.model_validate(controller_config.model_dump(exclude={"zones"}))


def test_curtain_report_and_state() -> None:
    state = CurtainState()
    assert not state.usable
    assert state.fault_code is None
    state = state.merge(PROFILE.decode_report(BASELINE, 0).updates)
    assert state.payload("s") == {
        "session_id": "s",
        "current_position": 0,
        "target_position": 0,
        "motion": "stopped",
    }
    assert state.usable
    report = PROFILE.decode_report({"201": True, "202": 101, "203": "bogus"}, 0)
    assert set(report.invalid_fields) == set(PROFILE.fields)
    assert not state.merge(report.updates).usable
    assert PROFILE.decode_report({"999": 1}, 0).updates == ()


def test_controller_confirms_target_before_arrival(
    curtain_config: ControllerConfig,
) -> None:
    clock = Clock()
    device = Device(clock, state=BASELINE.copy())
    mailbox = Mailbox()
    log = Log()
    core = ControllerCore(
        curtain_config,
        lambda: device,
        mailbox,
        clock=clock,
        jitter=lambda: 0,
        logger=log,
    )
    harness = Harness(core, mailbox, device, clock, log)
    harness.online()
    assert len(core.view.endpoints) == 1
    assert (
        mailbox.admit(
            harness.request(parse_command(b'{"target_position":80}')),
            writes_enabled=True,
        )
        is None
    )
    harness.tick(6)
    assert mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert core.view.endpoints[0].current_position == 0
    harness.push({"201": 70, "203": "opening"})
    assert device.writes == [{"202": 20}]
    mailbox.set_session("new-session")
    device.state = {"201": 70}
    harness.tick(3)
    assert not core.view.online
    assert core.view.previous_endpoints[0].current_position == 30
    assert core.view.endpoints[0].target_position is None
    core.stop()
