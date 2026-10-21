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

import json
from pathlib import Path
from typing import Any

import pytest

from tuya_mqtt.config import ControllerConfig
from tuya_mqtt.controller import ControllerCore, Mailbox
from tuya_mqtt.domain import BridgeError, CurtainState, ErrorCode, Outcome
from tuya_mqtt.profiles.zemismart import PROFILE
from tuya_mqtt.tuya_client import TinyTuyaClient

from .controller_fakes import Clock, Device, Log
from .test_controller import Harness
from .test_tuya_client import PUSH, QUERY, Frame, RawFake

OBSERVED = json.loads(
    (Path(__file__).parent / "fixtures/zemismart-bcd300fb.json").read_text()
)


@pytest.mark.parametrize("version", ["3.4", "3.5"])
def test_rejects_unqualified_protocols(
    zemismart_config: ControllerConfig, version: str
) -> None:
    with pytest.raises(ValueError, match="unsupported protocol"):
        ControllerConfig.model_validate(
            zemismart_config.model_dump() | {"protocol_version": version}
        )


@pytest.mark.parametrize(
    ("report", "position"),
    [("open_report", 100), ("closed_report", 0), ("halfway_report", 50)],
)
def test_observed_positions_and_writes(report: str, position: int) -> None:
    dps = OBSERVED[report]
    updates = PROFILE.decode_report(dps, 0)
    state = PROFILE.merge_report(CurtainState(), updates.updates)
    assert state == CurtainState(position, position)
    assert state.usable
    command = PROFILE.parse_command(
        json.dumps({"target_position": position}).encode(), "r"
    )
    assert PROFILE.write_dps(command, 0, "multi_dp") == {"101": dps["101"]}
    with pytest.raises(BridgeError) as error:
        PROFILE.write_dps(command, 0, "disabled")
    assert error.value.code is ErrorCode.WRITE_DISABLED


@pytest.mark.parametrize("raw", [True, False, -1, 101, 46.0, "46", None])
def test_invalid_position_clears_baseline(raw: Any) -> None:
    report = PROFILE.decode_report({"101": raw}, 0)
    assert report.invalid_fields
    assert PROFILE.merge_report(CurtainState(54, 80), report.updates) == CurtainState()


def test_operation_and_reversal_are_not_motion() -> None:
    for dps in ({"102": "STOP"}, {"102": "ZZ"}, {"103": True}, {"999": 2}):
        report = PROFILE.decode_report(dps, 0)
        assert report.updates == ()
        assert PROFILE.merge_report(
            CurtainState(54, 80), report.updates
        ) == CurtainState(54, 80)


@pytest.fixture
def curtain(zemismart_config: ControllerConfig) -> Harness:
    clock = Clock()
    device = Device(clock, state=OBSERVED["restored_report"].copy(), apply_writes=False)
    mailbox, log = Mailbox(), Log()
    core = ControllerCore(
        zemismart_config,
        lambda: device,
        mailbox,
        clock=clock,
        jitter=lambda: 0,
        logger=log,
    )
    harness = Harness(core, mailbox, device, clock, log)
    harness.online()
    return harness


def submit(curtain: Harness, target: int = 80) -> None:
    request = curtain.request(
        PROFILE.parse_command(json.dumps({"target_position": target}).encode(), "r")
    )
    assert curtain.mailbox.admit(request, writes_enabled=True) is None
    curtain.tick()
    assert curtain.device.writes[-1] == {"101": 100 - target}


def test_acknowledgement_confirms_before_arrival_and_survives_polls(
    curtain: Harness,
) -> None:
    assert curtain.core.view.endpoints == (CurtainState(54, 54),)
    submit(curtain)
    curtain.tick(2)
    assert not curtain.mailbox.drain_results()
    assert curtain.core.view.endpoints == (CurtainState(54, 54),)
    curtain.device.write_acknowledged = True
    curtain.tick(2)
    assert curtain.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert curtain.core.view.endpoints == (CurtainState(54, 80),)
    curtain.mailbox.refresh()
    curtain.tick()
    assert curtain.core.view.endpoints == (CurtainState(54, 80),)
    curtain.push({"101": 20})
    assert curtain.core.view.endpoints == (CurtainState(80, 80),)
    curtain.push({"101": 60})
    assert curtain.core.view.endpoints == (CurtainState(40, 40),)
    assert curtain.device.writes == [{"101": 20}]


def test_same_position_report_cannot_confirm_without_ack(curtain: Harness) -> None:
    submit(curtain, 54)
    curtain.tick(4)
    assert not curtain.mailbox.drain_results()
    curtain.clock.advance(9)
    curtain.tick(3)
    result = curtain.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.UNCONFIRMED
    assert result.error_code is ErrorCode.CONFIRMATION_TIMEOUT


def test_late_ack_cannot_change_target(curtain: Harness) -> None:
    submit(curtain)
    curtain.device.write_acknowledged = True
    curtain.clock.advance(9)
    curtain.mailbox.refresh()
    curtain.tick()
    assert curtain.core.view.endpoints == (CurtainState(54, 54),)
    curtain.tick(3)
    assert curtain.mailbox.drain_results()[0].result.outcome is Outcome.UNCONFIRMED


