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
from io import StringIO
lazy from collections.abc import Iterator

import pytest
import structlog
import tinytuya

from tuya_mqtt.log import UnsafeLogValueError, configure


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    yield
    structlog.reset_defaults()


def test_nested_redaction_primitive_values_and_level_filtering() -> None:
    stream = StringIO()
    key, password = "test-key-16-byte", "test-password"
    configure((key, password, key), stream=stream)
    logger = structlog.get_logger()
    logger.debug("disabled", value=object())
    logger.info(
        "observed",
        values=[None, True, 4, 2.5, (f"key={key}", {password: f"password={password}"})],
    )
    output = stream.getvalue()
    assert key not in output
    assert password not in output
    event = json.loads(output)
    assert event["event"] == "observed"
    assert event["level"] == "info"
    assert event["timestamp"].endswith("Z")
    assert event["values"] == [
        None,
        True,
        4,
        2.5,
        ["key=[redacted]", {"[redacted]": "password=[redacted]"}],
    ]


def test_credential_bearing_objects_cannot_reach_repr_or_renderer() -> None:
    stream = StringIO()
    key = "test-key-16-byte"
    configure((key,), stream=stream)

    class Opaque:
        def __repr__(self) -> str:
            message = "repr must not run"
            raise AssertionError(message)

        def __structlog__(self) -> str:
            message = "serialization must not run"
            raise AssertionError(message)

    device = tinytuya.Device("synthetic-device", "127.0.0.1", key, version=3.3)
    assert key in repr(device)
    for value in (device, Opaque(), {1: "unsupported key"}):
        with pytest.raises(
            UnsafeLogValueError, match="unsupported structured log value"
        ):
            structlog.get_logger().error("unsafe", value=value)
    assert stream.getvalue() == ""


def test_default_output_and_nonfinite_values_fail_loudly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(())
    structlog.get_logger().error("outage", controller="bed", error_code="mqtt_error")
    event = json.loads(capsys.readouterr().out)
    assert event["level"] == "error"
    with pytest.raises(ValueError, match="Out of range float values"):
        structlog.get_logger().info("invalid", duration=float("nan"))
    assert capsys.readouterr().out == ""
