from types import MappingProxyType
from typing import cast
from uuid import uuid4
lazy from collections.abc import Mapping

from tuya_mqtt.domain import (
    BridgeError,
    Command,
    EndpointState,
    ErrorCode,
    Field,
    Update,
    Value,
    ZoneState,
)
from tuya_mqtt.json_codec import InvalidJSONError, decode

from .base import (
    MAX_PAYLOAD_BYTES,
    MAX_REQUEST_ID,
    MIN_PRINTABLE,
    Profile,
    WriteMode,
    ZoneReport,
)

MIN_LEVEL = 1
MAX_LEVEL = 10
# Beyond this, adjacent integers share a float64 or a JavaScript number.
MAX_SAFE_INTEGER = 2**53 - 1
_TIMER_SECONDS: Mapping[str, int] = MappingProxyType(
    {
        "30m": 1800,
        "1h": 3600,
        "1h30m": 5400,
        "2h": 7200,
        "2h30m": 9000,
        "3h": 10800,
        "3h30m": 12600,
        "4h": 14400,
        "4h30m": 16200,
        "5h": 18000,
        "5h30m": 19800,
        "6h": 21600,
        "6h30m": 23400,
        "7h": 25200,
        "7h30m": 27000,
        "8h": 28800,
        "8h30m": 30600,
        "9h": 32400,
        "9h30m": 34200,
        "10h": 36000,
        "Stay_On": 86400,
    }
)
# Side A's firmware spells 7.5 hours "7h30". It rejects "7h30m" and then resets the
# countdown to 24 hours, so each side needs its own spelling.
TIMERS: tuple[Mapping[str, int], Mapping[str, int]] = (
    MappingProxyType(
        {
            ("7h30" if name == "7h30m" else name): seconds
            for name, seconds in _TIMER_SECONDS.items()
        }
    ),
    _TIMER_SECONDS,
)
TIMER_SECONDS = frozenset(_TIMER_SECONDS.values())
LEVELS: Mapping[str, int] = MappingProxyType(
    {f"level_{level}": level for level in range(MIN_LEVEL, MAX_LEVEL + 1)}
)
# DP 13 is the controller-wide fault bitmap, so both zones report it.
FAULT_DP = "13"
DP_MAP = (
    ("14", "20", "24", "26", "28", "102", FAULT_DP),
    ("15", "21", "25", "27", "29", "103", FAULT_DP),
)
REQUIRED_DPS = tuple(dict.fromkeys(dp for zone in DP_MAP for dp in zone))
WRITABLE = (Field.POWER, Field.HEAT_LEVEL, Field.PREHEAT, Field.AUTO_OFF_SECONDS)


def write_dps(command: Command, zone: int, mode: WriteMode) -> dict[str, Value | str]:
    if mode == "disabled":
        raise BridgeError(ErrorCode.WRITE_DISABLED, "controller writes are disabled")
    fields = [field for field, _value in command.values]
    if (
        not fields
        or len(fields) != len(set(fields))
        or any(
            field not in WRITABLE or not _valid_value(field, value)
            for field, value in command.values
        )
        or (Field.PREHEAT in fields and len(fields) != 1)
        or ((Field.POWER, False) in command.values and len(fields) != 1)
    ):
        raise BridgeError(ErrorCode.INVALID_COMMAND, "invalid command values")
    return encode_command(command, zone)


def _decode_field(field: Field, raw: object, zone: int) -> Value | None:
    if field in (Field.POWER, Field.PREHEAT):
        return raw if type(raw) is bool else None
    if field is Field.HEAT_LEVEL:
        return LEVELS.get(raw) if isinstance(raw, str) else None
    if field is Field.AUTO_OFF_SECONDS:
        return TIMERS[zone].get(raw) if isinstance(raw, str) else None
    return raw if type(raw) is int and 0 <= raw <= MAX_SAFE_INTEGER else None


