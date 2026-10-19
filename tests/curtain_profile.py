from typing import cast
lazy from collections.abc import Mapping

from tuya_mqtt.domain import (
    BridgeError,
    Command,
    CurtainField,
    CurtainState,
    ErrorCode,
    Update,
    Value,
)
from tuya_mqtt.profiles.base import Profile, WriteMode, ZoneReport
from tuya_mqtt.profiles.curtain import parse_command

# Synthetic DP numbers keep this fixture from implying a verified hardware mapping.
DP_MAP = {
    CurtainField.CURRENT_POSITION: "201",
    CurtainField.TARGET_POSITION: "202",
    CurtainField.MOTION: "203",
}
BASELINE: dict[str, object] = {"201": 100, "202": 100, "203": "stopped"}


def decode_report(dps: Mapping[str, object], _zone: int) -> ZoneReport:
    updates: list[Update] = []
    invalid: list[CurtainField] = []
    for field, dp in DP_MAP.items():
        if dp not in dps:
            continue
        raw = dps[dp]
        value: Value | str | None = None
        if field is CurtainField.MOTION:
            if raw in ("opening", "closing", "stopped"):
                value = cast("str", raw)
        elif type(raw) is int and 0 <= raw <= 100:
            value = 100 - raw
        updates.append((field, value))
        if value is None:
            invalid.append(field)
    return ZoneReport(tuple(updates), tuple(invalid))


def write_dps(command: Command, _zone: int, mode: WriteMode) -> dict[str, Value | str]:
    if mode == "disabled":
        raise BridgeError(ErrorCode.WRITE_DISABLED, "controller writes are disabled")
    _field, value = command.values[0]
    return {"202": 100 - cast("int", value)}


PROFILE = Profile(
    endpoint_count=1,
    state_factory=CurtainState,
    fields=tuple(DP_MAP),
    required_fields=frozenset(
        (CurtainField.CURRENT_POSITION, CurtainField.TARGET_POSITION)
    ),
    requested_dps=tuple(DP_MAP.values()),
    query_dps=frozenset(("201", "202")),
    baseline_dps=frozenset(("201", "202")),
    decode_report=decode_report,
    parse_command=parse_command,
    prepare_command=lambda command, _state: command,
    write_dps=write_dps,
    confirms=Command.matches,
)
