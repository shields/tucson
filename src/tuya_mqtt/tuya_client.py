from time import monotonic
from typing import Protocol, cast
lazy from collections.abc import Callable, Mapping

import tinytuya

from .domain import BridgeError, ErrorCode, Value
from .profiles.sunbeam import REQUIRED_DPS
lazy from .config import ControllerConfig

REQUEST_TIMEOUT = 2.0
RECEIVE_SLICE = 0.25


class RawDevice(Protocol):
    def set_socketPersistent(self, *, persist: bool) -> None: ...
    def set_socketRetryLimit(self, limit: int) -> None: ...
    def set_socketRetryDelay(self, delay: float) -> None: ...
    def set_socketTimeout(self, seconds: float) -> None: ...
    def set_retry(self, *, retry: bool) -> None: ...
    def set_sendWait(self, seconds: float) -> None: ...
    def set_dpsUsed(self, dps: dict[str, None]) -> None: ...
    def status(self) -> object: ...
    def receive(self) -> object: ...
    def heartbeat(self, *, nowait: bool) -> object: ...
    def generate_payload(
        self, command: int, data: dict[str, Value | str]
    ) -> object: ...
    def send(self, payload: object) -> object: ...
    def close(self) -> None: ...


class DeviceClient(Protocol):
    def connect(self) -> dict[str, object]: ...
    def status(self) -> dict[str, object]: ...
    def receive(self) -> dict[str, object] | None: ...
    def heartbeat(self) -> None: ...
    def write(self, dps: dict[str, Value | str]) -> None: ...
    def close(self) -> None: ...


def _normalize(result: object) -> dict[str, object] | None:
    if result is None:
        return None
    if not isinstance(result, dict):
        raise BridgeError(ErrorCode.COMPATIBILITY, "invalid device response")
    if "Err" in result or "Error" in result:
        raise BridgeError(ErrorCode.DEVICE_ERROR, "device operation failed")
    if "dps" not in result:
        return None
    dps = result["dps"]
    if not isinstance(dps, dict) or any(
        not isinstance(key, str) or not key.isdecimal() for key in dps
    ):
        raise BridgeError(ErrorCode.COMPATIBILITY, "invalid device DP report")
    return dict(dps)


class TinyTuyaClient:
    def __init__(
        self,
        config: ControllerConfig,
        local_key: str,
        *,
        port: int | None = None,
        request_timeout: float = REQUEST_TIMEOUT,
        receive_slice: float = RECEIVE_SLICE,
        factory: Callable[..., RawDevice] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._config = config
        self._key = local_key
        self._port = config.port if port is None else port
        self._request_timeout = request_timeout
        self._receive_slice = receive_slice
        self._clock = clock
        self._factory = factory or cast("Callable[..., RawDevice]", tinytuya.Device)
        self._device: RawDevice | None = None
        self._connected = False

    def _require_device(self) -> RawDevice:
        if self._device is None or not self._connected:
            raise BridgeError(ErrorCode.UNAVAILABLE, "device is not synchronized")
        return self._device

    def connect(self) -> dict[str, object]:
        self.close()
        device = self._factory(
            self._config.device_id,
            self._config.ip,
            self._key,
            version=float(self._config.protocol_version),
            port=self._port,
        )
        self._device = device
        device.set_socketPersistent(persist=True)
        device.set_socketRetryLimit(1)
        device.set_socketRetryDelay(0)
        device.set_socketTimeout(self._request_timeout)
        device.set_sendWait(0)
        device.set_retry(retry=False)
        device.set_dpsUsed(dict.fromkeys(REQUIRED_DPS))
        try:
            result = _normalize(device.status())
            if result is not None and any(dp not in result for dp in REQUIRED_DPS):
                # Device22 detection resets TinyTuya's requested DP set to DP 1.
                device.set_dpsUsed(dict.fromkeys(REQUIRED_DPS))
                result = _normalize(device.status())
        except OSError, ValueError, BridgeError:
            self.close()
            raise BridgeError(
                ErrorCode.CONNECTION_LOST, "device synchronization failed"
            ) from None
        if result is None:
            self.close()
            raise BridgeError(ErrorCode.CONNECTION_LOST, "status report missing")
        # A zero retry limit prevents reopening a lost socket and replaying a write.
        device.set_socketRetryLimit(0)
        self._connected = True
        return result

    def _call(
        self, operation: Callable[[RawDevice], object]
    ) -> dict[str, object] | None:
        device = self._require_device()
        try:
            return _normalize(operation(device))
        except OSError, ValueError:
            self._connected = False
            raise BridgeError(
                ErrorCode.CONNECTION_LOST, "device connection lost"
            ) from None
        except BridgeError:
            self._connected = False
            raise

    def status(self) -> dict[str, object]:
        deadline = self._clock() + self._request_timeout

        def query(device: RawDevice) -> object:
            device.set_socketTimeout(self._request_timeout)
            return device.status()

        result = self._call(query)
        # Empty ACKs may precede the status report on a persistent connection.
        while result is None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break

            def read(device: RawDevice, timeout: float = remaining) -> object:
                device.set_socketTimeout(min(timeout, self._receive_slice))
                return device.receive()

            result = self._call(read)
        if result is None:
            self._connected = False
            raise BridgeError(ErrorCode.CONNECTION_LOST, "status report missing")
        return result

    def receive(self) -> dict[str, object] | None:
        def read(device: RawDevice) -> object:
            device.set_socketTimeout(self._receive_slice)
            return device.receive()

        return self._call(read)

    def heartbeat(self) -> None:
        def send(device: RawDevice) -> object:
            device.set_socketTimeout(self._request_timeout)
            return device.heartbeat(nowait=True)

        self._call(send)

    def write(self, dps: dict[str, Value | str]) -> None:
        def send(device: RawDevice) -> object:
            device.set_socketTimeout(self._request_timeout)
            # The synchronous multi-DP helper retries failed writes one DP at a time.
            payload = device.generate_payload(tinytuya.CONTROL, dps)
            return device.send(payload)

        self._call(send)

    def close(self) -> None:
        self._connected = False
        if self._device is not None:
            self._device.close()
            self._device = None


def sanitized_dps(
    dps: Mapping[str, object], secrets: tuple[str, ...]
) -> dict[str, object]:
    def sanitize(value: object) -> object:
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {sanitize(key): sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return {key: sanitize(value) for key, value in dps.items()}
