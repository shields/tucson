from dataclasses import dataclass
from typing import Literal
lazy from collections.abc import Callable, Mapping

lazy from tuya_mqtt.domain import Command, EndpointState, StateField, Update, Value

MAX_PAYLOAD_BYTES = 4096
MAX_REQUEST_ID = 128
MIN_PRINTABLE = 32

type WriteMode = Literal["disabled", "multi_dp"]


@dataclass(frozen=True, slots=True)
class EndpointReport:
    updates: tuple[Update, ...]
    invalid_fields: tuple[StateField, ...]


@dataclass(frozen=True, slots=True)
class Profile:
    endpoint_count: int
    state_factory: Callable[[], EndpointState]
    fields: tuple[StateField, ...]
    required_fields: frozenset[StateField]
    requested_dps: tuple[str, ...]
    query_dps: frozenset[str]
    baseline_dps: frozenset[str]
    decode_report: Callable[[Mapping[str, object], int], EndpointReport]
    parse_command: Callable[[bytes, str], Command]
    prepare_command: Callable[[Command, EndpointState], Command]
    write_dps: Callable[[Command, int, WriteMode], dict[str, Value | str]]
    confirms: Callable[[Command, EndpointState], bool]
    protocols: frozenset[str] = frozenset(("3.3", "3.4", "3.5"))
    merge_report: Callable[[EndpointState, tuple[Update, ...]], EndpointState] = (
        lambda state, updates: state.merge(updates)
    )
    acknowledge: Callable[[Command], tuple[Update, ...]] | None = None

    def initial_states(self) -> tuple[EndpointState, ...]:
        return tuple(self.state_factory() for _ in range(self.endpoint_count))
