from dataclasses import replace
lazy from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

from tuya_mqtt.bridge import Bridge, publication_failures
from tuya_mqtt.config import load_config
from tuya_mqtt.domain import ConnectionState, ErrorCode, Outcome, ZoneState
from tuya_mqtt.metrics import Metrics, age
from tuya_mqtt.mqtt_client import MQTTFailureError, Operation

from .bridge_fakes import MQTT, Worker
from .controller_fakes import Clock, Log


def samples(metrics: Metrics) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(metrics.render().decode())
        for sample in family.samples
    }


@pytest.fixture
def rig(config_path: Path, secret_env: dict[str, str]) -> tuple[Metrics, Worker]:
    resolved = load_config(config_path, env=secret_env)
    clock = Clock(now=10)
    worker = Worker(clock)
    bridge = Bridge(resolved, (worker,), MQTT, clock=clock, logger=Log())
    return Metrics(bridge, resolved.local_keys), worker


def test_metrics_follow_shared_state_and_bound_labels(
    rig: tuple[Metrics, Worker],
) -> None:
    metrics, worker = rig
    bridge = metrics.bridge
    initial = samples(metrics)
    assert initial["tuya_mqtt_mqtt_connected", ()] == 0
    assert initial[
        "tuya_mqtt_controller_last_full_status_age_seconds", (("controller", "bed"),)
    ] == float("inf")
    worker.set_session("session")
    bridge.session_id = "session"
    bridge.connected = True
    bridge.heartbeat_at = 9
    worker.view = replace(
        worker.view,
        last_full_status=7,
        progress_at=8,
        reconnects=3,
        operation_failures=4,
        last_error=ErrorCode.COMPATIBILITY,
    )
    bridge.mqtt_reconnects = 2
    bridge.mqtt_failures = 5
    bridge.mqtt_publish_failures = 1
    bridge.outcomes[0, 0, Outcome.CONFIRMED, None] = 1
    bridge.outcomes[0, 1, Outcome.REJECTED, ErrorCode.INVALID_COMMAND] = 2
    bridge.latency_totals[0, 0] = 1, 0.5
    observed = samples(metrics)
    assert observed["tuya_mqtt_controller_available", (("controller", "bed"),)] == 1
    assert (
        observed[
            "tuya_mqtt_controller_last_full_status_age_seconds",
            (("controller", "bed"),),
        ]
        == 3
    )
    assert (
        observed["tuya_mqtt_controller_progress_age_seconds", (("controller", "bed"),)]
        == 2
    )
    assert observed["tuya_mqtt_mqtt_publish_failures_total", ()] == 1
    assert (
        observed[
            "tuya_mqtt_controller_operation_failures_total", (("controller", "bed"),)
        ]
        == 4
    )
    assert (
        observed[
            "tuya_mqtt_command_duration_seconds_sum",
            (("controller", "bed"), ("zone", "side_a")),
        ]
        == 0.5
    )
    assert all("request_id" not in dict(labels) for _name, labels in observed)
    worker.view = replace(worker.view, connection=ConnectionState.DISCONNECTED)
    assert (
        samples(metrics)["tuya_mqtt_controller_connected", (("controller", "bed"),)]
        == 0
    )


def test_fault_code_metric_is_absent_until_reported(
    rig: tuple[Metrics, Worker],
) -> None:
    metrics, worker = rig
    key = ("tuya_mqtt_controller_fault_code", (("controller", "bed"),))
    assert key not in samples(metrics)
    worker.view = replace(
        worker.view, zones=(ZoneState(fault_code=0), ZoneState(fault_code=0))
    )
    assert samples(metrics)[key] == 0
    worker.view = replace(
        worker.view, zones=(ZoneState(fault_code=12), ZoneState(fault_code=12))
    )
    assert samples(metrics)[key] == 12


def test_ages_and_nested_publication_failures() -> None:
    assert age(10, None) is None
    assert age(10, 11) == 0
    error = ExceptionGroup(
        "network",
        [
            MQTTFailureError("publish", Operation.PUBLISH),
            ExceptionGroup(
                "cleanup", [MQTTFailureError("disconnect", Operation.DISCONNECT)]
            ),
            ValueError("unexpected"),
        ],
    )
    assert publication_failures(error) == 1
