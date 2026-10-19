from dataclasses import replace
from typing import cast
lazy from collections.abc import Mapping

from tuya_mqtt.domain import (
    BridgeError,
    Command,
    CurtainField,
    CurtainState,
    EndpointState,
    ErrorCode,
    Update,
    Value,
)

from .base import EndpointReport, Profile, WriteMode
from .curtain import parse_command

MAX_POSITION = 100


def decode_report(dps: Mapping[str, object], _endpoint: int) -> EndpointReport:
    if "101" not in dps:
        return EndpointReport((), ())
    raw = dps["101"]
    valid = type(raw) is int and 0 <= raw <= MAX_POSITION
    return EndpointReport(
        ((CurtainField.CURRENT_POSITION, MAX_POSITION - raw if valid else None),),
        () if valid else (CurtainField.CURRENT_POSITION,),
    )


def merge_report(state: EndpointState, updates: tuple[Update, ...]) -> CurtainState:
    previous = cast("CurtainState", state)
    current = previous.merge(updates)
    if (
        current.current_position != previous.current_position
        or current.target_position is None
    ):
        return replace(current, target_position=current.current_position)
    return current


def write_dps(
    command: Command, _endpoint: int, mode: WriteMode
) -> dict[str, Value | str]:
    if mode == "disabled":
        raise BridgeError(ErrorCode.WRITE_DISABLED, "controller writes are disabled")
    _field, value = command.values[0]
    return {"101": MAX_POSITION - value}


PROFILE = Profile(
    endpoint_count=1,
    state_factory=CurtainState,
    fields=tuple(CurtainField),
    required_fields=frozenset(
        (CurtainField.CURRENT_POSITION, CurtainField.TARGET_POSITION)
    ),
    requested_dps=("101", "102", "103"),
    query_dps=frozenset(("101",)),
    baseline_dps=frozenset(("101",)),
    decode_report=decode_report,
    parse_command=parse_command,
    prepare_command=lambda command, _state: command,
    write_dps=write_dps,
    confirms=Command.matches,
    protocols=frozenset(("3.3",)),
    merge_report=merge_report,
    acknowledge=lambda command: command.values,
)
