from dataclasses import dataclass, replace
lazy from collections.abc import Callable

import pytest

from tuya_mqtt.controller import (
    CONFIRMATION_TIMEOUT,
    ControllerCore,
    Mailbox,
)
from tuya_mqtt.domain import (
    BridgeError,
    Command,
    ConnectionState,
    ErrorCode,
    Field,
    Outcome,
    Request,
)
from tuya_mqtt.profiles.sunbeam import parse_command
lazy from tuya_mqtt.config import ControllerConfig
lazy from tuya_mqtt.domain import Completion, ControllerSnapshot

from .controller_fakes import BASELINE, Clock, Device, Log


@dataclass
class Harness:
    core: ControllerCore
    mailbox: Mailbox
    device: Device
    clock: Clock
    log: Log

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.core.tick()

    def push(self, dps: dict[str, object] | Exception) -> None:
        self.device.pushes.append(dps)
        self.tick()

    def online(self) -> None:
        self.mailbox.set_session("session")
        self.tick(3)
        assert self.core.view.online

    def request(self, command: Command, endpoint: int = 0) -> Request:
        now = self.clock()
        return Request(
            endpoint,
            command,
            "session",
            self.core.view.generation,
            now,
            now + 2,
            now + CONFIRMATION_TIMEOUT,
        )

    def admit(self, payload: bytes, endpoint: int = 0) -> Request:
        request = self.request(parse_command(payload), endpoint)
        assert self.mailbox.admit(request, writes_enabled=True) is None
        return request


@pytest.fixture
def harness_factory(controller_config: ControllerConfig) -> Callable[..., Harness]:
    def create(*, mode: str = "multi_dp", jitter: float = 0) -> Harness:
        clock = Clock()
        device = Device(clock)
        mailbox = Mailbox()
        logger = Log()
        config = controller_config.model_copy(update={"write_mode": mode})
        core = ControllerCore(
            config,
            lambda: device,
            mailbox,
            clock=clock,
            jitter=lambda: jitter,
            logger=logger,
        )
        return Harness(core, mailbox, device, clock, logger)

    return create


@pytest.fixture
def harness(harness_factory: Callable[..., Harness]) -> Harness:
    return harness_factory()


def test_full_baseline_and_periodic_maintenance(harness: Harness) -> None:
    assert harness.core.view.connection is ConnectionState.DISCONNECTED
    harness.online()
    assert harness.core.view.generation == 1
    assert harness.core.view.last_full_status == 0
    assert harness.core.view.endpoints[0].power is False
    before = len(harness.device.calls)
    harness.tick()
    assert harness.device.calls[before][0] == "receive"
    assert harness.core.view.online
    harness.clock.advance(10)
    harness.tick()
    assert harness.device.calls[-1][0] == "heartbeat"
    harness.clock.advance(20)
    harness.tick(2)
    assert harness.device.calls[-1][0] == "status"
    assert harness.core.view.last_full_status == harness.clock()
    assert harness.core.view.online
    assert harness.mailbox.snapshot == harness.core.view


def test_partial_baseline_merges_and_unknown_countdowns(harness: Harness) -> None:
    harness.device.connections.append({"14": False, "20": "level_4"})
    harness.tick()
    assert harness.core.view.connection is ConnectionState.SYNCHRONIZING
    harness.push({"24": False, "15": False, "21": "level_8", "25": True})
    assert harness.core.view.online
    assert harness.core.view.last_full_status is None
    assert harness.core.view.endpoints[1].heat_level == 8
    assert harness.core.view.endpoints[0].remaining_seconds is None
    assert harness.core.view.endpoints[1].preheat_remaining_seconds is None


def test_physical_pushes_do_not_invent_countdowns(harness: Harness) -> None:
    harness.online()
    harness.push({"14": True, "20": "level_2", "28": 1700, "999": "unrelated"})
    state = harness.core.view.endpoints[0]
    assert state.power is True
    assert state.heat_level == 2
    assert state.remaining_seconds == 1700
    harness.clock.advance(5)
    harness.tick()
    assert harness.core.view.endpoints[0].remaining_seconds == 1700
    assert harness.core.view.last_full_status == 0


