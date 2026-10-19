import asyncio
import json
from datetime import UTC, datetime
from importlib.resources import files
from typing import cast
lazy from collections.abc import Awaitable, Callable

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST

from .bridge import online
from .log import sanitize
from .metrics import Metrics, age
lazy from .bridge import Bridge

CLIENT_LIMIT = 16
WRITE_TIMEOUT = 2.0
STREAM_HEARTBEAT = 15.0
STATUS_INTERVAL = 1.0


class StatusStoppedError(Exception):
    pass


def wall_time() -> str:
    return datetime.now(UTC).isoformat()


def latest(queue: asyncio.Queue[str | None], value: str | None) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(value)


class StatusServer:
    def __init__(
        self,
        bridge: Bridge,
        secrets: tuple[str, ...],
        versions: dict[str, str],
        *,
        wall_clock: Callable[[], str] = wall_time,
    ) -> None:
        self.bridge = bridge
        self.versions = versions
        self.metrics = Metrics(bridge, secrets)
        self._secrets = secrets
        self._wall_clock = wall_clock
        self._clients: set[asyncio.Queue[str | None]] = set()
        self._runner: web.AppRunner | None = None
        self._closing = False
        self._failure: Exception | None = None
        self._failed = asyncio.Event()
        self._page = files("tuya_mqtt").joinpath("status.html").read_text()
        self.app = web.Application(client_max_size=4096, middlewares=[self._fatal])
        self.app.add_routes(
            [
                web.get("/", self._index),
                web.get("/healthz", self._health),
                web.get("/readyz", self._ready),
                web.get("/metrics", self._metrics),
                web.get("/events", self._events, allow_head=False),
            ]
        )

    @web.middleware
    async def _fatal(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        # Request failures cross aiohttp's boundary to terminate the daemon.
        except Exception as error:  # noqa: BLE001
            self._failure = error
            self._failed.set()
            return web.Response(status=500, text="internal application failure")

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def data(self) -> dict[str, object]:
        bridge = self.bridge
        now = bridge.now
        reasons: list[str] = []
        healthy = bridge.healthy and self._failure is None
        if not healthy:
            reasons.append("coordination_unavailable")
        if not bridge.connected:
            reasons.append("mqtt_disconnected")
        controllers: list[dict[str, object]] = []
        for index, worker in enumerate(bridge.workers):
            config = bridge.config.controllers[index]
            view = worker.snapshot
            available = bridge.connected and online(view, bridge.session_id, now)
            if not available:
                reasons.append(f"controller_unavailable:{config.name}")
            active = view.active
            completion = view.last_completion
            controllers.append(
                {
                    "name": config.name,
                    "connection": view.connection.value,
                    "available": available,
                    "generation": view.generation,
                    "session_id": view.session_id,
                    "last_full_status_age_seconds": age(now, view.last_full_status),
                    "last_traffic_age_seconds": age(now, view.last_traffic),
                    "progress_age_seconds": age(now, view.progress_at),
                    "previous_full_status_age_seconds": age(
                        now, view.previous_full_status_at
                    ),
                    "queue_depth": worker.queue_depth,
                    "last_error": view.last_error.value if view.last_error else None,
                    "pending": None
                    if active is None
                    else {
                        "request_id": active.command.request_id,
                        "zone": config.zone_name(active.endpoint),
                        "requested": dict(active.command.values),
                        "age_seconds": age(now, active.received_at),
                        "deadline_seconds": max(0, active.deadline - now),
                    },
                    "last_result": None
                    if completion is None
                    else {
                        **completion.result.payload(),
                        "zone": config.zone_name(completion.request.endpoint),
                        "age_seconds": age(now, completion.completed_at),
                    },
                    "endpoints": [
                        {
                            "zone": config.zone_name(endpoint),
                            "reported": {
                                field.value: getattr(state, field.value)
                                for field in bridge.profiles[index].fields
                            },
                            "previous": {
                                field.value: getattr(
                                    view.previous_endpoints[endpoint], field.value
                                )
                                for field in bridge.profiles[index].fields
                            },
                            "invalid_fields": sorted(
                                field.value for field in view.invalid_fields[endpoint]
                            ),
                        }
                        for endpoint, state in enumerate(view.endpoints)
                    ],
                }
            )
        payload: dict[str, object] = {
            "observed_at": self._wall_clock(),
            "versions": self.versions,
            "healthy": healthy,
            "ready": not reasons,
            "reasons": reasons,
            "mqtt_connected": bridge.connected,
            "session_id": bridge.session_id,
            "heartbeat_age_seconds": age(now, bridge.heartbeat_at),
            "event_loop_progress_age_seconds": age(now, bridge.progress_at),
            "last_error": bridge.last_error.value if bridge.last_error else None,
            "controllers": controllers,
        }
        return cast("dict[str, object]", sanitize(payload, self._secrets))

    async def start(self, host: str, port: int) -> None:
        self._runner = web.AppRunner(
            self.app, access_log=None, shutdown_timeout=WRITE_TIMEOUT
        )
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, host, port).start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        for queue in self._clients:
            latest(queue, None)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def notify(self) -> None:
        if self._clients:
            payload = json.dumps(self.data(), allow_nan=False)
            for queue in self._clients:
                latest(queue, payload)

    async def _publish(self) -> None:
        while not self.bridge.stop_requested.is_set():
            self.notify()
            await self.bridge.wait_interval(STATUS_INTERVAL)

    async def _raise_failure(self) -> None:
        await self._failed.wait()
        raise cast("Exception", self._failure)

    async def _stopped(self) -> None:
        await self.bridge.stop_requested.wait()
        raise StatusStoppedError

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._publish())
                group.create_task(self._raise_failure())
                group.create_task(self._stopped())
        except* StatusStoppedError:
            pass

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=self._page, content_type="text/html")

    async def _health(self, _request: web.Request) -> web.Response:
        healthy = self.bridge.healthy and self._failure is None
        return web.json_response({"healthy": healthy}, status=200 if healthy else 503)

    async def _ready(self, _request: web.Request) -> web.Response:
        data = self.data()
        return web.json_response(
            {"ready": data["ready"], "reasons": data["reasons"]},
            status=200 if data["ready"] else 503,
        )

    async def _metrics(self, _request: web.Request) -> web.Response:
        return web.Response(
            body=self.metrics.render(), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    async def _events(self, request: web.Request) -> web.StreamResponse:
        if self._closing or len(self._clients) >= CLIENT_LIMIT:
            raise web.HTTPServiceUnavailable(text="status stream unavailable")
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
        self._clients.add(queue)
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        try:
            try:
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await response.prepare(request)
                latest(queue, json.dumps(self.data(), allow_nan=False))
                while True:
                    try:
                        value = await asyncio.wait_for(queue.get(), STREAM_HEARTBEAT)
                    except TimeoutError:
                        payload = b": keepalive\n\n"
                    else:
                        if value is None:
                            break
                        payload = f"data: {value}\n\n".encode()
                    async with asyncio.timeout(WRITE_TIMEOUT):
                        await response.write(payload)
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await response.write_eof()
            except ConnectionError, TimeoutError:
                if request.transport is not None:
                    request.transport.close()
        finally:
            self._clients.discard(queue)
            response.force_close()
        return response
