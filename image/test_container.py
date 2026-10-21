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
import os
import shutil
import socket
import subprocess
from contextlib import suppress
from dataclasses import dataclass
lazy from collections.abc import Iterator
lazy from pathlib import Path
lazy from typing import Any

import aiomqtt
import pytest
from aiohttp import ClientSession

from tests.process import Observed, ready
from tests.tcp import KEY
lazy from tests.broker import Broker
lazy from tests.tcp import Simulator

IMPORTS = """
import os, sys, errno, ssl, ctypes, bz2, lzma, sqlite3, decimal
import tinytuya, aiomqtt, aiohttp, pydantic, prometheus_client, structlog
from importlib.metadata import distributions, version
from importlib.resources import files
assert os.getuid() == os.getgid() == 65532
assert sys.version.split()[0] == os.environ['TUCSON_EXPECTED_PYTHON']
assert version('tinytuya') == '1.20.0'
assert not any(path.name == 'uv_cache.json'
    for distribution in distributions() for path in distribution.files or [])
assert files('tuya_mqtt').joinpath('status.html').is_file()
assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0
ctypes.CDLL('libgcc_s.so.1')
paths = ('/bin/sh', '/bin/bash', '/usr/bin/curl')
assert not any(os.path.exists(path) for path in paths)
try:
    open('/tmp/write-probe', 'w')
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError('runtime filesystem is writable')
"""


