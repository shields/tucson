import runpy
import sys
from dataclasses import replace
lazy from pathlib import Path

import pytest

import tuya_mqtt.__main__ as cli
from tuya_mqtt.__main__ import main
from tuya_mqtt.config import load_config
from tuya_mqtt.domain import BridgeError, ErrorCode


def test_validate_cli(
    config_path: Path,
    secret_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in secret_env.items():
        monkeypatch.setenv(key, value)
    assert main(["validate-config", "--config", str(config_path)]) == 0
    assert capsys.readouterr().out == "configuration valid\n"


def test_cli_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate-config", "--config", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().err == "invalid_config: invalid configuration\n"


def test_module_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["tuya-mqtt", "validate-config", "--config", str(tmp_path / "missing")],
    )
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as caught:
        runpy.run_module("tuya_mqtt.__main__", run_name="__main__")
    assert caught.value.code == 1


def test_missing_subcommand() -> None:
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2


@pytest.mark.parametrize("error", [None, ErrorCode.CONNECTION_LOST])
def test_inspection_uses_adapter_and_always_closes(
    error: ErrorCode | None,
    config_path: Path,
    secret_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in secret_env.items():
        monkeypatch.setenv(key, value)
    closed: list[bool] = []

    class Inspector:
        def connect(self) -> dict[str, object]:
            if error is not None:
                raise BridgeError(error, "device connection lost")
            return {"14": False, "999": secret_env["SUNBEAM_LOCAL_KEY"]}

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(cli, "TinyTuyaClient", lambda *_args: Inspector())
    status = main(["inspect", "--config", str(config_path), "--controller", "bed"])
    assert status == (1 if error is not None else 0)
    captured = capsys.readouterr()
    if error is None:
        assert captured.out == '{"14": false, "999": "[redacted]"}\n'
    else:
        assert captured.err == "connection_lost: device connection lost\n"
    assert closed == [True]


def test_inspection_unknown_controller(
    config_path: Path,
    secret_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in secret_env.items():
        monkeypatch.setenv(key, value)
    assert (
        main(["inspect", "--config", str(config_path), "--controller", "unknown"]) == 1
    )
    assert capsys.readouterr().err == "unknown controller\n"


@pytest.mark.parametrize("failure", [False, True])
def test_run_cli_returns_nonzero_on_unexpected_failure(
    *,
    failure: bool,
    config_path: Path,
    secret_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in secret_env.items():
        monkeypatch.setenv(key, value)

    async def run(_resolved: object) -> None:
        if failure:
            raise ValueError(secret_env["SUNBEAM_LOCAL_KEY"])

    monkeypatch.setattr(cli, "run", run)
    assert main(["run", "--config", str(config_path)]) == (1 if failure else 0)
    output = capsys.readouterr().out
    assert secret_env["SUNBEAM_LOCAL_KEY"] not in output
    if failure:
        assert "ValueError" in output
        assert "fatal" in output


def test_inspection_without_mqtt_password(
    config_path: Path,
    secret_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolved = replace(load_config(config_path, env=secret_env), mqtt_password=None)

    class Inspector:
        def connect(self) -> dict[str, object]:
            return {"999": secret_env["SUNBEAM_LOCAL_KEY"]}

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "load_config", lambda _path: resolved)
    monkeypatch.setattr(cli, "TinyTuyaClient", lambda *_args: Inspector())
    assert main(["inspect", "--config", str(config_path), "--controller", "bed"]) == 0
    assert capsys.readouterr().out == '{"999": "[redacted]"}\n'