@pytest.mark.parametrize(
    ("dp", "value"), [("14", "true"), ("20", "unknown"), ("25", 1)]
)
def test_invalid_required_push_takes_controller_offline(
    dp: str, value: object, harness: Harness
) -> None:
    harness.online()
    harness.push({dp: value})
    assert harness.core.view.connection is ConnectionState.DISCONNECTED
    assert harness.core.view.last_error is ErrorCode.COMPATIBILITY
    assert harness.device.calls[-1][0] == "close"
    assert harness.log.events[-1][0] == "error"


def test_optional_compatibility_error_blocks_dependent_command(
    harness: Harness,
) -> None:
    harness.online()
    harness.push({"26": "unexpected"})
    assert harness.core.view.online
    assert harness.core.view.last_error is ErrorCode.COMPATIBILITY
    assert harness.core.view.endpoints[0].auto_off_seconds is None
    harness.admit(b'{"auto_off_seconds":3600}')
    harness.tick()
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.REJECTED
    assert result.error_code is ErrorCode.COMPATIBILITY
    assert not harness.device.writes
    harness.push({"26": "1h"})
    assert harness.core.view.last_error is None


def fault_events(harness: Harness) -> list[tuple[str, str, dict[str, object]]]:
    return [event for event in harness.log.events if "fault" in event[1]]


def fault_log(harness: Harness) -> list[tuple[str, str, object, object]]:
    return [
        (level, event, fields["previous_fault_code"], fields["fault_code"])
        for level, event, fields in fault_events(harness)
    ]


def test_fault_code_is_reported_and_logged_when_it_changes(harness: Harness) -> None:
    harness.online()
    assert [state.fault_code for state in harness.core.view.endpoints] == [0, 0]
    assert not fault_events(harness)
    logged = len(harness.log.events)
    harness.push({"13": 4})
    assert harness.core.view.online
    assert [state.fault_code for state in harness.core.view.endpoints] == [4, 4]
    harness.push({"13": 4, "14": True})
    harness.push({"14": False})
    harness.push({"13": 6})
    harness.push({"13": 0})
    assert harness.log.events[logged:] == [
        (
            "error",
            "controller_fault",
            {
                "operation": "report",
                "fault_code": 4,
                "previous_fault_code": 0,
                "generation": 1,
            },
        ),
        (
            "error",
            "controller_fault",
            {
                "operation": "report",
                "fault_code": 6,
                "previous_fault_code": 4,
                "generation": 1,
            },
        ),
        (
            "info",
            "controller_fault_cleared",
            {
                "operation": "report",
                "fault_code": 0,
                "previous_fault_code": 6,
                "generation": 1,
            },
        ),
    ]
    assert harness.core.view.online
    assert harness.core.view.last_error is None


def test_pushed_fault_survives_a_status_refresh(harness: Harness) -> None:
    harness.online()
    harness.push({"13": 4})
    harness.mailbox.refresh()
    harness.tick()
    assert harness.device.calls[-1][0] == "status"
    assert [state.fault_code for state in harness.core.view.endpoints] == [4, 4]
    assert fault_log(harness) == [("error", "controller_fault", 0, 4)]


def test_fault_present_at_connection_is_logged(harness: Harness) -> None:
    harness.device.state["13"] = 2
    harness.online()
    assert [state.fault_code for state in harness.core.view.endpoints] == [2, 2]
    assert harness.core.view.online
    assert fault_log(harness) == [("error", "controller_fault", None, 2)]


@pytest.mark.parametrize("invalid", [-1, 2**53, 10**400])
def test_invalid_fault_code_is_a_visible_optional_compatibility_error(
    harness: Harness, invalid: int
) -> None:
    harness.online()
    harness.push({"13": invalid})
    assert harness.core.view.online
    assert harness.core.view.last_error is ErrorCode.COMPATIBILITY
    assert harness.core.view.invalid_fields == (frozenset({Field.FAULT_CODE}),) * 2
    assert [state.fault_code for state in harness.core.view.endpoints] == [None, None]
    assert not fault_events(harness)
    harness.push({"13": 0})
    assert harness.core.view.last_error is None
    assert [state.fault_code for state in harness.core.view.endpoints] == [0, 0]
    assert not fault_events(harness)