@dataclass(frozen=True)
class Docker:
    executable: str
    image: str

    def call(self, *args: str) -> str:
        result = subprocess.run(
            [self.executable, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()

    def options(self) -> tuple[str, ...]:
        return (
            "--platform",
            os.environ["TUCSON_PLATFORM"],
            "--network",
            "host",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
        )


@pytest.fixture(scope="session")
def docker() -> Docker:
    executable = shutil.which("docker")
    assert executable is not None, "Docker is required for image checks"
    service = Docker(executable, os.environ["TUCSON_IMAGE"])
    metadata = json.loads(service.call("image", "inspect", service.image))[0]
    assert metadata["Architecture"] == os.environ["TUCSON_PLATFORM"].split("/")[1]
    assert metadata["Config"]["User"] == "65532:65532"
    labels = metadata["Config"]["Labels"]
    assert labels["org.opencontainers.image.revision"] == os.environ["TUCSON_REVISION"]
    assert labels["org.opencontainers.image.version"] == os.environ["TUCSON_VERSION"]
    assert labels["org.opencontainers.image.source"] == (
        "https://github.com/shields/tucson"
    )
    return service


@pytest.fixture
def configuration(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    directory = tmp_path / "config"
    directory.mkdir(mode=0o755)
    tmp_path.chmod(0o755)
    (directory / "local-key").write_text(KEY)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    data = {
        "mqtt": {"host": "127.0.0.1", "port": 1, "client_id": "image-smoke"},
        "http": {"bind": "127.0.0.1", "port": port},
        "controllers": [
            {
                "name": "bed",
                "device_id": "image-smoke-controller",
                "ip": "192.0.2.10",
                "protocol_version": "3.3",
                "local_key": {"file": "/config/local-key"},
                "profile": "sunbeam-dual",
                "zones": ["side_a", "side_b"],
            }
        ],
    }
    (directory / "config.json").write_text(json.dumps(data))
    return directory, data


@pytest.fixture
def container(
    docker: Docker, configuration: tuple[Path, dict[str, Any]]
) -> Iterator[str]:
    directory, _data = configuration
    identifier = docker.call(
        "create",
        *docker.options(),
        "--mount",
        f"type=bind,source={directory},target=/config,readonly",
        docker.image,
    )
    try:
        yield identifier
    finally:
        docker.call("rm", "--force", identifier)


def test_image_loads_runtime_dependencies_and_enforces_user_and_filesystem(
    docker: Docker,
) -> None:
    root = Path(__file__).resolve().parents[1]
    python_version = next(
        line.split()[-1]
        for line in (root / "Dockerfile").read_text().splitlines()
        if line.startswith("RUN uv python install ")
    )
    docker.call(
        "run",
        "--rm",
        *docker.options(),
        "--env",
        f"TUCSON_EXPECTED_PYTHON={python_version}",
        "--entrypoint",
        "/opt/app/bin/python",
        docker.image,
        "-c",
        IMPORTS,
    )
    assert "validate-config" in docker.call(
        "run", "--rm", *docker.options(), docker.image, "--help"
    )


def test_image_validates_mounted_configuration_without_network_or_secret_output(
    docker: Docker, configuration: tuple[Path, dict[str, Any]]
) -> None:
    directory, _data = configuration
    output = docker.call(
        "run",
        "--rm",
        *docker.options(),
        "--mount",
        f"type=bind,source={directory},target=/config,readonly",
        docker.image,
        "validate-config",
        "--config",
        "/config/config.json",
    )
    assert KEY not in output
    (directory / "local-key").write_text("invalid-key")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        docker.call(
            "run",
            "--rm",
            *docker.options(),
            "--mount",
            f"type=bind,source={directory},target=/config,readonly",
            docker.image,
            "validate-config",
            "--config",
            "/config/config.json",
        )
    diagnostic = failure.value.output + failure.value.stderr
    assert "invalid_config" in diagnostic
    assert "invalid-key" not in diagnostic


async def wait_http(client: ClientSession, base: str) -> None:
    async with asyncio.timeout(15):
        while True:
            try:
                async with client.get(f"{base}/healthz") as response:
                    if response.status == 200:
                        assert await response.json() == {"healthy": True}
                        return
            except OSError:
                pass
            await asyncio.sleep(0.025)


@pytest.mark.parametrize("signal", ["SIGTERM", "SIGINT"])
async def test_image_reports_dependency_outages_and_stops_on_signals(
    signal: str,
    docker: Docker,
    configuration: tuple[Path, dict[str, Any]],
    container: str,
) -> None:
    _directory, data = configuration
    base = f"http://127.0.0.1:{data['http']['port']}"
    docker.call("start", container)
    async with ClientSession() as client:
        await wait_http(client, base)
        async with client.get(f"{base}/readyz") as response:
            assert response.status == 503
            assert "mqtt_disconnected" in (await response.json())["reasons"]
        async with client.get(f"{base}/metrics") as response:
            assert "tuya_mqtt_mqtt_connected 0.0" in await response.text()
        async with client.get(f"{base}/events") as response:
            line = await asyncio.wait_for(response.content.readline(), 3)
            snapshot = json.loads(line[6:])
            assert snapshot["versions"]["revision"] == os.environ["TUCSON_REVISION"]
            assert snapshot["versions"]["application"] == os.environ["TUCSON_VERSION"]
            assert KEY not in json.dumps(snapshot)
            assert (
                snapshot["controllers"][0]["endpoints"][0]["reported"]["power"] is None
            )
        async with client.get(base) as response:
            assert response.status == 200
            assert "EventSource" in await response.text()
    docker.call("kill", "--signal", signal, container)
    assert await asyncio.wait_for(
        asyncio.to_thread(docker.call, "wait", container), 12
    ) == ("0")
    assert KEY not in docker.call("logs", container)


@pytest.mark.parametrize("simulator", ["3.3", "3.4", "3.5"], indirect=True)
async def test_image_runs_controller_protocols_and_publishes_offline_on_shutdown(
    docker: Docker,
    configuration: tuple[Path, dict[str, Any]],
    container: str,
    broker: Broker,
    simulator: Simulator,
) -> None:
    directory, data = configuration
    data["mqtt"]["port"] = broker.port
    data["controllers"][0] |= {
        "ip": "127.0.0.1",
        "port": simulator.mock.port,
        "protocol_version": simulator.mock.config.version,
        "write_mode": "multi_dp",
    }
    (directory / "config.json").write_text(json.dumps(data))
    await broker.ready()
    observed = Observed()
    async with aiomqtt.Client(
        "127.0.0.1", broker.port, protocol=aiomqtt.ProtocolVersion.V5
    ) as client:
        await client.subscribe("tuya/#", qos=1)
        consumer = asyncio.create_task(observed.consume(client))
        try:
            docker.call("start", container)
            await ready(observed)
            await client.publish(
                "tuya/bed/side_b/set",
                '{"request_id":"image-smoke","power":true,"heat_level":8}',
                qos=0,
            )
            await observed.wait(lambda data: "tuya/bed/side_b/result" in data.latest)
            assert observed.payload("tuya/bed/side_b/result")["outcome"] == "confirmed"
            assert simulator.writes == [{"15": True, "21": "level_8"}]
            base = f"http://127.0.0.1:{data['http']['port']}"
            async with (
                ClientSession() as http,
                http.get(f"{base}/readyz") as response,
            ):
                assert response.status == 200
            docker.call("kill", "--signal", "SIGTERM", container)
            assert (
                await asyncio.wait_for(
                    asyncio.to_thread(docker.call, "wait", container), 12
                )
                == "0"
            )
            await observed.wait(
                lambda data: (
                    data.latest["tuya/bridge/availability"].payload == b"offline"
                )
            )
            assert observed.latest["tuya/bed/availability"].payload == b"offline"
            assert KEY not in docker.call("logs", container)
        finally:
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer
