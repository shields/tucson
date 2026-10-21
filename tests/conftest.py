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

import json
from pathlib import Path
from typing import Any

import pytest

from tuya_mqtt.config import ControllerConfig, load_config

pytest_plugins = ["tests.broker", "tests.tcp"]


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


@pytest.fixture
def zemismart_config(controller_config: ControllerConfig) -> ControllerConfig:
    return ControllerConfig.model_validate(
        controller_config.model_dump(exclude={"zones"})
        | {
            "profile": "zemismart-bcd300fb",
            "protocol_version": "3.3",
            "write_mode": "multi_dp",
        }
    )
