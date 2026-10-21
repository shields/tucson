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
from dataclasses import dataclass, replace
from random import random
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol, cast
lazy from collections.abc import Callable

import structlog

from .domain import (
    BridgeError,
    Command,
    CommandResult,
    Completion,
    ConnectionState,
    ControllerSnapshot,
    EndpointState,
    ErrorCode,
    Outcome,
    Request,
    Value,
)
from .profiles import get_profile
lazy from .config import ControllerConfig
lazy from .tuya_client import DeviceClient

QUEUE_LIMIT = 16
OUTSTANDING_LIMIT = QUEUE_LIMIT + 1
START_TIMEOUT = 2.0
CONFIRMATION_TIMEOUT = 8.0
HEARTBEAT_INTERVAL = 10.0
POLL_INTERVAL = 30.0
STALE_TIMEOUT = 90.0
SHUTDOWN_TIMEOUT = 10.0
MAX_BACKOFF = 60.0
IDLE_SLICE = 0.25
CONFIRMATION_POLL_INTERVAL = 1.0


class ControllerLog(Protocol):
    def info(self, event: str, **fields: object) -> None: ...
    def error(self, event: str, **fields: object) -> None: ...


class WorkerFailureError(RuntimeError):
    pass


class Mailbox:
    def __init__(self) -> None:
        self.lock = Lock()
        self.wake = Event()
        self.stop = Event()
        self._session_id: str | None = None
        self._refresh = False
        self._view = ControllerSnapshot()
        self._queued: deque[Request] = deque()
        self._completed: deque[Completion] = deque()
        self._outstanding = 0

    @property
    def snapshot(self) -> ControllerSnapshot:
        with self.lock:
            return self._view

    @property
    def queue_depth(self) -> int:
        with self.lock:
            return len(self._queued)

    @property
    def session_id(self) -> str | None:
        with self.lock:
            return self._session_id

    def set_session(self, session_id: str | None) -> None:
        with self.lock:
            self._session_id = session_id
        self.wake.set()

    def refresh(self) -> None:
        with self.lock:
            self._refresh = True
        self.wake.set()

    def take_refresh(self) -> bool:
        with self.lock:
            requested = self._refresh
            self._refresh = False
            return requested

    def publish(self, view: ControllerSnapshot) -> None:
        with self.lock:
            self._view = view

    def admit(self, request: Request, *, writes_enabled: bool) -> ErrorCode | None:
        with self.lock:
            if (
                self.stop.is_set()
                or not self._view.online
                or request.session_id != self._session_id
                or request.session_id != self._view.session_id
            ):
                return ErrorCode.UNAVAILABLE
            if not writes_enabled:
                return ErrorCode.WRITE_DISABLED
            if (
                len(self._queued) >= QUEUE_LIMIT
                or self._outstanding >= OUTSTANDING_LIMIT
            ):
                return ErrorCode.QUEUE_FULL
            self._queued.append(request)
            self._outstanding += 1
        self.wake.set()
        return None

    def take_request(self) -> Request | None:
        with self.lock:
            return self._queued.popleft() if self._queued else None

    def complete(self, completion: Completion) -> None:
        with self.lock:
            self._completed.append(completion)

    def drain_results(self) -> tuple[Completion, ...]:
        with self.lock:
            results = tuple(self._completed)
            self._completed.clear()
            self._outstanding -= len(results)
            return results


@dataclass(slots=True)
class Pending:
    request: Request
    command: Command
    dps: dict[str, Value | str]
    confirmation: EndpointState
    written: bool = False
    acknowledged: bool = False
    next_query: float = 0


