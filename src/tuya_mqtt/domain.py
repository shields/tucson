from dataclasses import dataclass, replace
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_CONFIG = "invalid_config"
    INVALID_COMMAND = "invalid_command"
    COMPATIBILITY = "compatibility"
    UNAVAILABLE = "unavailable"
    QUEUE_FULL = "queue_full"
    EXPIRED = "expired"
    DUPLICATE_ID = "duplicate_id"
    CONNECTION_LOST = "connection_lost"
    CONFIRMATION_TIMEOUT = "confirmation_timeout"
    DEVICE_ERROR = "device_error"


class BridgeError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    SYNCHRONIZING = "synchronizing"
    ONLINE = "online"
    STOPPING = "stopping"


class Outcome(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCONFIRMED = "unconfirmed"


class Field(StrEnum):
    POWER = "power"
    HEAT_LEVEL = "heat_level"
    PREHEAT = "preheat"
    AUTO_OFF_SECONDS = "auto_off_seconds"
    REMAINING_SECONDS = "remaining_seconds"
    PREHEAT_REMAINING_SECONDS = "preheat_remaining_seconds"


type Value = bool | int
type Update = tuple[Field, Value | None]


@dataclass(frozen=True, slots=True)
class ZoneState:
    power: bool | None = None
    heat_level: int | None = None
    preheat: bool | None = None
    auto_off_seconds: int | None = None
    remaining_seconds: int | None = None
    preheat_remaining_seconds: int | None = None

    @property
    def usable(self) -> bool:
        return all(
            value is not None for value in (self.power, self.heat_level, self.preheat)
        )

    def merge(self, updates: tuple[Update, ...]) -> ZoneState:
        return replace(self, **dict(updates))

    def payload(self, session_id: str) -> dict[str, str | Value | None]:
        return {"session_id": session_id} | {
            field.value: getattr(self, field.value) for field in Field
        }


@dataclass(frozen=True, slots=True)
class Command:
    request_id: str
    values: tuple[tuple[Field, Value], ...]

    def matches(self, state: ZoneState) -> bool:
        return all(getattr(state, field.value) == value for field, value in self.values)


@dataclass(frozen=True, slots=True)
class CommandResult:
    request_id: str
    outcome: Outcome
    error_code: ErrorCode | None = None

    def payload(self) -> dict[str, str]:
        result = {"request_id": self.request_id, "outcome": self.outcome.value}
        if self.error_code is not None:
            result["error_code"] = self.error_code.value
        return result
