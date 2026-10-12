import json
from pathlib import Path
from typing import Any

import pytest

from tuya_mqtt.config import ControllerConfig, load_config

pytest_plugins = ["tests.broker"]


@pytest.fixture
def config_data() -> dict[str, Any]:
    return json.loads(Path("config.example.json").read_text())


@pytest.fixture
def config_path(tmp_path: Path, config_data: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    return path


@pytest.fixture
def secret_env() -> dict[str, str]:
    return {"SUNBEAM_LOCAL_KEY": "test-key-16-byte", "MQTT_PASSWORD": "test-password"}


@pytest.fixture
def controller_config(
    config_path: Path, secret_env: dict[str, str]
) -> ControllerConfig:
    return load_config(config_path, env=secret_env).config.controllers[0]
