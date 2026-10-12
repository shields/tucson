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
    structlog.get_logger().info(
        "starting",
        operation="startup",
        application_version=version("tuya-mqtt"),
        revision=os.environ.get("TUYA_MQTT_REVISION", "unknown"),
        python_version=sys.version.split()[0],
        tinytuya_version=version("tinytuya"),
        controllers=len(resolved.config.controllers),
    )
    bridge = build(resolved)
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, bridge.stop)
            installed.append(signum)
        await bridge.run()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