def reconnect_with_fault(harness: Harness, fault: int) -> None:
    harness.push(BridgeError(ErrorCode.CONNECTION_LOST, "lost"))
    assert not harness.core.view.online
    harness.device.state["13"] = fault
    harness.clock.advance(1)
    harness.tick(3)
    assert harness.core.view.online


def test_unchanged_fault_is_not_logged_again_after_reconnect_or_session_change(
    harness: Harness,
) -> None:
    harness.device.state["13"] = 4
    harness.online()
    reconnect_with_fault(harness, 4)
    harness.mailbox.set_session("new-session")
    harness.tick(3)
    assert harness.core.view.online
    harness.mailbox.set_session(None)
    harness.tick(2)
    harness.mailbox.set_session("another-session")
    harness.tick(3)
    assert harness.core.view.online
    assert harness.core.view.generation >= 3
    assert fault_log(harness) == [("error", "controller_fault", None, 4)]


def test_fault_changes_and_clears_are_logged_across_disconnects(
    harness: Harness,
) -> None:
    harness.device.state["13"] = 4
    harness.online()
    reconnect_with_fault(harness, 6)
    reconnect_with_fault(harness, 0)
    reconnect_with_fault(harness, 0)
    assert fault_log(harness) == [
        ("error", "controller_fault", None, 4),
        ("error", "controller_fault", 4, 6),
        ("info", "controller_fault_cleared", 6, 0),
    ]


def test_invalid_fault_is_not_a_clear_and_does_not_repeat_the_fault(
    harness: Harness,
) -> None:
    harness.online()
    harness.push({"13": 4})
    harness.push({"13": -1})
    harness.push({"13": 4})
    harness.push({"13": -1})
    harness.push({"13": 0})
    assert fault_log(harness) == [
        ("error", "controller_fault", 0, 4),
        ("info", "controller_fault_cleared", 4, 0),
    ]


def test_full_status_does_not_require_the_fault_dp(harness: Harness) -> None:
    del harness.device.state["13"]
    harness.online()
    assert harness.core.view.last_full_status == 0
    assert [state.fault_code for state in harness.core.view.endpoints] == [None, None]
    assert not fault_events(harness)
    harness.clock.advance(30)
    harness.tick(2)
    assert harness.core.view.last_full_status >= 30


@pytest.mark.parametrize("fault", [1, 4, 8191])
def test_fault_does_not_change_availability_or_command_admission(
    fault: int, harness: Harness
) -> None:
    harness.device.state["13"] = fault
    harness.online()
    harness.admit(b'{"power":true,"heat_level":6}')
    harness.tick(8)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert harness.core.view.online
    assert harness.core.view.last_error is None
    assert harness.core.view.endpoints[0].heat_level == 6
    assert harness.core.view.endpoints[0].fault_code == fault


def test_reconnect_never_merges_previous_baseline(harness: Harness) -> None:
    harness.online()
    harness.push(BridgeError(ErrorCode.CONNECTION_LOST, "lost"))
    harness.device.connections.append({"14": True})
    harness.clock.advance(1)
    harness.tick()
    assert harness.core.view.generation == 2
    assert harness.core.view.reconnects == 1
    assert harness.core.view.previous_endpoints[0].heat_level == 4
    assert harness.core.view.previous_full_status_at is not None
    assert harness.core.view.endpoints[0].heat_level is None
    assert not harness.core.view.online
    assert harness.core.view.last_full_status is None
    assert harness.core.view.last_error is ErrorCode.CONNECTION_LOST


def test_broker_session_change_requires_new_full_read(harness: Harness) -> None:
    harness.online()
    harness.mailbox.set_session("new-session")
    harness.device.connections.append({"14": True})
    harness.tick()
    assert harness.core.view.session_id == "new-session"
    assert harness.core.view.generation == 2
    assert not harness.core.view.online
    assert harness.core.view.endpoints[0].heat_level is None
    assert harness.core.view.previous_endpoints[0].heat_level == 4
    harness.mailbox.refresh()
    harness.tick()
    assert harness.core.view.online


def test_explicit_refresh_republishes_unchanged_state(harness: Harness) -> None:
    harness.online()
    before = harness.core.view.endpoints
    revision = harness.core.view.refresh_revision
    harness.mailbox.refresh()
    harness.mailbox.refresh()
    harness.tick()
    assert harness.core.view.endpoints == before
    assert harness.core.view.refresh_revision == revision + 1
    assert harness.device.calls[-1][0] == "status"


