import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import replace
from importlib.resources import files
lazy from collections.abc import AsyncIterator
lazy from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import tuya_mqtt.status_page as status_module
from tuya_mqtt.bridge import Bridge
from tuya_mqtt.config import load_config
from tuya_mqtt.domain import (
    CommandResult,
    Completion,
    CurtainField,
    ErrorCode,
    Field,
    Outcome,
    Request,
)
from tuya_mqtt.profiles.sunbeam import parse_command
from tuya_mqtt.status_page import CLIENT_LIMIT, StatusServer, latest

from .bridge_fakes import MQTT, Scheduler, Worker, eventually
from .controller_fakes import Log


@pytest.fixture
async def rig(
    config_path: Path, secret_env: dict[str, str]
) -> AsyncIterator[tuple[StatusServer, TestClient, Worker, Scheduler]]:
    resolved = load_config(config_path, env=secret_env)
    clock = Scheduler()
    worker = Worker(clock)
    mqtt = MQTT()
    bridge = Bridge(
        resolved, (worker,), lambda: mqtt, clock=clock, sleep=clock.sleep, logger=Log()
    )
    server = StatusServer(
        bridge,
        (*resolved.local_keys, resolved.mqtt_password),
        {"application": "test", "revision": resolved.local_keys[0]},
        wall_clock=lambda: "test time",
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    task = asyncio.create_task(bridge.run())
    await eventually(lambda: bridge.connected and bridge.healthy)
    try:
        yield server, client, worker, clock
    finally:
        bridge.stop()
        await task
        await server.close()
        await client.close()


async def test_http_health_readiness_metrics_and_read_only_page(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
) -> None:
    server, client, worker, clock = rig
    health = await client.get("/healthz")
    assert health.status == 200
    assert await health.json() == {"healthy": True}
    ready = await client.get("/readyz")
    assert ready.status == 200
    assert await ready.json() == {"ready": True, "reasons": []}
    metrics = await client.get("/metrics")
    assert metrics.headers["Content-Type"].startswith("text/plain")
    assert (
        'tuya_mqtt_controller_available{controller="bed"} 1.0' in await metrics.text()
    )
    page = await client.get("/")
    assert page.status == 200
    assert "EventSource" in await page.text()
    assert (await client.post("/")).status == 405
    server.bridge.connected = False
    unavailable = await client.get("/readyz")
    assert unavailable.status == 503
    assert "mqtt_disconnected" in (await unavailable.json())["reasons"]
    assert (await client.get("/healthz")).status == 200
    server.bridge.connected = True
    clock.advance(90)
    await eventually(lambda: server.bridge.progress_at == 90)
    assert (await client.get("/readyz")).status == 503
    worker.refresh()
    assert (await client.get("/readyz")).status == 200
    server.bridge.stop()
    assert (await client.get("/healthz")).status == 503
    assert (await client.get("/readyz")).status == 503


def test_status_page_labels_every_profile_field() -> None:
    page = files("tuya_mqtt").joinpath("status.html").read_text()
    literal = re.search(r"const fields = \{([^}]*)\}", page)
    assert literal is not None
    assert re.findall(r"^\s*(\w+):", literal[1], re.MULTILINE) == [
        field.value for field in (*Field, *CurtainField)
    ]


async def test_status_page_renders_profile_fields(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
) -> None:
    server, _client, _worker, _clock = rig
    data = server.data()
    bedding = data["controllers"][0]
    data["controllers"].append(
        bedding
        | {
            "name": "Living room",
            "zones": [
                {
                    "name": "Curtain",
                    "reported": {
                        "current_position": 40,
                        "target_position": 80,
                        "motion": "opening",
                    },
                    "previous": {
                        "current_position": 0,
                        "target_position": None,
                        "motion": None,
                    },
                    "invalid_fields": [],
                }
            ],
        }
    )
    page = files("tuya_mqtt").joinpath("status.html").read_text()
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert script is not None
    bun = shutil.which("bun")
    assert bun is not None, "Bun is required to test the status-page renderer"
    result = await asyncio.to_thread(
        subprocess.run,
        [
            bun,
            "--eval",
            """
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
const { script, snapshot } = JSON.parse(readFileSync(0, "utf8"));
class Element {
    constructor(tag) { this.tag = tag; this.children = []; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    set textContent(text) { this.text = String(text); }
}
const content = new Element("div");
const stream = new Element("p");
let events;
runInNewContext(script, {
    document: {
        getElementById: id => id === "content" ? content : stream,
        createElement: tag => new Element(tag),
    },
    EventSource: class { constructor() { events = this; } },
});
const tables = [];
function collect(element) {
    if (element.tag === "table") {
        tables.push(element.children.map(row => row.children.map(cell => cell.text)));
    }
    for (const child of element.children) collect(child);
}
events.onmessage({ data: JSON.stringify(snapshot) });
collect(content);
process.stdout.write(JSON.stringify(tables));
""",
        ],
        input=json.dumps({"script": script[1], "snapshot": data}),
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    tables = json.loads(result.stdout)
    assert len(tables) == 6
    assert tables[0] == [
        ["Power", "Off"],
        ["Heat level", "4"],
        ["Preheat", "Off"],
        ["Auto-off setting", "3600\u202fs"],
        ["Reported time remaining", "3600\u202fs"],
        ["Reported preheat time remaining", "0\u202fs"],
        ["Reported fault bits", "0"],
    ]
    assert all(value == "Unknown" for _label, value in tables[1])
    assert tables[4] == [
        ["Current position", "40"],
        ["Target position", "80"],
        ["Motion", "opening"],
    ]
    assert tables[5] == [
        ["Current position", "0"],
        ["Target position", "Unknown"],
        ["Motion", "Unknown"],
    ]
    assert "undefined" not in result.stdout


async def test_diagnostics_keep_unknown_previous_pending_and_results_separate(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
) -> None:
    server, _client, worker, clock = rig
    command = parse_command(b'{"request_id":"test-key-16-byte","power":true}')
    request = Request(0, command, "session", 1, 0, 2, 8)
    result = CommandResult(
        command.request_id, Outcome.UNCONFIRMED, ErrorCode.CONNECTION_LOST
    )
    worker.view = replace(
        worker.view,
        active=request,
        last_completion=Completion(request, result, 0),
        previous_zones=worker.view.zones,
        previous_full_status_at=0,
        invalid_fields=(
            frozenset({field for field, _value in command.values}),
            frozenset(),
        ),
    )
    clock.advance(1)
    data = server.data()
    assert data["versions"]["revision"] == "[redacted]"
    controller = data["controllers"][0]
    assert controller["pending"]["requested"] == {"power": True}
    assert controller["pending"]["request_id"] == "[redacted]"
    assert controller["last_result"]["outcome"] == "unconfirmed"
    assert controller["zones"][0]["reported"]["power"] is False
    assert controller["previous_full_status_age_seconds"] == 1


async def test_status_stream_initial_update_capacity_and_cleanup(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
) -> None:
    server, client, _worker, _clock = rig
    response = await client.get("/events")
    line = await response.content.readline()
    assert line.startswith(b"data: ")
    assert json.loads(line[6:])["ready"] is True
    assert server.client_count == 1
    for _ in range(100):
        server.notify()
    assert all(queue.qsize() == 1 for queue in server._clients)
    responses = [response]
    responses.extend([await client.get("/events") for _ in range(CLIENT_LIMIT - 1)])
    assert server.client_count == CLIENT_LIMIT
    assert (await client.get("/events")).status == 503
    assert (await client.head("/events")).status == 405
    await server.close()
    for connection in responses:
        await connection.read()
        connection.close()
    await eventually(lambda: server.client_count == 0)
    assert (await client.get("/events")).status == 503


async def test_request_failure_is_sanitized_and_fatal(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, client, _worker, _clock = rig

    def fail() -> bytes:
        message = "test-key-16-byte"
        raise ValueError(message)

    monkeypatch.setattr(server.metrics, "render", fail)
    response = await client.get("/metrics")
    assert response.status == 500
    assert await response.text() == "internal application failure"
    assert (await client.get("/healthz")).status == 503
    with pytest.raises(ExceptionGroup):
        await server.run()


async def test_server_bind_failure_cleanup_and_latest_slot(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _client, _worker, _clock = rig

    class Site:
        async def start(self) -> None:
            message = "bind failed"
            raise OSError(message)

    monkeypatch.setattr(web, "TCPSite", lambda *_args: Site())
    with pytest.raises(OSError, match="bind failed"):
        await server.start("127.0.0.1", 0)
    assert server._runner is None
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
    latest(queue, "one")
    latest(queue, "two")
    assert queue.get_nowait() == "two"


async def test_real_status_listener(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
) -> None:
    server, _client, _worker, _clock = rig
    other = StatusServer(server.bridge, (), {})
    await other.start("127.0.0.1", 0)
    try:
        site = next(iter(other._runner.sites))
        port = site._server.sockets[0].getsockname()[1]
        async with ClientSession() as client:
            response = await client.get(f"http://127.0.0.1:{port}/healthz")
            assert response.status == 200
    finally:
        await other.close()


async def test_publisher_updates_then_stops_and_default_wall_clock(
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
) -> None:
    server, client, _worker, clock = rig
    other = StatusServer(server.bridge, (), {})
    assert "+00:00" in other.data()["observed_at"]
    response = await client.get("/events")
    await response.content.readline()
    await response.content.readline()
    publisher = asyncio.create_task(server.run())
    await response.content.readline()
    clock.advance(1)
    await response.content.readline()
    server.bridge.stop()
    await asyncio.wait_for(publisher, 3)
    await server.run()
    await server.close()
    response.close()


@pytest.mark.parametrize(
    "mode",
    [
        "heartbeat",
        "slow",
        "prepare_failure",
        "prepare_slow",
        "no_transport",
        "eof_failure",
    ],
)
async def test_stream_heartbeats_slow_readers_and_disconnects_release_capacity(
    mode: str,
    rig: tuple[StatusServer, TestClient, Worker, Scheduler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _client, _worker, _clock = rig
    written: list[bytes] = []
    closed: list[bool] = []

    class Transport(asyncio.Transport):
        def close(self) -> None:
            closed.append(True)

    class Response:
        async def prepare(self, _request: web.Request) -> None:
            if mode == "prepare_slow":
                await asyncio.Event().wait()
            if mode in ("prepare_failure", "no_transport"):
                message = "client disappeared"
                raise ConnectionError(message)

        async def write(self, payload: bytes) -> None:
            written.append(payload)
            if mode == "slow":
                await asyncio.Event().wait()
            if payload.startswith(b": keepalive"):
                await server.close()

        async def write_eof(self) -> None:
            if mode == "eof_failure":
                message = "client disappeared"
                raise ConnectionError(message)

        def force_close(self) -> None:
            pass

    monkeypatch.setattr(
        status_module.web, "StreamResponse", lambda **_kwargs: Response()
    )
    monkeypatch.setattr(status_module, "STREAM_HEARTBEAT", 0.005)
    monkeypatch.setattr(status_module, "WRITE_TIMEOUT", 0.005)
    transport = Transport()
    request = make_mocked_request("GET", "/events", transport=transport)
    if mode == "no_transport":
        monkeypatch.setattr(type(request), "transport", property(lambda _self: None))
    task = asyncio.create_task(server._events(request))
    if mode == "eof_failure":
        await eventually(lambda: bool(written))
        await server.close()
    await asyncio.wait_for(task, 3)
    assert server.client_count == 0
    if mode == "heartbeat":
        assert any(payload.startswith(b": keepalive") for payload in written)
        assert not closed
    elif mode == "no_transport":
        assert not closed
    else:
        assert closed == [True]
