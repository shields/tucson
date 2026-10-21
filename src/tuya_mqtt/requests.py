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

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
lazy from collections.abc import Callable

from .domain import CommandResult, ErrorCode, Outcome

REQUEST_LIMIT = 1024
RESULT_LIFETIME = 60.0


class Admission(StrEnum):
    NEW = "new"
    PENDING = "pending"
    REPLAY = "replay"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Decision:
    admission: Admission
    result: CommandResult | None = None


@dataclass(slots=True)
class Entry:
    fingerprint: bytes
    result: CommandResult | None = None
    completed_at: float | None = None


class RequestLedger:
    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, Entry] = {}

    def begin(self, request_id: str, fingerprint: bytes) -> Decision:
        now = self._clock()
        self._entries = {
            key: entry
            for key, entry in self._entries.items()
            if entry.completed_at is None or now - entry.completed_at < RESULT_LIFETIME
        }
        if (entry := self._entries.get(request_id)) is not None:
            if entry.fingerprint != fingerprint:
                return Decision(
                    Admission.REJECTED,
                    CommandResult(request_id, Outcome.REJECTED, ErrorCode.DUPLICATE_ID),
                )
            if entry.result is None:
                return Decision(Admission.PENDING)
            return Decision(Admission.REPLAY, entry.result)
        if len(self._entries) >= REQUEST_LIMIT:
            return Decision(
                Admission.REJECTED,
                CommandResult(request_id, Outcome.REJECTED, ErrorCode.QUEUE_FULL),
            )
        self._entries[request_id] = Entry(fingerprint)
        return Decision(Admission.NEW)

    def finish(self, result: CommandResult) -> None:
        entry = self._entries[result.request_id]
        if entry.result is not None:
            message = "request already completed"
            raise RuntimeError(message)
        entry.result = result
        entry.completed_at = self._clock()
