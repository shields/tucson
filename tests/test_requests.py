import pytest

from tuya_mqtt.domain import CommandResult, ErrorCode, Outcome
from tuya_mqtt.requests import REQUEST_LIMIT, Admission, RequestLedger

from .controller_fakes import Clock


def test_pending_duplicate_content_and_terminal_replay() -> None:
    clock = Clock()
    ledger = RequestLedger(clock=clock)
    assert ledger.begin("r", b"same").admission is Admission.NEW
    assert ledger.begin("r", b"same").admission is Admission.PENDING
    conflict = ledger.begin("r", b"different")
    assert conflict.admission is Admission.REJECTED
    assert conflict.result.error_code is ErrorCode.DUPLICATE_ID
    result = CommandResult("r", Outcome.CONFIRMED)
    ledger.finish(result)
    replay = ledger.begin("r", b"same")
    assert replay.admission is Admission.REPLAY
    assert replay.result == result
    assert ledger.begin("r", b"different").result.error_code is ErrorCode.DUPLICATE_ID
    with pytest.raises(RuntimeError, match="already completed"):
        ledger.finish(result)


def test_completed_results_expire_pending_entries_do_not() -> None:
    clock = Clock()
    ledger = RequestLedger(clock=clock)
    ledger.begin("completed", b"a")
    ledger.finish(
        CommandResult("completed", Outcome.UNCONFIRMED, ErrorCode.CONNECTION_LOST)
    )
    ledger.begin("pending", b"b")
    clock.advance(59.999)
    assert ledger.begin("completed", b"a").admission is Admission.REPLAY
    clock.advance(0.001)
    assert ledger.begin("completed", b"new").admission is Admission.NEW
    assert ledger.begin("pending", b"b").admission is Admission.PENDING


def test_capacity_never_evicts_pending_or_unexpired_results() -> None:
    clock = Clock()
    ledger = RequestLedger(clock=clock)
    for identifier in range(REQUEST_LIMIT):
        assert ledger.begin(str(identifier), b"a").admission is Admission.NEW
    rejected = ledger.begin("overflow", b"a")
    assert rejected.admission is Admission.REJECTED
    assert rejected.result.error_code is ErrorCode.QUEUE_FULL
    ledger.finish(CommandResult("0", Outcome.REJECTED, ErrorCode.UNAVAILABLE))
    assert ledger.begin("overflow", b"a").admission is Admission.REJECTED
    clock.advance(60)
    assert ledger.begin("overflow", b"a").admission is Admission.NEW
    assert ledger.begin("1", b"a").admission is Admission.PENDING
