import json
from pathlib import Path
from typing import Any

import pytest


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
