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

import asyncio
import json
from hashlib import sha256
from random import random
from time import monotonic
from typing import Protocol, cast
from uuid import uuid4
lazy from collections.abc import Awaitable, Callable, Mapping

import structlog

from .controller import STALE_TIMEOUT, START_TIMEOUT
from .domain import (
    BridgeError,
    Command,
    CommandResult,
    Completion,
    ControllerSnapshot,
    ErrorCode,
    Outcome,
)
from .json_codec import InvalidJSONError, decode
from .mqtt_client import MQTTFailureError, Operation
from .profiles import get_profile
from .profiles.base import (
    MAX_PAYLOAD_BYTES,
    MAX_REQUEST_ID,
    MIN_PRINTABLE,
)
from .requests import Admission, RequestLedger
lazy from .config import ResolvedConfig
lazy from .controller import ControllerLog
lazy from .mqtt_client import Message, MQTTConnection

HEARTBEAT_INTERVAL = 15.0
MONITOR_INTERVAL = 0.05
MAX_BACKOFF = 60.0
LIVENESS_TIMEOUT = 2.0


def publication_failures(error: BaseException) -> int:
    if isinstance(error, BaseExceptionGroup):
        return sum(publication_failures(item) for item in error.exceptions)
    return int(
        isinstance(error, MQTTFailureError) and error.operation is Operation.PUBLISH
    )


class Worker(Protocol):
    @property
    def snapshot(self) -> ControllerSnapshot: ...
    @property
    def queue_depth(self) -> int: ...
    def set_session(self, session_id: str | None) -> None: ...
    def refresh(self) -> None: ...
    def submit(
        self,
        endpoint: int,
        command: Command,
        session_id: str,
        *,
        received_at: float | None = None,
    ) -> CommandResult | None: ...
    def drain_results(self) -> tuple[Completion, ...]: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def check(self) -> None: ...
    def join(self) -> None: ...


class SessionStoppedError(Exception):
    pass


class IntervalFinishedError(Exception):
    pass


def random_id() -> str:
    return str(uuid4())


def encode(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False)


def online(view: ControllerSnapshot, session_id: str | None, now: float) -> bool:
    fresh_at = (
        view.last_full_status
        if view.last_full_status is not None
        else view.synchronized_at
    )
    return (
        session_id is not None
        and view.session_id == session_id
        and view.online
        and all(state.usable for state in view.endpoints)
        and fresh_at is not None
        and now - fresh_at < STALE_TIMEOUT
    )


