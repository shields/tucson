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
                str(sanitize(zone, self._secrets)) for zone in config.zones
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
            errors.add_metric(
                [name, view.last_error.value if view.last_error else "none"], 1
            )
            for zone, zone_name in enumerate(zone_names):
                count, total = bridge.latency_totals.get((index, zone), (0, 0.0))
                durations.add_metric([name, zone_name], count, total)
        for (index, zone, outcome, error), count in bridge.outcomes.items():
            name, zone_names = labels[index]
            outcomes.add_metric(
                [
                    name,
                    zone_names[zone],
                    outcome.value,
                    error.value if error else "none",
                ],
                count,
            )
        yield from gauges.values()
        yield from counters.values()
        yield errors
        yield durations
        yield outcomes
