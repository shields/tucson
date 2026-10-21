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

from collections import deque
from dataclasses import dataclass, field
from threading import get_ident
lazy from collections.abc import Callable

from tuya_mqtt.profiles.sunbeam import DP_MAP
lazy from tuya_mqtt.domain import Value

ZONE_VALUES: list[object] = [False, "level_4", False, "1h", 3600, 0, 0]
BASELINE: dict[str, object] = {
    dp: value for zone in DP_MAP for dp, value in zip(zone, ZONE_VALUES, strict=True)
}


@dataclass
class Clock:
    now: float = 0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Device:
    clock: Clock
    state: dict[str, object] = field(default_factory=BASELINE.copy)
    connections: deque[dict[str, object] | Exception] = field(default_factory=deque)
    statuses: deque[dict[str, object] | Exception] = field(default_factory=deque)
    pushes: deque[dict[str, object] | Exception | None] = field(default_factory=deque)
    calls: list[tuple[str, object]] = field(default_factory=list)
    owner_ids: set[int] = field(default_factory=set)
    writes: list[dict[str, Value | str]] = field(default_factory=list)
    apply_writes: bool = True
    write_acknowledged: bool = False
    echo_writes: bool = False
    write_error: Exception | None = None
    heartbeat_error: Exception | None = None
    close_error: Exception | None = None
    on_write: Callable[[], None] | None = None
    receive_hook: Callable[[], None] | None = None
    status_duration: float = 0

    def record(self, operation: str, payload: object = None) -> None:
        self.owner_ids.add(get_ident())
        self.calls.append((operation, payload))

    def connect(self) -> dict[str, object]:
        self.record("connect")
        result = self.connections.popleft() if self.connections else self.state.copy()
        if isinstance(result, Exception):
            raise result
        return result

    def status(self) -> dict[str, object]:
        self.record("status")
        self.clock.advance(self.status_duration)
        result = self.statuses.popleft() if self.statuses else self.state.copy()
        if isinstance(result, Exception):
            raise result
        return result

    def receive(self) -> dict[str, object] | None:
        self.record("receive")
        if self.receive_hook is not None:
            self.receive_hook()
        self.clock.advance(0.25)
        result = self.pushes.popleft() if self.pushes else None
        if isinstance(result, Exception):
            raise result
        if result is not None:
            self.state.update(result)
        return result

    def heartbeat(self) -> None:
        self.record("heartbeat")
        if self.heartbeat_error is not None:
            raise self.heartbeat_error

    def write(self, dps: dict[str, Value | str]) -> None:
        self.write_acknowledged = False
        self.record("write", dps.copy())
        self.writes.append(dps.copy())
        if self.apply_writes:
            self.state.update(dps)
        if self.echo_writes:
            self.pushes.append(dps.copy())
        if self.on_write is not None:
            self.on_write()
        if self.write_error is not None:
            raise self.write_error

    def close(self) -> None:
        self.record("close")
        if self.close_error is not None:
            raise self.close_error


@dataclass
class Log:
    events: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))