@pytest.mark.parametrize("acknowledged", [True, False])
def test_reconnect_resets_target_without_replay(
    curtain: Harness, *, acknowledged: bool
) -> None:
    submit(curtain)
    curtain.device.write_acknowledged = acknowledged
    curtain.tick(2)
    curtain.device.pushes.append(BridgeError(ErrorCode.CONNECTION_LOST, "closed"))
    curtain.tick(3)
    assert not curtain.core.view.online
    curtain.clock.advance(2)
    curtain.tick(3)
    assert curtain.core.view.endpoints == (CurtainState(54, 54),)
    assert curtain.core.view.generation == 2
    assert curtain.device.writes == [{"101": 20}]


def test_session_reset_drops_accepted_target(curtain: Harness) -> None:
    submit(curtain)
    curtain.device.write_acknowledged = True
    curtain.tick(2)
    curtain.mailbox.set_session("new")
    curtain.tick(3)
    assert curtain.core.view.endpoints == (CurtainState(54, 54),)
    assert curtain.device.writes == [{"101": 20}]


class AckRaw(RawFake):
    def __init__(self) -> None:
        super().__init__()
        self.response = {"dps": OBSERVED["restored_report"].copy()}
        self.sequence = 1

    def send(self, payload: object) -> object:
        self.calls.append(("send", payload))
        self.sequence += 1
        self.raw_sent = Frame(seqno=self.sequence)
        self.raw_recv = []
        return None


@pytest.fixture
def adapter(zemismart_config: ControllerConfig) -> tuple[TinyTuyaClient, AckRaw]:
    raw = AckRaw()
    client = TinyTuyaClient(
        zemismart_config, "test-key-16-byte", factory=lambda *_a, **_k: raw
    )
    client.connect()
    client.write({"101": 20})
    raw.response = None
    return client, raw


@pytest.mark.parametrize("frame", [Frame(cmd=9), Frame(seqno=1), Frame(seqno=0)])
def test_unrelated_acks_do_not_confirm(
    adapter: tuple[TinyTuyaClient, AckRaw], frame: Frame
) -> None:
    client, raw = adapter
    raw.frames.append((frame, None))
    assert client.receive() is None
    assert not client.write_acknowledged


@pytest.mark.parametrize("operation", ["receive", "status"])
@pytest.mark.parametrize(
    "frame", [Frame(retcode=1), Frame(retcode=None), Frame(crc_good=False)]
)
def test_invalid_control_ack_fails_connection(
    adapter: tuple[TinyTuyaClient, AckRaw], frame: Frame, operation: str
) -> None:
    client, raw = adapter
    raw.frames.append((frame, None))
    with pytest.raises(BridgeError) as error:
        getattr(client, operation)()
    assert error.value.code is ErrorCode.DEVICE_ERROR
    assert not client.write_acknowledged
    with pytest.raises(BridgeError, match="not synchronized"):
        client.write({"101": 50})


def test_observed_ack_identity_is_bound_to_each_write(
    adapter: tuple[TinyTuyaClient, AckRaw],
) -> None:
    client, raw = adapter
    ack = OBSERVED["close_trace"][2]["data"]
    raw.frames.append(
        (
            Frame(ack["command"], ack["sequence"], ack["return_code"], ack["crc_good"]),
            None,
        )
    )
    assert client.receive() is None
    assert client.write_acknowledged
    client.write({"101": 50})
    raw.frames.append((Frame(), None))
    client.receive()
    assert not client.write_acknowledged
    raw.frames.append((Frame(seqno=3), None))
    client.receive()
    assert client.write_acknowledged
    client.close()
    assert not client.write_acknowledged


@pytest.mark.parametrize("header", [None, Frame(cmd=13)])
def test_missing_control_identity_fails_closed(
    adapter: tuple[TinyTuyaClient, AckRaw],
    monkeypatch: pytest.MonkeyPatch,
    header: Frame | None,
) -> None:
    client, raw = adapter
    raw.raw_sent = header
    monkeypatch.setattr(raw, "send", lambda _payload: None)
    with pytest.raises(BridgeError) as error:
        client.write({"101": 50})
    assert error.value.code is ErrorCode.COMPATIBILITY
    assert not client.write_acknowledged


@pytest.mark.parametrize(
    "order",
    [
        pytest.param(("ack",), id="answers the query call"),
        pytest.param(("push", "ack"), id="behind a push"),
        pytest.param(("ack", "push"), id="ahead of a push"),
    ],
)
def test_ack_read_while_a_status_query_waits_for_its_reply_is_recorded(
    adapter: tuple[TinyTuyaClient, AckRaw], order: tuple[str, ...]
) -> None:
    client, raw = adapter
    report = OBSERVED["restored_report"]
    frames = {
        "ack": (Frame(seqno=raw.sequence), None),
        "push": (PUSH, {"dps": {"101": 80}}),
    }
    raw.frames.extend([*(frames[name] for name in order), (QUERY, {"dps": report})])
    assert client.status() == report
    assert not raw.frames
    assert client.write_acknowledged