def decode_report(dps: Mapping[str, object], zone: int) -> ZoneReport:
    updates: list[Update] = []
    invalid: list[Field] = []
    for field, dp in zip(Field, DP_MAP[zone], strict=True):
        if dp in dps:
            value = _decode_field(field, dps[dp], zone)
            updates.append((field, value))
            if value is None:
                invalid.append(field)
    return ZoneReport(tuple(updates), tuple(invalid))


def _valid_value(field: Field, value: object) -> bool:
    if field in (Field.POWER, Field.PREHEAT):
        return type(value) is bool
    if field is Field.HEAT_LEVEL:
        return type(value) is int and MIN_LEVEL <= value <= MAX_LEVEL
    return type(value) is int and value in TIMER_SECONDS


def parse_command(payload: bytes, *, generated_id: str | None = None) -> Command:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise BridgeError(ErrorCode.INVALID_COMMAND, "command payload too large")
    try:
        data = decode(payload)
    except InvalidJSONError:
        raise BridgeError(ErrorCode.INVALID_COMMAND, "invalid command JSON") from None
    if (
        not isinstance(data, dict)
        or not data
        or data.keys() - {*WRITABLE, "request_id"}
    ):
        raise BridgeError(
            ErrorCode.INVALID_COMMAND, "unknown or missing command fields"
        )
    request_id = data.get("request_id", generated_id or str(uuid4()))
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id) <= MAX_REQUEST_ID
        or any(ord(char) < MIN_PRINTABLE for char in request_id)
    ):
        raise BridgeError(ErrorCode.INVALID_COMMAND, "invalid request ID")
    values: list[tuple[Field, Value]] = []
    for field in WRITABLE:
        if field in data:
            value = data[field]
            if not _valid_value(field, value):
                raise BridgeError(ErrorCode.INVALID_COMMAND, "invalid command value")
            values.append((field, value))
    # The firmware turns a side on when a level is written, even in the same
    # packet as power off, so power off must stand alone.
    if (
        not values
        or (Field.PREHEAT in data and len(values) != 1)
        or (data.get(Field.POWER) is False and len(values) != 1)
    ):
        raise BridgeError(ErrorCode.INVALID_COMMAND, "unsupported command combination")
    return Command(request_id, tuple(values))


def encode_command(command: Command, zone: int) -> dict[str, Value | str]:
    result: dict[str, Value | str] = {}
    mapping = dict(zip(Field, DP_MAP[zone], strict=True))
    for field, value in command.values:
        if field is Field.HEAT_LEVEL:
            encoded: Value | str = f"level_{value}"
        elif field is Field.AUTO_OFF_SECONDS:
            encoded = next(
                enum for enum, seconds in TIMERS[zone].items() if seconds == value
            )
        else:
            encoded = value
        result[mapping[cast("Field", field)]] = encoded
    return result


def prepare_command(command: Command, state: EndpointState) -> Command:
    state = cast("ZoneState", state)
    fields = {field for field, _value in command.values}
    if state.power is not False or Field.POWER in fields:
        return command
    # Only an explicit power write starts the firmware's presence-confirmation window.
    if Field.HEAT_LEVEL in fields:
        return Command(command.request_id, ((Field.POWER, True), *command.values))
    if Field.AUTO_OFF_SECONDS in fields:
        raise BridgeError(ErrorCode.ZONE_OFF, "timer for a side that is off")
    return command


REQUIRED_FIELDS = frozenset((Field.POWER, Field.HEAT_LEVEL, Field.PREHEAT))
PROFILE = Profile(
    endpoint_count=2,
    state_factory=ZoneState,
    fields=tuple(Field),
    required_fields=REQUIRED_FIELDS,
    requested_dps=REQUIRED_DPS,
    query_dps=frozenset(dp for dp in REQUIRED_DPS if dp != FAULT_DP),
    baseline_dps=frozenset(
        dp
        for zone in DP_MAP
        for field, dp in zip(Field, zone, strict=True)
        if field in REQUIRED_FIELDS
    ),
    decode_report=decode_report,
    parse_command=lambda payload, request_id: parse_command(
        payload, generated_id=request_id
    ),
    prepare_command=prepare_command,
    write_dps=write_dps,
    confirms=Command.matches,
)
