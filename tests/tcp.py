import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
lazy from collections.abc import Iterator

import pytest
import tinytuya
from tuyamock import MockDevice, Session

from .controller_fakes import BASELINE

KEY = "test-key-16-byte"


@dataclass
class Simulator:
    mock: MockDevice
    writes: list[dict[str, object]] = field(default_factory=list)
    written: threading.Event = field(default_factory=threading.Event)
    mode: str = "normal"
    preludes: deque[list[dict[str, object]]] = field(default_factory=deque)
    pushed: list[dict[str, object]] = field(default_factory=list)
    rejected_queries: int = 0
    split_gap: float = 0.0

    def reset(self) -> None:
        client = self.mock.server._client
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()


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
        previous = mock.dps.copy()
        response = original(session, message)
        if values:
            simulation.writes.append(values)
            if simulation.mode == "close":
                mock.server._client.shutdown(socket.SHUT_RDWR)
                response = None
            elif simulation.mode in ("empty_ack", "curtain"):
                if simulation.mode == "curtain":
                    mock.dps.clear()
                    mock.dps.update(previous)
                response = session.profile.pack_response(session, message.cmd, b"")
            elif simulation.mode == "drop_reply":
                response = None
            simulation.written.set()
        return response

    original_dispatch = Session.handle

    def dispatch(session: Session, message: Any) -> bytes | None:
        query = message.cmd in (tinytuya.DP_QUERY, tinytuya.DP_QUERY_NEW)
        if query and mock.config.dev22:
            simulation.rejected_queries += 1
        pushes = b""
        if query and simulation.preludes:
            batch = simulation.preludes.popleft()
            simulation.pushed.extend(batch)
            pushes = b"".join(session.status_push(dps) for dps in batch)
        if query and simulation.mode == "drop_query_reply":
            return pushes or None
        reply = original_dispatch(session, message)
        if query and reply and simulation.split_gap:
            cut = len(reply) // 2
            mock.server._client.sendall(pushes + reply[:cut])
            time.sleep(simulation.split_gap)
            return reply[cut:]
        return pushes + reply if pushes else reply

    monkeypatch.setattr(Session, "_handle_control", handle)
    monkeypatch.setattr(Session, "handle", dispatch)
    mock.start()
    thread = mock._thread
    try:
        yield simulation
    finally:
        mock.stop()
        assert not thread.is_alive()
        assert not simulation.preludes
