import runpy
import sys
lazy from pathlib import Path

import pytest

from tuya_mqtt.__main__ import main


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