class ControllerCore:
    def __init__(
        self,
        config: ControllerConfig,
        factory: Callable[[], DeviceClient],
        mailbox: Mailbox,
        *,
        clock: Callable[[], float] = monotonic,
        jitter: Callable[[], float] = random,
        logger: ControllerLog | None = None,
    ) -> None:
        self.config = config
        self.mailbox = mailbox
        self.profile = get_profile(config.profile)
        self.view = ControllerSnapshot(
            progress_at=clock(),
            endpoints=self.profile.initial_states(),
            previous_endpoints=self.profile.initial_states(),
            invalid_fields=tuple(
                frozenset() for _ in range(self.profile.endpoint_count)
            ),
        )
        self._factory = factory
        self._clock = clock
        self._jitter = jitter
        self._log = logger or cast(
            "ControllerLog", structlog.get_logger().bind(controller=config.name)
        )
        self._device: DeviceClient | None = None
        self._pending: Pending | None = None
        self._backoff = 1.0
        self._retry_at = 0.0
        self._next_poll = 0.0
        self._next_heartbeat = 0.0
        self._baseline_started = clock()
        self._need_receive = False
        # The view is blanked on every reconnect and session change, so change
        # detection keeps its own last valid fault code.
        self._fault: int | None = None
        self.mailbox.publish(self.view)

    def _publish(self, **changes: object) -> None:
        self.view = replace(self.view, **changes)
        self.mailbox.publish(self.view)

    def _finish(
        self, request: Request, outcome: Outcome, code: ErrorCode | None = None
    ) -> None:
        completion = Completion(
            request,
            CommandResult(request.command.request_id, outcome, code),
            self._clock(),
        )
        self.mailbox.complete(completion)
        self._publish(last_completion=completion, active=None)
        if outcome is Outcome.UNCONFIRMED and self.view.online:
            self.mailbox.refresh()
        log = self._log.info if outcome is Outcome.CONFIRMED else self._log.error
        log(
            "command_result",
            zone=self.config.zone_name(request.endpoint),
            operation="command",
            request_id=request.command.request_id,
            outcome=outcome.value,
            error_code=code,
            duration=completion.completed_at - request.received_at,
        )

    def _cancel_pending(self, code: ErrorCode) -> None:
        if self._pending is not None:
            pending = self._pending
            self._pending = None
            self._finish(
                pending.request,
                Outcome.UNCONFIRMED if pending.written else Outcome.REJECTED,
                code,
            )
        while (request := self.mailbox.take_request()) is not None:
            self._finish(request, Outcome.REJECTED, code)

    def _close(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            device.close()

    def _disconnect(self, code: ErrorCode, operation: str) -> None:
        self._publish(
            connection=ConnectionState.DISCONNECTED,
            synchronized_at=None,
            last_error=code,
            operation_failures=self.view.operation_failures + 1,
        )
        self._cancel_pending(code)
        self._close()
        delay = min(MAX_BACKOFF, self._backoff * (1 + 0.25 * self._jitter()))
        self._retry_at = self._clock() + delay
        self._backoff = min(MAX_BACKOFF, self._backoff * 2)
        self._log.error(
            "controller_disconnected",
            operation=operation,
            error_code=code.value,
            generation=self.view.generation,
            retry_delay=delay,
        )

    def _reset_baseline(self, session_id: str | None, *, new_connection: bool) -> None:
        self._baseline_started = self._clock()
        current_known = any(
            getattr(state, field.value) is not None
            for state in self.view.endpoints
            for field in self.profile.fields
        )
        previous = (
            self.view.endpoints if current_known else self.view.previous_endpoints
        )
        connection = (
            ConnectionState.CONNECTING
            if new_connection
            else ConnectionState.SYNCHRONIZING
            if self._device is not None
            else ConnectionState.DISCONNECTED
        )
        self._publish(
            connection=connection,
            generation=self.view.generation + int(new_connection),
            reconnects=self.view.reconnects
            + int(new_connection and self.view.generation > 0),
            session_id=session_id,
            previous_endpoints=previous,
            previous_full_status_at=(
                self.view.last_full_status
                if current_known
                else self.view.previous_full_status_at
            ),
            endpoints=self.profile.initial_states(),
            invalid_fields=tuple(
                frozenset() for _ in range(self.profile.endpoint_count)
            ),
            synchronized_at=None,
            last_full_status=None,
            last_traffic=None,
        )

    def _log_fault(self, fault: int | None) -> None:
        if fault is None or fault == self._fault:
            return
        previous, self._fault = self._fault, fault
        if fault:
            event, log = "controller_fault", self._log.error
        elif previous:
            event, log = "controller_fault_cleared", self._log.info
        else:
            return
        log(
            event,
            operation="report",
            fault_code=fault,
            previous_fault_code=previous,
            generation=self.view.generation,
        )

    def _report(self, dps: dict[str, object]) -> None:
        now = self._clock()
        reports = tuple(
            self.profile.decode_report(dps, endpoint)
            for endpoint in range(self.profile.endpoint_count)
        )
        endpoints = tuple(
            self.profile.merge_report(self.view.endpoints[index], report.updates)
            for index, report in enumerate(reports)
        )
        invalid = tuple(
            (
                self.view.invalid_fields[index]
                - {field for field, _value in report.updates}
            )
            | set(report.invalid_fields)
            for index, report in enumerate(reports)
        )
        error = ErrorCode.COMPATIBILITY if any(invalid) else None
        self._log_fault(endpoints[0].fault_code)
        self._publish(
            endpoints=endpoints,
            invalid_fields=tuple(frozenset(fields) for fields in invalid),
            last_traffic=now,
            last_error=error
            if self.view.online or error is not None
            else self.view.last_error,
        )
        if self._pending is not None and self._pending.written:
            report = reports[self._pending.request.endpoint]
            self._pending.confirmation = self._pending.confirmation.merge(
                report.updates
            )
        if any(self.profile.required_fields & fields for fields in invalid):
            raise BridgeError(ErrorCode.COMPATIBILITY, "invalid required DP")
        if dps.keys() >= self.profile.baseline_dps and all(
            state.usable for state in endpoints
        ):
            self._publish(last_full_status=now)
        if all(state.usable for state in endpoints) and not self.view.online:
            self._publish(
                connection=ConnectionState.ONLINE, synchronized_at=now, last_error=error
            )
            self._backoff = 1.0
            self._log.info(
                "controller_online",
                generation=self.view.generation,
                operation="synchronize",
            )

    def _guard(self, request: Request, *, started: bool) -> ErrorCode | None:
        now = self._clock()
        if (
            self.mailbox.stop.is_set()
            or not self.view.online
            or request.generation != self.view.generation
            or request.session_id != self.view.session_id
            or request.session_id != self.mailbox.session_id
        ):
            return ErrorCode.UNAVAILABLE
        if now >= request.deadline:
            return ErrorCode.CONFIRMATION_TIMEOUT if started else ErrorCode.EXPIRED
        if not started and now >= request.start_by:
            return ErrorCode.EXPIRED
        if any(
            field in self.view.invalid_fields[request.endpoint]
            for field, _value in request.command.values
        ):
            return ErrorCode.COMPATIBILITY
        return None

    def _acknowledge(self, device: DeviceClient) -> None:
        pending = self._pending
        acknowledge = self.profile.acknowledge
        if (
            pending is None
            or not pending.written
            or pending.acknowledged
            or acknowledge is None
            or not device.write_acknowledged
            or self._guard(pending.request, started=True) is not None
        ):
            return
        updates = acknowledge(pending.command)
        pending.acknowledged = True
        pending.confirmation = pending.confirmation.merge(updates)
        endpoints = list(self.view.endpoints)
        endpoint = pending.request.endpoint
        endpoints[endpoint] = endpoints[endpoint].merge(updates)
        self._publish(endpoints=tuple(endpoints))

    def _command_step(self, device: DeviceClient) -> None:
        pending = self._pending
        if pending is None:
            request = self.mailbox.take_request()
            if request is None:
                report = device.receive()
                if report is not None:
                    self._report(report)
                return
            error = self._guard(request, started=False)
            if error is not None:
                self._finish(request, Outcome.REJECTED, error)
                return
            try:
                command = self.profile.prepare_command(
                    request.command, self.view.endpoints[request.endpoint]
                )
                dps = self.profile.write_dps(
                    command, request.endpoint, self.config.write_mode
                )
            except BridgeError as error:
                self._finish(request, Outcome.REJECTED, error.code)
                return
            pending = Pending(request, command, dps, self.profile.state_factory())
            self._pending = pending
            self._publish(active=request)
        error = self._guard(pending.request, started=pending.written)
        if error is not None:
            self._pending = None
            self._finish(
                pending.request,
                Outcome.UNCONFIRMED if pending.written else Outcome.REJECTED,
                error,
            )
            return
        if not pending.written:
            self._log.info(
                "command_write",
                zone=self.config.zone_name(pending.request.endpoint),
                operation="command",
                request_id=pending.request.command.request_id,
                dps=pending.dps,
            )
            pending.written = True
            device.write(pending.dps)
            self._need_receive = True
            return
        if self.profile.confirms(pending.command, pending.confirmation):
            self._pending = None
            self._finish(pending.request, Outcome.CONFIRMED)
            return
        now = self._clock()
        if now >= pending.next_query:
            report = device.status()
            pending.next_query = self._clock() + CONFIRMATION_POLL_INTERVAL
            self._report(report)
        else:
            report = device.receive()
            if report is not None:
                self._report(report)
        if self._clock() >= pending.request.deadline:
            self._pending = None
            self._finish(
                pending.request, Outcome.UNCONFIRMED, ErrorCode.CONFIRMATION_TIMEOUT
            )
        elif self.profile.confirms(pending.command, pending.confirmation):
            self._pending = None
            self._finish(pending.request, Outcome.CONFIRMED)

    def tick(self) -> None:
        if self.mailbox.stop.is_set():
            self.stop()
            return
        self._publish(progress_at=self._clock())
        session_id = self.mailbox.session_id
        if session_id != self.view.session_id:
            self._cancel_pending(ErrorCode.UNAVAILABLE)
            if session_id is not None and self._device is not None:
                self._close()
                self._retry_at = self._clock()
            self._reset_baseline(session_id, new_connection=False)
            self.mailbox.refresh()
        now = self._clock()
        if self._device is None:
            if now < self._retry_at:
                return
            self._reset_baseline(session_id, new_connection=True)
            self._device = self._factory()
            try:
                report = self._device.connect()
                self._publish(connection=ConnectionState.SYNCHRONIZING)
                self._report(report)
            except BridgeError as error:
                self._disconnect(error.code, "connect")
                return
            self._next_poll = self._clock() + POLL_INTERVAL
            self._next_heartbeat = self._clock() + HEARTBEAT_INTERVAL
            self._need_receive = True
            return
        device = self._device
        fresh_at = self.view.last_full_status
        if fresh_at is None:
            fresh_at = self.view.synchronized_at
        if fresh_at is None:
            fresh_at = self._baseline_started
        if now - fresh_at >= STALE_TIMEOUT:
            self._disconnect(ErrorCode.STALE, "freshness")
            return
        operation = "receive"
        try:
            if now >= self._next_heartbeat:
                operation = "heartbeat"
                device.heartbeat()
                self._next_heartbeat = self._clock() + HEARTBEAT_INTERVAL
            elif (refresh := self.mailbox.take_refresh()) or now >= self._next_poll:
                operation = "status"
                report = device.status()
                self._report(report)
                self._next_poll = self._clock() + POLL_INTERVAL
                if refresh:
                    self._publish(refresh_revision=self.view.refresh_revision + 1)
            elif self._need_receive:
                self._need_receive = False
                report = device.receive()
                if report is not None:
                    self._report(report)
            else:
                operation = "command"
                self._command_step(device)
            self._acknowledge(device)
        except BridgeError as error:
            self._disconnect(error.code, operation)

    def delay(self) -> float:
        return (
            min(IDLE_SLICE, max(0, self._retry_at - self._clock()))
            if self._device is None
            else 0
        )

    def stop(self) -> None:
        self._publish(connection=ConnectionState.STOPPING)
        self._cancel_pending(ErrorCode.UNAVAILABLE)
        self._close()


class ControllerWorker:
    def __init__(
        self,
        config: ControllerConfig,
        factory: Callable[[], DeviceClient],
        *,
        clock: Callable[[], float] = monotonic,
        jitter: Callable[[], float] = random,
        logger: ControllerLog | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._mailbox = Mailbox()
        self._core = ControllerCore(
            config, factory, self._mailbox, clock=clock, jitter=jitter, logger=logger
        )
        self._thread: Thread | None = None
        self._failure: BaseException | None = None

    @property
    def snapshot(self) -> ControllerSnapshot:
        return self._mailbox.snapshot

    @property
    def queue_depth(self) -> int:
        return self._mailbox.queue_depth

    def set_session(self, session_id: str | None) -> None:
        self._mailbox.set_session(session_id)

    def refresh(self) -> None:
        self._mailbox.refresh()

    def submit(
        self,
        endpoint: int,
        command: Command,
        session_id: str,
        *,
        received_at: float | None = None,
    ) -> CommandResult | None:
        now = self._clock() if received_at is None else received_at
        request = Request(
            endpoint,
            command,
            session_id,
            self.snapshot.generation,
            now,
            now + START_TIMEOUT,
            now + CONFIRMATION_TIMEOUT,
        )
        error = self._mailbox.admit(
            request, writes_enabled=self._config.write_mode != "disabled"
        )
        return (
            CommandResult(command.request_id, Outcome.REJECTED, error)
            if error is not None
            else None
        )

    def drain_results(self) -> tuple[Completion, ...]:
        return self._mailbox.drain_results()

    def start(self) -> None:
        if self._thread is not None:
            message = "controller worker already started"
            raise WorkerFailureError(message)
        # A failed shutdown bound must not trap the process waiting for a thread.
        self._thread = Thread(
            target=self._run, name=f"tuya-{self._config.name}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._mailbox.stop.is_set():
                self._core.tick()
                self._mailbox.wake.wait(self._core.delay())
                self._mailbox.wake.clear()
        # Thread failures must cross the boundary to the coordinating loop.
        except BaseException as error:  # noqa: BLE001
            self._failure = error
        finally:
            try:
                self._core.stop()
            except BaseException as error:  # noqa: BLE001
                self._failure = error

    def stop(self) -> None:
        self._mailbox.stop.set()
        self._mailbox.wake.set()

    def check(self) -> None:
        if self._failure is not None:
            message = "controller worker failed"
            raise WorkerFailureError(message) from None

    def join(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                message = "controller worker exceeded shutdown bound"
                raise WorkerFailureError(message)
        self.check()
