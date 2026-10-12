import asyncio
import os
import signal
import sys
from functools import partial
from importlib.metadata import version

import structlog

from .bridge import Bridge
from .controller import ControllerWorker
from .log import configure
from .mqtt_client import Client, tls_context
from .status_page import StatusServer
from .tuya_client import TinyTuyaClient
lazy from .config import ResolvedConfig


def build(resolved: ResolvedConfig) -> Bridge:
    context = tls_context(resolved)
    workers = tuple(
        ControllerWorker(config, partial(TinyTuyaClient, config, key))
        for config, key in zip(
            resolved.config.controllers, resolved.local_keys, strict=True
        )
    )
    return Bridge(resolved, workers, lambda: Client(resolved, context))


async def run(resolved: ResolvedConfig) -> None:
    secrets = resolved.local_keys + (
        (resolved.mqtt_password,) if resolved.mqtt_password is not None else ()
    )
    configure(secrets)
    versions = {
        "application": version("tuya-mqtt"),
        "revision": os.environ.get("TUYA_MQTT_REVISION", "unknown"),
        "python": sys.version.split()[0],
        "tinytuya": version("tinytuya"),
    }
    structlog.get_logger().info(
        "starting",
        operation="startup",
        application_version=versions["application"],
        revision=versions["revision"],
        python_version=versions["python"],
        tinytuya_version=versions["tinytuya"],
        controllers=len(resolved.config.controllers),
    )
    bridge = build(resolved)
    server = StatusServer(bridge, secrets, versions)
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, bridge.stop)
            installed.append(signum)
        await server.start(resolved.config.http.bind, resolved.config.http.port)
        async with asyncio.TaskGroup() as group:
            publisher = group.create_task(server.run())
            await bridge.run()
            publisher.cancel()
    finally:
        try:
            await server.close()
        finally:
            for signum in installed:
                loop.remove_signal_handler(signum)
