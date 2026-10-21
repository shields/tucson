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
from uuid import UUID

import pytest

from tuya_mqtt.domain import (
    BridgeError,
    Command,
    CommandResult,
    ErrorCode,
    Field,
    Outcome,
    ZoneState,
)
from tuya_mqtt.json_codec import InvalidJSONError, decode
from tuya_mqtt.profiles.sunbeam import (
    DP_MAP,
    LEVELS,
    REQUIRED_DPS,
    TIMERS,
    decode_report,
    encode_command,
    parse_command,
    write_dps,
)


@pytest.mark.parametrize("zone", [0, 1])
@pytest.mark.parametrize("level", range(1, 11))
def test_levels_and_zone_isolation(zone: int, level: int) -> None:
    report = decode_report({DP_MAP[zone][1]: f"level_{level}"}, zone)
    assert report.updates == ((Field.HEAT_LEVEL, level),)
    assert not report.invalid_fields
    assert not decode_report({DP_MAP[1 - zone][1]: "level_10"}, zone).updates
    command = parse_command(json.dumps({"heat_level": level}).encode())
    assert encode_command(command, zone) == {DP_MAP[zone][1]: f"level_{level}"}


@pytest.mark.parametrize(
    ("zone", "enum", "seconds"),
    [
        (zone, enum, seconds)
        for zone in (0, 1)
        for enum, seconds in TIMERS[zone].items()
    ],
)
def test_timer_table(zone: int, enum: str, seconds: int) -> None:
    report = decode_report({DP_MAP[zone][3]: enum}, zone)
    assert report.updates == ((Field.AUTO_OFF_SECONDS, seconds),)
    command = parse_command(json.dumps({"auto_off_seconds": seconds}).encode())
    assert encode_command(command, zone) == {DP_MAP[zone][3]: enum}
    assert TIMERS[zone]["Stay_On"] == 86400


def test_each_side_has_its_own_spelling_of_seven_and_a_half_hours() -> None:
    assert TIMERS[0]["7h30"] == TIMERS[1]["7h30m"] == 27000
    assert TIMERS[0].keys() ^ TIMERS[1].keys() == {"7h30", "7h30m"}
    assert len(TIMERS[0]) == len(TIMERS[1]) == 21
    for zone, rejected in ((0, "7h30m"), (1, "7h30")):
        report = decode_report({DP_MAP[zone][3]: rejected}, zone)
        assert report.invalid_fields == (Field.AUTO_OFF_SECONDS,)
    command = parse_command(b'{"auto_off_seconds":27000}')
    assert encode_command(command, 0) == {"26": "7h30"}
    assert encode_command(command, 1) == {"27": "7h30m"}


def test_power_off_must_stand_alone() -> None:
    assert parse_command(b'{"power":false}').values == ((Field.POWER, False),)
    for mixed in (
        Command("r", ((Field.POWER, False), (Field.HEAT_LEVEL, 3))),
        Command("r", ((Field.HEAT_LEVEL, 3), (Field.POWER, False))),
    ):
        with pytest.raises(BridgeError) as caught:
            write_dps(mixed, 0, "multi_dp")
        assert caught.value.code == ErrorCode.INVALID_COMMAND


@pytest.mark.parametrize("zone", [0, 1])
@pytest.mark.parametrize("boolean", [True, False])
def test_boolean_and_countdown_fields(zone: int, *, boolean: bool) -> None:
    report = decode_report(
        dict(
            zip(
                DP_MAP[zone],
                [boolean, "level_4", boolean, "1h", 0, 86400, 5],
                strict=True,
            )
        ),
        zone,
    )
    state = ZoneState().merge(report.updates)
    assert state.usable
    assert state.remaining_seconds == 0
    assert state.preheat_remaining_seconds == 86400
    assert state.fault_code == 5
    assert state.payload("session")["power"] is boolean
    assert state.payload("session")["heat_level"] == 4
    assert not report.invalid_fields
    for field in ("power", "preheat"):
        command = parse_command(json.dumps({field: boolean}).encode())
        assert command.matches(state)
        assert encode_command(command, zone) == {
            DP_MAP[zone][0 if field == "power" else 2]: boolean
        }


@pytest.mark.parametrize("zone", [0, 1])
@pytest.mark.parametrize(
    ("index", "value"),
    [
        (0, 1),
        (0, "true"),
        (0, None),
        (1, True),
        (1, "level_0"),
        (1, "level_11"),
        (1, "level_01"),
        (2, 0),
        (3, 3600),
        (3, "unknown"),
        (4, True),
        (4, -1),
        (4, 1.0),
        (4, 2**53),
        (5, "30"),
        (5, 2**53),
        (6, True),
        (6, -1),
        (6, 1.0),
        (6, "1"),
        (6, 2**53),
        (6, 10**400),
    ],
)
def test_invalid_report_invalidates_only_affected_field(
    zone: int, index: int, value: object
) -> None:
    state = ZoneState(power=True, heat_level=4, preheat=False)
    field = tuple(Field)[index]
    report = decode_report({DP_MAP[zone][index]: value}, zone)
    assert report.invalid_fields == (field,)
    merged = state.merge(report.updates)
    assert getattr(merged, field) is None
    if field != Field.POWER:
        assert merged.power is True