@pytest.mark.parametrize("jitter", [0, 1])
def test_backoff_is_capped_and_resets_only_after_synchronization(
    jitter: float, harness_factory: Callable[..., Harness]
) -> None:
    harness = harness_factory(jitter=jitter)
    for base in [1, 2, 4, 8, 16, 32, 60, 60]:
        harness.device.connections.append(
            BridgeError(ErrorCode.CONNECTION_LOST, "lost")
        )
        harness.tick()
        expected = min(60, base * (1 + 0.25 * jitter))
        assert harness.log.events[-1][2]["retry_delay"] == expected
        assert 0 <= harness.core.delay() <= 0.25
        calls = len(harness.device.calls)
        harness.tick()
        assert len(harness.device.calls) == calls
        harness.clock.advance(expected)
    harness.device.connections.append({"14": True})
    harness.tick()
    assert not harness.core.view.online
    harness.push(BridgeError(ErrorCode.CONNECTION_LOST, "lost"))
    assert harness.log.events[-1][2]["retry_delay"] == 60
    harness.clock.advance(60)
    harness.tick()
    assert harness.core.view.online
    harness.push(BridgeError(ErrorCode.CONNECTION_LOST, "lost"))
    assert harness.log.events[-1][2]["retry_delay"] == 1 * (1 + 0.25 * jitter)


@pytest.mark.parametrize("partial", [False, True])
def test_stale_required_state_reconnects(harness: Harness, *, partial: bool) -> None:
    if partial:
        harness.device.connections.append({"14": True})
    harness.tick()
    harness.clock.advance(90)
    harness.tick()
    assert harness.core.view.connection is ConnectionState.DISCONNECTED
    assert harness.core.view.last_error is ErrorCode.STALE


def test_partial_poll_does_not_reset_full_status_age(harness: Harness) -> None:
    harness.online()
    harness.device.statuses.append({"14": True})
    harness.clock.advance(30)
    harness.tick(2)
    assert harness.core.view.last_full_status == 0
    assert harness.core.view.last_traffic == harness.clock()
    harness.clock.advance(60)
    harness.tick()
    assert harness.core.view.last_error is ErrorCode.STALE


@pytest.mark.parametrize("operation", ["status", "heartbeat"])
def test_failed_maintenance_invalidates_pending_work(
    operation: str, harness: Harness
) -> None:
    harness.online()
    harness.admit(b'{"power":true}')
    if operation == "status":
        harness.device.statuses.append(BridgeError(ErrorCode.DEVICE_ERROR, "failed"))
        harness.mailbox.refresh()
    else:
        harness.device.heartbeat_error = BridgeError(ErrorCode.DEVICE_ERROR, "failed")
        harness.clock.advance(10)
    harness.tick()
    assert not harness.core.view.online
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.REJECTED
    assert not harness.device.writes


@pytest.mark.parametrize("zone", [0, 1])
def test_serialized_command_confirmation(zone: int, harness: Harness) -> None:
    harness.online()
    request = harness.admit(b'{"power":true,"heat_level":6}', zone)
    for _ in range(8):
        harness.tick()
    result = harness.mailbox.drain_results()[0]
    assert result.request == request
    assert result.result.outcome is Outcome.CONFIRMED
    power_dp, level_dp = ("14", "20") if zone == 0 else ("15", "21")
    assert harness.device.writes == [{power_dp: True, level_dp: "level_6"}]
    assert harness.core.view.endpoints[zone].heat_level == 6
    assert harness.core.view.active is None


@pytest.mark.parametrize("echo", [False, True])
def test_completion_is_queued_after_the_state_that_confirms_it(
    harness: Harness, *, echo: bool
) -> None:
    published: list[ControllerSnapshot] = []

    class Recording(Mailbox):
        def complete(self, completion: Completion) -> None:
            published.append(self.snapshot)
            super().complete(completion)

    mailbox = Recording()
    core = ControllerCore(
        harness.core.config,
        lambda: harness.device,
        mailbox,
        clock=harness.clock,
        logger=harness.log,
    )
    harness = Harness(core, mailbox, harness.device, harness.clock, harness.log)
    harness.online()
    harness.device.echo_writes = echo
    request = harness.admit(b'{"power":true,"heat_level":6}')
    harness.tick(8)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert [request.command.matches(view.endpoints[0]) for view in published] == [True]