class Bridge:
    def __init__(
        self,
        resolved: ResolvedConfig,
        workers: tuple[Worker, ...],
        mqtt_factory: Callable[[], MQTTConnection],
        *,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random,
        identifiers: Callable[[], str] = random_id,
        logger: ControllerLog | None = None,
    ) -> None:
        if len(workers) != len(resolved.config.controllers):
            message = "one worker is required per controller"
            raise ValueError(message)
        self.config = resolved.config
        self.workers = workers
        self.profiles = tuple(get_profile(c.profile) for c in self.config.controllers)
        self.stop_requested = asyncio.Event()
        self.connected = False
        self.running = False
        self.session_id: str | None = None
        self.progress_at = clock()
        self.heartbeat_at: float | None = None
        self.mqtt_reconnects = 0
        self.mqtt_failures = 0
        self.mqtt_publish_failures = 0
        self.last_error: ErrorCode | None = None
        self.outcomes: dict[tuple[int, int, Outcome, ErrorCode | None], int] = {}
        self.latency_totals: dict[tuple[int, int], tuple[int, float]] = {}
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._identifiers = identifiers
        self._mqtt_factory = mqtt_factory
        self._log = logger or cast("ControllerLog", structlog.get_logger())
        self._admitting = False
        self._synchronized = False
        self._broker_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._ledgers: dict[tuple[int, int], RequestLedger] = {}
        self._routes = {
            f"{self.endpoint_topic(index, endpoint)}/{action}": (
                index,
                endpoint,
                action,
            )
            for index, profile in enumerate(self.profiles)
            for endpoint in range(profile.endpoint_count)
            for action in ("set", "get")
        }

    def stop(self) -> None:
        self._admitting = False
        self.stop_requested.set()
        for worker in self.workers:
            worker.stop()

    def endpoint_topic(self, index: int, endpoint: int) -> str:
        config = self.config.controllers[index]
        zone = config.zone_name(endpoint)
        base = f"{self.config.mqtt.topic_prefix}/{config.name}"
        return base if zone is None else f"{base}/{zone}"

    @property
    def now(self) -> float:
        return self._clock()

    @property
    def healthy(self) -> bool:
        return (
            self.running
            and not self.stop_requested.is_set()
            and self._broker_task is not None
            and not self._broker_task.done()
            and self._monitor_task is not None
            and not self._monitor_task.done()
            and self.now - self.progress_at < LIVENESS_TIMEOUT
        )

    def controller_topic(self, index: int) -> str:
        return (
            f"{self.config.mqtt.topic_prefix}/"
            f"{self.config.controllers[index].name}/availability"
        )

    def _record(
        self, index: int, endpoint: int, result: CommandResult, elapsed: float
    ) -> None:
        key = (index, endpoint, result.outcome, result.error_code)
        self.outcomes[key] = self.outcomes.get(key, 0) + 1
        key_endpoint = (index, endpoint)
        count, total = self.latency_totals.get(key_endpoint, (0, 0.0))
        self.latency_totals[key_endpoint] = count + 1, total + elapsed
        log = self._log.info if result.outcome is Outcome.CONFIRMED else self._log.error
        log(
            "command_result",
            controller=self.config.controllers[index].name,
            zone=self.config.controllers[index].zone_name(endpoint),
            operation="command",
            request_id=result.request_id,
            outcome=result.outcome.value,
            error_code=result.error_code,
            duration=elapsed,
        )

    def _drain(self, index: int) -> tuple[Completion, ...]:
        results = self.workers[index].drain_results()
        for completion in results:
            self._record(
                index,
                completion.request.endpoint,
                completion.result,
                completion.completed_at - completion.request.received_at,
            )
        return results

    async def _result(
        self, client: MQTTConnection, index: int, endpoint: int, result: CommandResult
    ) -> None:
        await client.publish(
            f"{self.endpoint_topic(index, endpoint)}/result",
            encode(result.payload()),
            qos=0,
            retain=False,
        )

    def _identity(self, payload: bytes) -> tuple[str, bytes]:
        request_id = self._identifiers()
        fingerprint = sha256(payload).digest()
        if len(payload) <= MAX_PAYLOAD_BYTES:
            try:
                data = decode(payload)
            except InvalidJSONError:
                return request_id, fingerprint
            if isinstance(data, dict):
                supplied = data.get("request_id")
                if (
                    isinstance(supplied, str)
                    and 1 <= len(supplied) <= MAX_REQUEST_ID
                    and all(ord(char) >= MIN_PRINTABLE for char in supplied)
                ):
                    request_id = supplied
                fingerprint = sha256(
                    json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
                ).digest()
        return request_id, fingerprint

    async def handle(self, client: MQTTConnection, message: Message) -> None:
        received_at = (
            self._clock() if message.received_at is None else message.received_at
        )
        route = self._routes.get(message.topic)
        if route is None:
            return
        index, endpoint, action = route
        request_id, fingerprint = self._identity(message.payload)
        error = (
            ErrorCode.RETAINED_COMMAND
            if message.retained
            else ErrorCode.INVALID_COMMAND
            if action == "get" and message.payload
            else None
        )
        if error is not None:
            result = CommandResult(request_id, Outcome.REJECTED, error)
            self._record(index, endpoint, result, self._clock() - received_at)
            await self._result(client, index, endpoint, result)
            return
        if action == "get":
            self.workers[index].refresh()
            return
        ledger = self._ledgers[index, endpoint]
        decision = ledger.begin(request_id, fingerprint)
        if decision.admission is Admission.PENDING:
            return
        if decision.result is not None:
            if decision.admission is Admission.REJECTED:
                self._record(
                    index, endpoint, decision.result, self._clock() - received_at
                )
            await self._result(client, index, endpoint, decision.result)
            return
        try:
            command = self.profiles[index].parse_command(message.payload, request_id)
        except BridgeError as failure:
            result = CommandResult(request_id, Outcome.REJECTED, failure.code)
        else:
            if (
                not self._admitting
                or self.stop_requested.is_set()
                or not online(
                    self.workers[index].snapshot, self.session_id, self._clock()
                )
            ):
                result = CommandResult(
                    request_id, Outcome.REJECTED, ErrorCode.UNAVAILABLE
                )
            elif self._clock() - received_at >= START_TIMEOUT:
                result = CommandResult(request_id, Outcome.REJECTED, ErrorCode.EXPIRED)
            else:
                result = self.workers[index].submit(
                    endpoint,
                    command,
                    cast("str", self.session_id),
                    received_at=received_at,
                )
            if result is None:
                return
        ledger.finish(result)
        self._record(index, endpoint, result, self._clock() - received_at)
        await self._result(client, index, endpoint, result)

    async def _consume(self, client: MQTTConnection) -> None:
        async for message in client.messages:
            await self.handle(client, message)
        message = "MQTT command stream ended"
        raise MQTTFailureError(message, Operation.RECEIVE)

    async def _heartbeat(self, client: MQTTConnection) -> None:
        while True:
            await client.publish(
                f"{self.config.mqtt.topic_prefix}/bridge/heartbeat",
                encode({"session_id": self.session_id}),
                qos=0,
                retain=False,
            )
            self.heartbeat_at = self._clock()
            await self._sleep(HEARTBEAT_INTERVAL)

    async def _emit(self, client: MQTTConnection) -> None:
        snapshots: dict[tuple[int, int], tuple[str, int, int]] = {}
        availability: dict[int, bool] = {}
        while True:
            for index, worker in enumerate(self.workers):
                # The worker publishes the state a command confirms before it queues
                # the completion, so a snapshot read after the drain holds the state
                # behind every result published below.
                results = self._drain(index)
                view = worker.snapshot
                for endpoint in range(self.profiles[index].endpoint_count):
                    state = (
                        view.endpoints[endpoint]
                        if view.session_id == self.session_id
                        else self.profiles[index].state_factory()
                    )
                    payload = encode(state.payload(cast("str", self.session_id)))
                    version = payload, view.refresh_revision, view.generation
                    if snapshots.get((index, endpoint)) != version:
                        await client.publish(
                            self.endpoint_topic(index, endpoint),
                            payload,
                            qos=1,
                            retain=True,
                        )
                        snapshots[index, endpoint] = version
                available = online(view, self.session_id, self._clock())
                if availability.get(index) != available:
                    await client.publish(
                        self.controller_topic(index),
                        "online" if available else "offline",
                        qos=1,
                        retain=True,
                    )
                    availability[index] = available
                for completion in results:
                    if completion.request.session_id == self.session_id:
                        self._ledgers[index, completion.request.endpoint].finish(
                            completion.result
                        )
                for completion in results:
                    if completion.request.session_id == self.session_id:
                        await self._result(
                            client,
                            index,
                            completion.request.endpoint,
                            completion.result,
                        )
            await self._sleep(MONITOR_INTERVAL)

    async def _stop_session(self) -> None:
        await self.stop_requested.wait()
        self._admitting = False
        for worker in self.workers:
            worker.stop()
        raise SessionStoppedError

    async def _offline(self, client: MQTTConnection) -> None:
        for index in range(len(self.workers)):
            await client.publish(
                self.controller_topic(index), "offline", qos=1, retain=True
            )
        await client.publish(
            f"{self.config.mqtt.topic_prefix}/bridge/availability",
            "offline",
            qos=1,
            retain=True,
        )

    async def _session(self) -> None:
        self._synchronized = False
        async with self._mqtt_factory() as client:
            try:
                try:
                    async with asyncio.TaskGroup() as group:
                        group.create_task(self._stop_session())
                        self.session_id = self._identifiers()
                        self.heartbeat_at = None
                        await self._offline(client)
                        for topic in self._routes:
                            await client.subscribe(topic)
                        self._ledgers = {
                            (index, endpoint): RequestLedger(clock=self._clock)
                            for index in range(len(self.workers))
                            for endpoint in range(self.profiles[index].endpoint_count)
                        }
                        for worker in self.workers:
                            worker.set_session(self.session_id)
                        await client.publish(
                            f"{self.config.mqtt.topic_prefix}/bridge/availability",
                            "online",
                            qos=1,
                            retain=True,
                        )
                        self.connected = True
                        self._synchronized = True
                        self._admitting = True
                        self.last_error = None
                        self._log.info(
                            "mqtt_connected",
                            operation="connect",
                            session_id=self.session_id,
                        )
                        group.create_task(self._consume(client))
                        group.create_task(self._emit(client))
                        group.create_task(self._heartbeat(client))
                except* SessionStoppedError:
                    pass
                await self._offline(client)
            finally:
                self.connected = False
                self._admitting = False
                for worker in self.workers:
                    worker.set_session(None)

    async def _interval(self, delay: float) -> None:
        async def elapsed() -> None:
            await self._sleep(delay)
            raise IntervalFinishedError

        async def stopped() -> None:
            await self.stop_requested.wait()
            raise IntervalFinishedError

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(elapsed())
                group.create_task(stopped())
        except* IntervalFinishedError:
            pass

    async def wait_interval(self, delay: float) -> None:
        await self._interval(delay)

    async def _broker_loop(self) -> None:
        backoff = 1.0
        while not self.stop_requested.is_set():
            try:
                await self._session()
            except* MQTTFailureError as failures:
                self.mqtt_failures += 1
                self.mqtt_publish_failures += publication_failures(failures)
                self.last_error = ErrorCode.MQTT_ERROR
                self._log.error(
                    "mqtt_disconnected",
                    operation="connection",
                    error_code=ErrorCode.MQTT_ERROR,
                )
            if self.stop_requested.is_set():
                break
            if self._synchronized:
                backoff = 1.0
            delay = min(MAX_BACKOFF, backoff * (1 + 0.25 * self._jitter()))
            backoff = min(MAX_BACKOFF, backoff * 2)
            self.mqtt_reconnects += 1
            await self._interval(delay)

    async def _monitor(self) -> None:
        while not self.stop_requested.is_set():
            self.progress_at = self._clock()
            for index, worker in enumerate(self.workers):
                worker.check()
                if not self.connected:
                    self._drain(index)
            await self._interval(MONITOR_INTERVAL)

    async def run(self) -> None:
        try:
            for worker in self.workers:
                worker.start()
            self.running = True
            async with asyncio.TaskGroup() as group:
                self._broker_task = group.create_task(self._broker_loop())
                self._monitor_task = group.create_task(self._monitor())
        finally:
            self.running = False
            self.connected = False
            self._admitting = False
            for worker in self.workers:
                worker.stop()
            results = await asyncio.gather(
                *(asyncio.to_thread(worker.join) for worker in self.workers),
                return_exceptions=True,
            )
            for index in range(len(self.workers)):
                self._drain(index)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
