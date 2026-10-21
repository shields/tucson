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

from time import monotonic
from typing import Protocol, cast
lazy from collections.abc import Callable, Mapping, Sequence

import tinytuya

from .domain import BridgeError, ErrorCode, Value
from .profiles import get_profile
lazy from .config import ControllerConfig

REQUEST_TIMEOUT = 2.0
RECEIVE_SLICE = 0.25
CONNECT_RETRY_LIMIT = 1


class RawHeader(Protocol):
    @property
    def cmd(self) -> int: ...
    @property
    def seqno(self) -> int: ...


class RawMessage(RawHeader, Protocol):
    @property
    def retcode(self) -> int | None: ...
    @property
    def crc_good(self) -> bool: ...


class RawDevice(Protocol):
    @property
    def raw_sent(self) -> RawHeader | None: ...
    @property
    def raw_recv(self) -> Sequence[RawMessage]: ...

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
    @property
    def write_acknowledged(self) -> bool: ...
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


def _answers_query(device: RawDevice) -> bool:
    sent = device.raw_sent
    return (
        sent is not None
        and bool(device.raw_recv)
        and device.raw_recv[-1].cmd == sent.cmd
    )


def _is_error(result: object, code: int) -> bool:
    return isinstance(result, dict) and result.get("Err") == str(code)


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
        self._profile = get_profile(config.profile)
        self._key = local_key
        self._port = config.port if port is None else port
        self._request_timeout = request_timeout
        self._receive_slice = receive_slice
        self._clock = clock
        self._factory = factory or cast("Callable[..., RawDevice]", tinytuya.Device)
        self._device: RawDevice | None = None
        self._connected = False
        self._write_header: tuple[int, int] | None = None
        self._write_acknowledged = False

    @property
    def write_acknowledged(self) -> bool:
        return self._write_acknowledged

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
        device.set_socketRetryLimit(CONNECT_RETRY_LIMIT)
        device.set_socketRetryDelay(0)
        device.set_sendWait(0)
        device.set_retry(retry=False)
        device.set_dpsUsed(dict.fromkeys(self._profile.requested_dps))
        try:
            result = self._query(device, connecting=True)
            if result is not None and any(
                dp not in result for dp in self._profile.query_dps
            ):
                # Device22 detection resets TinyTuya's requested DP set to DP 1.
                device.set_dpsUsed(dict.fromkeys(self._profile.requested_dps))
                result = self._query(device, connecting=True)
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

    def _observed(self, device: RawDevice, response: object) -> object:
        # TinyTuya replaces raw_recv on every call, so each call's frames are
        # checked before the next one.
        if self._write_header is not None:
            for frame in device.raw_recv:
                if (frame.cmd, frame.seqno) != self._write_header:
                    continue
                if not frame.crc_good or frame.retcode != 0:
                    raise BridgeError(
                        ErrorCode.DEVICE_ERROR, "device rejected control packet"
                    )
                self._write_acknowledged = True
        return response

    def _query(
        self, device: RawDevice, *, connecting: bool
    ) -> dict[str, object] | None:
        retry_limit = CONNECT_RETRY_LIMIT if connecting else 0
        retries = retry_limit

        def start() -> float:
            return self._clock() + self._request_timeout

        def ask() -> object:
            device.set_socketTimeout(self._request_timeout)
            return self._observed(device, device.status())

        def listen(timeout: float) -> object:
            device.set_socketTimeout(timeout)
            # A retry after a connection reset reconnects and waits on the new
            # socket for a reply to a query that was not sent again.
            device.set_socketRetryLimit(0)
            try:
                return self._observed(device, device.receive())
            finally:
                device.set_socketRetryLimit(retry_limit)

        deadline = None if connecting else start()
        result = ask()
        if deadline is None:
            # Connecting and negotiating run inside the first call, so they do not
            # consume the time allowed for the reply to arrive.
            deadline = start()
        # TinyTuya returns one frame per call, so a push or an empty ACK can come
        # back in place of the reply.
        while True:
            rejected = _is_error(result, tinytuya.ERR_DEVTYPE)
            report = None if rejected else _normalize(result)
            if report is not None and _answers_query(device):
                return report
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            if rejected:
                # Behind a push, receive() reads the rejection and does not ask again.
                result = ask()
            else:
                result = listen(remaining)
                if retries and _is_error(result, tinytuya.ERR_CONNECT):
                    retries -= 1
                    result = ask()
                    deadline = start()

    def _call(
        self, operation: Callable[[RawDevice], dict[str, object] | None]
    ) -> dict[str, object] | None:
        device = self._require_device()
        try:
            return operation(device)
        except OSError, ValueError:
            self._connected = False
            raise BridgeError(
                ErrorCode.CONNECTION_LOST, "device connection lost"
            ) from None
        except BridgeError:
            self._connected = False
            raise

    def status(self) -> dict[str, object]:
        result = self._call(lambda device: self._query(device, connecting=False))
        if result is None:
            self._connected = False
            raise BridgeError(ErrorCode.CONNECTION_LOST, "status report missing")
        return result

    def receive(self) -> dict[str, object] | None:
        def read(device: RawDevice) -> dict[str, object] | None:
            device.set_socketTimeout(self._receive_slice)
            return _normalize(self._observed(device, device.receive()))

        return self._call(read)

    def heartbeat(self) -> None:
        def send(device: RawDevice) -> dict[str, object] | None:
            device.set_socketTimeout(self._request_timeout)
            return _normalize(self._observed(device, device.heartbeat(nowait=True)))

        self._call(send)

    def write(self, dps: dict[str, Value | str]) -> None:
        self._write_header = None
        self._write_acknowledged = False

        def send(device: RawDevice) -> dict[str, object] | None:
            device.set_socketTimeout(self._request_timeout)
            # The synchronous multi-DP helper retries failed writes one DP at a time.
            payload = device.generate_payload(tinytuya.CONTROL, dps)
            return _normalize(device.send(payload))

        self._call(send)
        if self._profile.acknowledge is not None:
            header = self._require_device().raw_sent
            if header is None or header.cmd != tinytuya.CONTROL:
                self._connected = False
                raise BridgeError(
                    ErrorCode.COMPATIBILITY, "control packet identity missing"
                )
            self._write_header = (header.cmd, header.seqno)

    def close(self) -> None:
        self._connected = False
        self._write_header = None
        self._write_acknowledged = False
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
