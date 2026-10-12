import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from threading import get_ident
from typing import Self
lazy from collections.abc import AsyncIterator, Callable

from tuya_mqtt.controller import CONFIRMATION_TIMEOUT, START_TIMEOUT
from tuya_mqtt.domain import (
    Command,
    CommandResult,
    Completion,
    ConnectionState,
    ControllerSnapshot,
    ErrorCode,
    Outcome,
    Request,
    ZoneState,
)
lazy from tuya_mqtt.mqtt_client import Message

from .controller_fakes import Clock

BASELINE = ZoneState(
    power=False,
    heat_level=4,
    preheat=False,
    auto_off_seconds=3600,
    remaining_seconds=3600,
    preheat_remaining_seconds=0,
)


class Scheduler(Clock):
    def __init__(self) -> None:
        super().__init__()
        self.waiters: list[tuple[float, asyncio.Future[None]]] = []
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        item = (self.now + delay, asyncio.get_running_loop().create_future())
        self.waiters.append(item)
        try:
            await item[1]
        finally:
            self.waiters.remove(item)

    def advance(self, seconds: float) -> None:
        super().advance(seconds)
        for due, future in self.waiters:
            if due <= self.now and not future.done():
                future.set_result(None)


@dataclass
class Worker:
    clock: Clock
    auto_online: bool = True
    auto_confirm: bool = True
    admission_error: ErrorCode | None = None
    start_error: Exception | None = None
    check_error: Exception | None = None
    join_error: Exception | None = None
    sessions: list[str | None] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    completed: deque[Completion] = field(default_factory=deque)
    view: ControllerSnapshot = field(default_factory=ControllerSnapshot)
    started: int = 0
    stopped: int = 0
    joined: int = 0
    refreshes: int = 0
    join_threads: set[int] = field(default_factory=set)

    @property
    def snapshot(self) -> ControllerSnapshot:
        return self.view

    @property
    def queue_depth(self) -> int:
        return len(self.requests)

    def set_session(self, session_id: str | None) -> None:
        self.sessions.append(session_id)
        available = session_id is not None and self.auto_online
        self.view = replace(
            self.view,
            session_id=session_id,
            generation=self.view.generation + 1,
            connection=ConnectionState.ONLINE
            if available
            else ConnectionState.SYNCHRONIZING,
            zones=(BASELINE, BASELINE) if available else (ZoneState(), ZoneState()),
            synchronized_at=self.clock() if available else None,
            last_full_status=self.clock() if available else None,
        )

    def refresh(self) -> None:
        self.refreshes += 1
        self.view = replace(
            self.view,
            refresh_revision=self.view.refresh_revision + 1,
            last_full_status=self.clock(),
        )

    def submit(
        self,
        zone: int,
        command: Command,
        session_id: str,
        *,
        received_at: float | None = None,
    ) -> CommandResult | None:
        if self.admission_error is not None:
            return CommandResult(
                command.request_id, Outcome.REJECTED, self.admission_error
            )
        now = self.clock() if received_at is None else received_at
        request = Request(
            zone,
            command,
            session_id,
            self.view.generation,
            now,
            now + START_TIMEOUT,
            now + CONFIRMATION_TIMEOUT,
        )
        self.requests.append(request)
        if self.auto_confirm:
            self.confirm(request)
        return None

    def confirm(self, request: Request) -> None:
        zones = list(self.view.zones)
        zones[request.zone] = zones[request.zone].merge(request.command.values)
        self.view = replace(self.view, zones=tuple(zones))
        self.completed.append(
            Completion(
                request,
                CommandResult(request.command.request_id, Outcome.CONFIRMED),
                self.clock(),
            )
        )

    def drain_results(self) -> tuple[Completion, ...]:
        results = tuple(self.completed)
        self.completed.clear()
        return results

    def start(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> None:
        self.stopped += 1

    def check(self) -> None:
        if self.check_error is not None:
            raise self.check_error

    def join(self) -> None:
        self.joined += 1
        self.join_threads.add(get_ident())
        if self.join_error is not None:
            raise self.join_error


@dataclass
class MQTT:
    enter_error: Exception | None = None
    subscribe_error: Exception | None = None
    publish_error: Exception | None = None
    fail_topic: str | None = None
    exit_hook: Callable[[], None] | None = None
    entered: int = 0
    exited: int = 0
    delivered: int = 0
    subscriptions: list[str] = field(default_factory=list)
    publications: list[tuple[str, str, int, bool]] = field(default_factory=list)
    incoming: asyncio.Queue[Message | Exception | None] = field(
        default_factory=asyncio.Queue
    )

    async def __aenter__(self) -> Self:
        self.entered += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1
        if self.exit_hook is not None:
            self.exit_hook()

    async def subscribe(self, topic: str) -> None:
        self.subscriptions.append(topic)
        if self.subscribe_error is not None:
            raise self.subscribe_error

    async def publish(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> None:
        self.publications.append((topic, payload, qos, retain))
        if self.publish_error is not None and (
            self.fail_topic is None or topic == self.fail_topic
        ):
            raise self.publish_error

    @property
    def messages(self) -> AsyncIterator[Message]:
        return self.stream()

    async def stream(self) -> AsyncIterator[Message]:
        while True:
            item = await self.incoming.get()
            if isinstance(item, Exception):
                raise item
            if item is None:
                return
            self.delivered += 1
            yield item


async def eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            ready = asyncio.Event()
            asyncio.get_running_loop().call_soon(ready.set)
            await ready.wait()
