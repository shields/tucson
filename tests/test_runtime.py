import asyncio
import json
import signal
from dataclasses import replace
lazy from collections.abc import Callable, Iterator
lazy from pathlib import Path

import pytest
import structlog

from tuya_mqtt import runtime
from tuya_mqtt.config import ResolvedConfig, load_config


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    yield
    structlog.reset_defaults()


@pytest.fixture
def resolved(config_path: Path, secret_env: dict[str, str]) -> ResolvedConfig:
    return load_config(config_path, env=secret_env)


def test_build_defers_device_and_mqtt_creation_to_owners(
    resolved: ResolvedConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[tuple[object, ...]] = []
    factories: list[Callable[[], object]] = []

    def worker(config: object, factory: Callable[[], object]) -> object:
        made.append(("worker", config))
        factories.append(factory)
        return object()

    monkeypatch.setattr(runtime, "ControllerWorker", worker)
    monkeypatch.setattr(
        runtime, "TinyTuyaClient", lambda *args: made.append(("device", *args))
    )
    monkeypatch.setattr(runtime, "Client", lambda *args: made.append(("mqtt", *args)))
    bridge = runtime.build(resolved)
    assert len(made) == 1
    factories[0]()
    assert made[-1] == (
        "device",
        resolved.config.controllers[0],
        resolved.local_keys[0],
    )
    bridge._mqtt_factory()
    assert made[-1] == ("mqtt", resolved, None)


@pytest.mark.parametrize(
    "mode", ["normal", "run_failure", "signal_failure", "no_password"]
)
async def test_runtime_signals_versions_cleanup_and_secret_redaction(
    mode: str,
    resolved: ResolvedConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handlers: dict[signal.Signals, Callable[[], None]] = {}
    removed: list[signal.Signals] = []
    stopped: list[bool] = []
    server_calls: list[str] = []
    loop = asyncio.get_running_loop()
    failure = RuntimeError("runtime failure")

    class Bridge:
        def stop(self) -> None:
            stopped.append(True)

        async def run(self) -> None:
            assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
            if mode == "run_failure":
                raise failure
            handlers[signal.SIGTERM]()

    class Server:
        async def start(self, host: str, port: int) -> None:
            assert host == resolved.config.http.bind
            assert port == resolved.config.http.port
            server_calls.append("start")

        async def run(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            server_calls.append("close")

    def register(signum: signal.Signals, callback: Callable[[], None]) -> None:
        if mode == "signal_failure" and signum is signal.SIGTERM:
            raise failure
        handlers[signum] = callback

    monkeypatch.setattr(loop, "add_signal_handler", register)
    monkeypatch.setattr(loop, "remove_signal_handler", removed.append)
    monkeypatch.setattr(runtime, "build", lambda _resolved: Bridge())
    monkeypatch.setattr(runtime, "StatusServer", lambda *_args: Server())
    monkeypatch.setenv("TUYA_MQTT_REVISION", resolved.local_keys[0])
    if mode == "no_password":
        resolved = replace(resolved, mqtt_password=None)
    if mode in ("run_failure", "signal_failure"):
        with pytest.raises((RuntimeError, ExceptionGroup)):
            await runtime.run(resolved)
    else:
        await runtime.run(resolved)
        assert stopped == [True]
    assert removed == list(handlers)
    assert server_calls[-1] == "close"
    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "starting"
    assert event["revision"] == "[redacted]"
    assert event["python_version"]
    assert event["tinytuya_version"]
    assert event["application_version"]
    assert event["controllers"] == 1