@pytest.mark.parametrize("zone", [0, 1])
def test_level_for_a_side_that_is_off_is_sent_with_power_on(
    zone: int, harness: Harness
) -> None:
    harness.online()
    harness.admit(b'{"heat_level":6}', zone)
    harness.tick(8)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    power_dp, level_dp = ("14", "20") if zone == 0 else ("15", "21")
    assert harness.device.writes == [{power_dp: True, level_dp: "level_6"}]
    assert list(harness.device.writes[0]) == [power_dp, level_dp]
    assert harness.core.view.endpoints[zone].power is True


def test_the_packet_is_logged_before_it_is_written(harness: Harness) -> None:
    harness.online()
    before_write: list[list[str]] = []
    harness.device.on_write = lambda: before_write.append(
        [event[1] for event in harness.log.events]
    )
    harness.admit(b'{"request_id":"r","heat_level":6}')
    harness.tick(8)
    assert len(before_write) == 1
    assert "command_write" in before_write[0]
    assert (
        "info",
        "command_write",
        {
            "zone": "side_a",
            "operation": "command",
            "request_id": "r",
            "dps": {"14": True, "20": "level_6"},
        },
    ) in harness.log.events


def test_level_for_a_side_that_is_off_is_confirmed_only_once_power_is_reported(
    harness: Harness,
) -> None:
    harness.online()
    harness.device.apply_writes = False
    harness.device.pushes.append({"20": "level_6"})
    harness.admit(b'{"heat_level":6}')
    harness.tick(4)
    assert harness.device.writes == [{"14": True, "20": "level_6"}]
    assert not harness.mailbox.drain_results()
    harness.push({"14": True})
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED


def test_level_for_a_side_that_is_on_does_not_write_power(harness: Harness) -> None:
    harness.device.state["14"] = True
    harness.online()
    harness.admit(b'{"heat_level":6}')
    harness.tick(8)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert harness.device.writes == [{"20": "level_6"}]


def test_level_and_timer_for_a_side_that_is_off_share_one_packet(
    harness: Harness,
) -> None:
    harness.online()
    harness.admit(b'{"heat_level":3,"auto_off_seconds":5400}')
    harness.tick(8)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert harness.device.writes == [{"14": True, "20": "level_3", "26": "1h30m"}]
    assert list(harness.device.writes[0]) == ["14", "20", "26"]


@pytest.mark.parametrize("zone", [0, 1])
def test_timer_alone_for_a_side_that_is_off_is_rejected(
    zone: int, harness: Harness
) -> None:
    harness.online()
    harness.admit(b'{"auto_off_seconds":5400}', zone)
    harness.tick(3)
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.REJECTED
    assert result.error_code is ErrorCode.ZONE_OFF
    assert not harness.device.writes


@pytest.mark.parametrize(
    ("zone", "dp", "enum"), [(0, "26", "7h30"), (1, "27", "7h30m")]
)
def test_timer_for_a_side_that_is_on_uses_that_sides_spelling(
    zone: int, dp: str, enum: str, harness: Harness
) -> None:
    harness.device.state["14" if zone == 0 else "15"] = True
    harness.online()
    harness.admit(b'{"auto_off_seconds":27000}', zone)
    harness.tick(8)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert harness.device.writes == [{dp: enum}]


def test_preheat_for_a_side_that_is_off_is_written_as_requested(
    harness: Harness,
) -> None:
    harness.online()
    harness.admit(b'{"preheat":true}')
    harness.tick(8)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert harness.device.writes == [{"24": True}]


def test_fresh_push_confirms_without_polling(harness: Harness) -> None:
    harness.online()
    harness.device.echo_writes = True
    calls = len(harness.device.calls)
    harness.admit(b'{"power":true}')
    harness.tick(3)
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED
    assert "status" not in [
        operation for operation, _payload in harness.device.calls[calls:]
    ]


