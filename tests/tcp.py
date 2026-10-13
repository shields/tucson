import socket
import threading
from dataclasses import dataclass, field
from typing import Any
lazy from collections.abc import Iterator

import pytest
from tuyamock import MockDevice, Session

from .controller_fakes import BASELINE

KEY = "test-key-16-byte"


@dataclass
class Simulator:
    mock: MockDevice
    writes: list[dict[str, object]] = field(default_factory=list)
    written: threading.Event = field(default_factory=threading.Event)
    mode: str = "normal"


@pytest.fixture
def simulator(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Simulator]:
    mock = MockDevice(local_key=KEY, version=request.param, dps=BASELINE.copy())
    simulation = Simulator(mock)
    original = Session._handle_control

    def handle(session: Session, message: Any) -> bytes | None:
        values = {
            key: value
            for key, value in session._parse_dps(message).items()
            if value is not None
        }
        response = original(session, message)
        if values:
            simulation.writes.append(values)
            if simulation.mode == "close":
                mock.server._client.shutdown(socket.SHUT_RDWR)
                response = None
            elif simulation.mode == "empty_ack":
                response = session.profile.pack_response(session, message.cmd, b"")
            elif simulation.mode == "drop_reply":
                response = None
            simulation.written.set()
        return response

    monkeypatch.setattr(Session, "_handle_control", handle)
    mock.start()
    thread = mock._thread
    try:
        yield simulation
    finally:
        mock.stop()
        assert not thread.is_alive()
