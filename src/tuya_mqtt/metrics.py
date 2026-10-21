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

lazy from collections.abc import Iterator

from prometheus_client import (
    CollectorRegistry,
    GCCollector,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    SummaryMetricFamily,
)

from .bridge import online
from .domain import ConnectionState
from .log import sanitize
lazy from .bridge import Bridge

PREFIX = "tuya_mqtt"


def age(now: float, timestamp: float | None) -> float | None:
    return None if timestamp is None else max(0, now - timestamp)


class Metrics:
    def __init__(self, bridge: Bridge, secrets: tuple[str, ...]) -> None:
        self.bridge = bridge
        self._secrets = secrets
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        self.registry.register(self)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def collect(
        self,
    ) -> Iterator[GaugeMetricFamily | CounterMetricFamily | SummaryMetricFamily]:
        bridge = self.bridge
        now = bridge.now
        for name, description, value in (
            ("mqtt_connected", "MQTT connection availability.", int(bridge.connected)),
            (
                "healthy",
                "Required coordination loops are running.",
                int(bridge.healthy),
            ),
            (
                "event_loop_progress_age_seconds",
                "Age of coordinating loop progress.",
                age(now, bridge.progress_at),
            ),
            (
                "heartbeat_age_seconds",
                "Age of the last bridge heartbeat.",
                age(now, bridge.heartbeat_at),
            ),
        ):
            yield GaugeMetricFamily(
                f"{PREFIX}_{name}",
                description,
                value=float("inf") if value is None else value,
            )
        for name, description, value in (
            ("mqtt_reconnects", "MQTT reconnection attempts.", bridge.mqtt_reconnects),
            ("mqtt_failures", "MQTT connection failures.", bridge.mqtt_failures),
            (
                "mqtt_publish_failures",
                "Failed MQTT publications.",
                bridge.mqtt_publish_failures,
            ),
        ):
            yield CounterMetricFamily(f"{PREFIX}_{name}", description, value=value)
        gauges = {
            name: GaugeMetricFamily(
                f"{PREFIX}_{name}", description, labels=["controller"]
            )
            for name, description in (
                (
                    "controller_connected",
                    "Controller socket is synchronized or synchronizing.",
                ),
                (
                    "controller_available",
                    "Controller is available in the current MQTT session.",
                ),
                (
                    "controller_last_full_status_age_seconds",
                    "Age of the last full controller status.",
                ),
                (
                    "controller_progress_age_seconds",
                    "Age of controller worker progress.",
                ),
                ("controller_queue_depth", "Queued controller commands."),
            )
        }
        counters = {
            name: CounterMetricFamily(
                f"{PREFIX}_{name}", description, labels=["controller"]
            )
            for name, description in (
                ("controller_reconnects", "Controller reconnection attempts."),
                ("controller_operation_failures", "Controller operation failures."),
            )
        }
        faults = GaugeMetricFamily(
            f"{PREFIX}_controller_fault_code",
            "Reported controller fault bitmap; absent until reported.",
            labels=["controller"],
        )
        errors = GaugeMetricFamily(
            f"{PREFIX}_controller_error_info",
            "Last controller error code.",
            labels=["controller", "error_code"],
        )
        durations = SummaryMetricFamily(
            f"{PREFIX}_command_duration_seconds",
            "Terminal command latency from MQTT arrival.",
            labels=["controller", "zone"],
        )
        outcomes = CounterMetricFamily(
            f"{PREFIX}_command_outcomes",
            "Terminal command outcomes.",
            labels=["controller", "zone", "outcome", "error_code"],
        )
        labels: list[tuple[str, tuple[str, ...]]] = []
        for index, worker in enumerate(bridge.workers):
            config = bridge.config.controllers[index]
            name = str(sanitize(config.name, self._secrets))
            zone_names = tuple(
                str(sanitize(config.zone_name(endpoint) or "", self._secrets))
                for endpoint in range(bridge.profiles[index].endpoint_count)
            )
            labels.append((name, zone_names))
            view = worker.snapshot
            values = (
                int(
                    view.connection
                    in (ConnectionState.SYNCHRONIZING, ConnectionState.ONLINE)
                ),
                int(bridge.connected and online(view, bridge.session_id, now)),
                age(now, view.last_full_status),
                age(now, view.progress_at),
                worker.queue_depth,
            )
            for metric, value in zip(gauges.values(), values, strict=True):
                metric.add_metric([name], float("inf") if value is None else value)
            for metric, value in zip(
                counters.values(),
                (view.reconnects, view.operation_failures),
                strict=True,
            ):
                metric.add_metric([name], value)
            if (fault := view.endpoints[0].fault_code) is not None:
                faults.add_metric([name], fault)
            errors.add_metric(
                [name, view.last_error.value if view.last_error else "none"], 1
            )
            for endpoint, zone_name in enumerate(zone_names):
                count, total = bridge.latency_totals.get((index, endpoint), (0, 0.0))
                durations.add_metric([name, zone_name], count, total)
        for (index, endpoint, outcome, error), count in bridge.outcomes.items():
            name, zone_names = labels[index]
            outcomes.add_metric(
                [
                    name,
                    zone_names[endpoint],
                    outcome.value,
                    error.value if error else "none",
                ],
                count,
            )
        yield from gauges.values()
        yield from counters.values()
        yield faults
        yield errors
        yield durations
        yield outcomes