def test_cached_match_and_empty_ack_are_not_confirmation(harness: Harness) -> None:
    harness.online()
    harness.device.apply_writes = False
    harness.device.statuses.extend([{"14": True}] * 10)
    harness.admit(b'{"power":false}')
    harness.tick(5)
    assert not harness.mailbox.drain_results()
    harness.clock.advance(8)
    harness.tick()
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.UNCONFIRMED
    assert result.error_code is ErrorCode.CONFIRMATION_TIMEOUT


def test_delayed_matching_push_confirms_within_deadline(harness: Harness) -> None:
    harness.online()
    harness.device.apply_writes = False
    harness.admit(b'{"power":true}')
    harness.tick(3)
    assert not harness.mailbox.drain_results()
    harness.push({"14": True})
    assert harness.mailbox.drain_results()[0].result.outcome is Outcome.CONFIRMED


def test_queue_expiry_prevents_write(harness: Harness) -> None:
    harness.online()
    request = harness.admit(b'{"power":true}')
    harness.clock.advance(2)
    harness.tick()
    result = harness.mailbox.drain_results()[0].result
    assert result.error_code is ErrorCode.EXPIRED
    assert result.outcome is Outcome.REJECTED
    assert not harness.device.writes
    assert request.received_at + 2 == harness.clock()


@pytest.mark.parametrize("kind", ["deadline", "generation"])
def test_execution_rechecks_request_metadata(kind: str, harness: Harness) -> None:
    harness.online()
    request = harness.request(parse_command(b'{"power":true}'))
    request = (
        replace(request, deadline=0)
        if kind == "deadline"
        else replace(request, generation=request.generation - 1)
    )
    assert harness.mailbox.admit(request, writes_enabled=True) is None
    harness.tick()
    expected = ErrorCode.EXPIRED if kind == "deadline" else ErrorCode.UNAVAILABLE
    assert harness.mailbox.drain_results()[0].result.error_code is expected
    assert not harness.device.writes


def test_inflight_session_loss_leaves_the_write_unconfirmed(harness: Harness) -> None:
    harness.online()
    harness.admit(b'{"power":true,"heat_level":6}')
    harness.tick()
    harness.mailbox.set_session("new-session")
    harness.tick()
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.UNCONFIRMED
    assert harness.device.writes == [{"14": True, "20": "level_6"}]
    assert harness.core.view.endpoints[0].heat_level == 6
    assert harness.core.view.endpoints[0].power is True


def test_write_failure_is_unconfirmed_without_rollback_or_replay(
    harness: Harness,
) -> None:
    harness.online()
    harness.admit(b'{"power":true,"heat_level":6}')
    harness.device.write_error = BridgeError(ErrorCode.CONNECTION_LOST, "lost")
    harness.tick()
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.UNCONFIRMED
    assert harness.device.writes == [{"14": True, "20": "level_6"}]
    harness.device.write_error = None
    harness.clock.advance(1)
    harness.tick()
    assert harness.core.view.endpoints[0].power is True
    assert len(harness.device.writes) == 1


def test_late_matching_report_does_not_change_terminal_result(harness: Harness) -> None:
    harness.online()
    harness.device.apply_writes = False
    harness.admit(b'{"power":true}')
    harness.tick()
    harness.clock.advance(8)
    harness.tick(2)
    result = harness.mailbox.drain_results()[0]
    assert result.result.outcome is Outcome.UNCONFIRMED
    harness.device.state["14"] = True
    harness.push({"14": True})
    assert harness.core.view.endpoints[0].power is True
    assert harness.core.view.last_completion == result
    assert not harness.mailbox.drain_results()


def test_report_after_confirmation_deadline_is_unconfirmed(harness: Harness) -> None:
    harness.online()
    harness.admit(b'{"power":true}')
    harness.tick(2)
    harness.device.status_duration = 9
    harness.tick()
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.UNCONFIRMED
    assert result.error_code is ErrorCode.CONFIRMATION_TIMEOUT
    assert harness.core.view.endpoints[0].power is True


