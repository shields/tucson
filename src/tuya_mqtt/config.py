import os
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal, Self, cast
lazy from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .domain import BridgeError, ErrorCode
from .json_codec import InvalidJSONError, decode
from .profiles import get_profile
lazy from .profiles.base import WriteMode

LOCAL_KEY_BYTES = 16

type Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
type Text = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00]+$")]
type Port = Annotated[int, Field(ge=1, le=65535)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SecretReference(StrictModel):
    env: Text | None = None
    file: Text | None = None

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if (self.env is None) == (self.file is None):
            message = "exactly one secret source is required"
            raise ValueError(message)
        return self

    def resolve(self, env: Mapping[str, str]) -> str:
        try:
            value = (
                env[self.env]
                if self.env is not None
                else Path(cast("str", self.file))
                .read_text(encoding="utf-8")
                .removesuffix("\n")
            )
        except KeyError, OSError, UnicodeError:
            raise BridgeError(
                ErrorCode.INVALID_CONFIG, "cannot resolve secret"
            ) from None
        if not value or "\x00" in value:
            raise BridgeError(ErrorCode.INVALID_CONFIG, "invalid secret")
        return value


class TLSConfig(StrictModel):
    ca_file: Text | None = None
    cert_file: Text | None = None
    key_file: Text | None = None

    @model_validator(mode="after")
    def check_pair(self) -> Self:
        if (self.cert_file is None) != (self.key_file is None):
            message = "TLS certificate and key must be supplied together"
            raise ValueError(message)
        return self


class MQTTConfig(StrictModel):
    host: Text
    port: Port = 1883
    client_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")]
    topic_prefix: Annotated[
        str, Field(pattern=r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$", max_length=256)
    ] = "tuya"
    username: Text | None = None
    password: SecretReference | None = None
    tls: TLSConfig | None = None


class HTTPConfig(StrictModel):
    bind: Text = "127.0.0.1"
    port: Port = 8080

    @model_validator(mode="after")
    def check_address(self) -> Self:
        ip_address(self.bind)
        return self


class ControllerConfig(StrictModel):
    name: Name
    device_id: Text
    ip: Text
    port: Port = 6668
    protocol_version: Literal["3.3", "3.4", "3.5"]
    local_key: SecretReference
    profile: Text
    zones: tuple[Name, ...] | None = None
    write_mode: WriteMode = "disabled"

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        profile = get_profile(self.profile)
        if self.protocol_version not in profile.protocols:
            message = "unsupported protocol for profile"
            raise ValueError(message)
        if profile.endpoint_count > 1:
            if self.zones is None or len(self.zones) != profile.endpoint_count:
                message = "incorrect zone count for profile"
                raise ValueError(message)
        elif "zones" in self.model_fields_set:
            message = "zones apply only to profiles with more than one endpoint"
            raise ValueError(message)
        address = ip_address(self.ip)
        if address.is_unspecified or address.is_multicast:
            message = "controller must have a unicast address"
            raise ValueError(message)
        zones = self.zones or ()
        if (
            self.name == "bridge"
            or (profile.endpoint_count == 1 and self.name == "availability")
            or len(set(zones)) != len(zones)
            or "availability" in zones
        ):
            message = "duplicate or reserved topic path"
            raise ValueError(message)
        return self

    def zone_name(self, endpoint: int) -> str | None:
        return None if self.zones is None else self.zones[endpoint]


class Config(StrictModel):
    mqtt: MQTTConfig
    http: HTTPConfig = HTTPConfig()
    controllers: Annotated[tuple[ControllerConfig, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def check_uniqueness(self) -> Self:
        for values in (
            [controller.name for controller in self.controllers],
            [controller.device_id for controller in self.controllers],
            [
                (str(ip_address(controller.ip)), controller.port)
                for controller in self.controllers
            ],
        ):
            if len(values) != len(set(values)):
                message = "duplicate controller identity"
                raise ValueError(message)
        return self


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    config: Config
    local_keys: tuple[str, ...] = field(repr=False)
    mqtt_password: str | None = field(repr=False)


def load_config(path: Path, *, env: Mapping[str, str] | None = None) -> ResolvedConfig:
    try:
        data = path.read_text(encoding="utf-8")
        decode(data)
        config = Config.model_validate_json(data)
    except OSError, UnicodeError, InvalidJSONError, ValidationError:
        raise BridgeError(ErrorCode.INVALID_CONFIG, "invalid configuration") from None
    environment = os.environ if env is None else env
    keys = tuple(
        controller.local_key.resolve(environment) for controller in config.controllers
    )
    if any(not key.isascii() or len(key) != LOCAL_KEY_BYTES for key in keys):
        raise BridgeError(ErrorCode.INVALID_CONFIG, "local key must be 16 ASCII bytes")
    password = (
        config.mqtt.password.resolve(environment)
        if config.mqtt.password is not None
        else None
    )
    return ResolvedConfig(config, keys, password)
