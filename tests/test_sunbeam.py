import json
from uuid import UUID

import pytest

from tuya_mqtt.domain import (
    BridgeError,
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
    TIMERS,
    decode_report,
    encode_command,
    parse_command,
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


@pytest.mark.parametrize("zone", [0, 1])
@pytest.mark.parametrize(("enum", "seconds"), TIMERS.items())
def test_timer_table(zone: int, enum: str, seconds: int) -> None:
    report = decode_report({DP_MAP[zone][3]: enum}, zone)
    assert report.updates == ((Field.AUTO_OFF_SECONDS, seconds),)
    command = parse_command(json.dumps({"auto_off_seconds": seconds}).encode())
    assert encode_command(command, zone) == {DP_MAP[zone][3]: enum}
    assert TIMERS["Stay_On"] == 86400


@pytest.mark.parametrize("zone", [0, 1])
@pytest.mark.parametrize("boolean", [True, False])
def test_boolean_and_countdown_fields(zone: int, *, boolean: bool) -> None:
    report = decode_report(
        dict(
            zip(
                DP_MAP[zone],
                [boolean, "level_4", boolean, "1h", 0, 86400],
                strict=True,
            )
        ),
        zone,
    )
    state = ZoneState().merge(report.updates)
    assert state.usable
    assert state.remaining_seconds == 0
    assert state.preheat_remaining_seconds == 86400
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
        (5, "30"),
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
        b'{"other":true}',
        b'{"request_id":"only"}',
        b'{"request_id":null,"power":true}',
        b'{"request_id":1,"power":true}',
        b'{"request_id":"","power":true}',
        b'{"request_id":"\\n","power":true}',
        json.dumps({"request_id": "x" * 129, "power": True}).encode(),
        b'{"preheat":true,"power":false}',
        b'{"preheat":false,"heat_level":2}',
    ],
)
def test_invalid_command(payload: bytes) -> None:
    with pytest.raises(BridgeError) as caught:
        parse_command(payload)
    assert caught.value.code == ErrorCode.INVALID_COMMAND


def test_command_normalization_and_no_implicit_power() -> None:
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