def test_queue_and_completion_backpressure_do_not_block_maintenance(
    harness: Harness,
) -> None:
    harness.online()
    first = harness.admit(b'{"power":true}')
    harness.tick()
    for _ in range(16):
        harness.admit(b'{"power":false}')
    assert harness.mailbox.queue_depth == 16
    assert harness.mailbox.admit(first, writes_enabled=True) is ErrorCode.QUEUE_FULL
    harness.tick(2)
    assert harness.mailbox.queue_depth == 16
    harness.tick()
    assert harness.mailbox.queue_depth == 15
    assert harness.mailbox.admit(first, writes_enabled=True) is ErrorCode.QUEUE_FULL
    harness.clock.advance(10)
    harness.tick()
    assert harness.device.calls[-1][0] == "heartbeat"
    harness.mailbox.drain_results()
    assert (
        harness.mailbox.admit(
            harness.request(parse_command(b'{"power":false}')), writes_enabled=True
        )
        is None
    )


def test_stop_invalidates_queued_and_inflight_commands(harness: Harness) -> None:
    harness.online()
    harness.admit(b'{"power":true}')
    harness.admit(b'{"power":false}', 1)
    harness.tick()
    harness.core.stop()
    assert harness.core.view.connection is ConnectionState.STOPPING
    results = harness.mailbox.drain_results()
    assert [completion.result.outcome for completion in results] == [
        Outcome.UNCONFIRMED,
        Outcome.REJECTED,
    ]
    assert harness.device.calls[-1][0] == "close"


@pytest.mark.parametrize("mode", ["disabled", "multi_dp"])
def test_full_validation_before_any_write(
    mode: str, harness_factory: Callable[..., Harness]
) -> None:
    harness = harness_factory(mode=mode)
    harness.online()
    request = harness.request(
        Command("bad", ((Field.POWER, True), (Field.HEAT_LEVEL, 11)))
    )
    assert harness.mailbox.admit(request, writes_enabled=True) is None
    harness.tick()
    expected = (
        ErrorCode.WRITE_DISABLED if mode == "disabled" else ErrorCode.INVALID_COMMAND
    )
    assert harness.mailbox.drain_results()[0].result.error_code is expected
    assert not harness.device.writes


def test_refresh_flood_cannot_starve_heartbeats(harness: Harness) -> None:
    harness.online()
    for _ in range(60):
        harness.clock.advance(0.25)
        harness.mailbox.refresh()
        harness.tick()
    assert "heartbeat" in [operation for operation, _payload in harness.device.calls]
    assert harness.core.view.refresh_revision >= 59


def test_complete_push_refreshes_required_state_age(harness: Harness) -> None:
    harness.online()
    harness.clock.advance(5)
    harness.push(BASELINE.copy())
    assert harness.core.view.last_full_status == harness.clock()


def test_shutdown_does_not_begin_a_connection(harness: Harness) -> None:
    harness.mailbox.stop.set()
    harness.tick()
    assert harness.core.view.connection is ConnectionState.STOPPING
    assert harness.device.calls == []


def test_disconnect_after_admission_before_first_write(harness: Harness) -> None:
    class DisconnectAtStart(Mailbox):
        def publish(self, view: ControllerSnapshot) -> None:
            super().publish(view)
            if view.active is not None:
                self.set_session("new-session")

    mailbox = DisconnectAtStart()
    core = ControllerCore(
        harness.core.config,
        lambda: harness.device,
        mailbox,
        clock=harness.clock,
        logger=harness.log,
    )
    harness = Harness(core, mailbox, harness.device, harness.clock, harness.log)
    harness.online()
    harness.admit(b'{"power":true}')
    harness.tick()
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.REJECTED
    assert result.error_code is ErrorCode.UNAVAILABLE
    assert harness.device.writes == []


def test_deadline_during_the_write_is_unconfirmed_and_reads_back(
    harness: Harness,
) -> None:
    harness.online()
    harness.device.on_write = lambda: harness.clock.advance(8)
    harness.admit(b'{"power":true,"heat_level":6}')
    harness.tick(3)
    result = harness.mailbox.drain_results()[0].result
    assert result.outcome is Outcome.UNCONFIRMED
    assert result.error_code is ErrorCode.CONFIRMATION_TIMEOUT
    assert harness.device.writes == [{"14": True, "20": "level_6"}]
    harness.tick()
    assert harness.device.calls[-1][0] == "status"
    assert harness.core.view.endpoints[0].heat_level == 6
    assert harness.core.view.endpoints[0].power is True