@pytest.mark.parametrize("zone", [0, 1])
@pytest.mark.parametrize("fault_code", [0, 1, 0b1_0000_0000_0001, 2**32, 2**53 - 1])
def test_fault_code_is_reported_opaquely_for_both_zones(
    zone: int, fault_code: int
) -> None:
    report = decode_report({"13": fault_code}, zone)
    assert report.updates == ((Field.FAULT_CODE, fault_code),)
    assert not report.invalid_fields
    state = ZoneState().merge(report.updates)
    assert state.fault_code == fault_code
    assert state.payload("session")["fault_code"] == fault_code
    assert not state.usable


def test_fault_dp_is_requested_once_for_both_zones() -> None:
    assert DP_MAP[0][-1] == DP_MAP[1][-1] == "13"
    assert REQUIRED_DPS.count("13") == 1
    assert len(REQUIRED_DPS) == len(set(REQUIRED_DPS))


def test_partial_merges_and_unknown_fields() -> None:
    state = ZoneState()
    assert not state.usable
    assert state.payload("s")["power"] is None
    assert decode_report({"999": "unrelated"}, 0).updates == ()
    state = state.merge(decode_report({"14": False, "20": "level_4"}, 0).updates)
    assert not state.usable
    state = state.merge(decode_report({"24": False}, 0).updates)
    assert state.usable
    assert state.power is False
    assert state.heat_level == 4
    assert state.auto_off_seconds is None
    assert not ZoneState().merge(decode_report({"14": True}, 0).updates).usable


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * 4097,
        b"\xff",
        b"{",
        b"[]",
        b"null",
        b"{}",
        b'{"power":true,"power":false}',
        b'{"power":NaN}',
        b'{"heat_level":Infinity}',
        b'{"heat_level":1e999}',
        b'{"power":1}',
        b'{"power":"true"}',
        b'{"heat_level":true}',
        b'{"heat_level":0}',
        b'{"heat_level":11}',
        b'{"heat_level":1.0}',
        b'{"preheat":0}',
        b'{"auto_off_seconds":true}',
        b'{"auto_off_seconds":1}',
        b'{"remaining_seconds":0}',
        b'{"fault_code":3600}',
        b'{"other":true}',
        b'{"request_id":"only"}',
        b'{"request_id":null,"power":true}',
        b'{"request_id":1,"power":true}',
        b'{"request_id":"","power":true}',
        b'{"request_id":"\\n","power":true}',
        json.dumps({"request_id": "x" * 129, "power": True}).encode(),
        b'{"preheat":true,"power":false}',
        b'{"preheat":false,"heat_level":2}',
        b'{"power":false,"heat_level":3}',
        b'{"power":false,"auto_off_seconds":3600}',
    ],
)
def test_invalid_command(payload: bytes) -> None:
    with pytest.raises(BridgeError) as caught:
        parse_command(payload)
    assert caught.value.code == ErrorCode.INVALID_COMMAND


def test_command_normalization_and_level_only_encoding() -> None:
    first = parse_command(b'{"request_id":"r","heat_level":6,"power":true}')
    second = parse_command(b'{"power":true,"heat_level":6,"request_id":"r"}')
    assert first == second
    assert first.matches(ZoneState(power=True, heat_level=6))
    assert not first.matches(ZoneState(power=False, heat_level=6))
    level_only = parse_command(b'{"heat_level":4}', generated_id="generated")
    assert level_only.request_id == "generated"
    assert encode_command(level_only, 0) == {"20": "level_4"}
    assert UUID(parse_command(b'{"power":false}').request_id)
    assert len(LEVELS) == 10


def test_result_payload() -> None:
    assert CommandResult("r", Outcome.CONFIRMED).payload() == {
        "request_id": "r",
        "outcome": "confirmed",
    }
    assert CommandResult("r", Outcome.REJECTED, ErrorCode.UNAVAILABLE).payload() == {
        "request_id": "r",
        "outcome": "rejected",
        "error_code": "unavailable",
    }


def test_json_nested_duplicates_and_nonfinite() -> None:
    for payload in (b'{"nested":{"a":1,"a":2}}', b'{"a":NaN}'):
        with pytest.raises(InvalidJSONError, match="invalid JSON"):
            decode(payload)
