# TinyTuya adapter qualification

## Tested baseline

The TCP suite runs the application adapter with Python 3.15.0, TinyTuya 1.20.0,
and tuyamock 0.0.6. Exact package artifacts and the dependency graph are
recorded in `uv.lock`. `make integration` requires the simulator and fails if it
is absent. The coverage gate also runs these tests.

The simulator is an unmodified existing implementation that delegates framing,
encryption, and negotiation to TinyTuya. It is useful for validating application
behavior but is not an independent protocol oracle for this client. The tests
prove behavior against a simulator, not compatibility with a Sunbeam controller.

The published simulator declares TinyTuya 1.18.1. A uv dependency override tests
it against the application’s pinned 1.20.0 without downgrading the runtime or
copying protocol code. This development-only compatibility decision is qualified
by the maintained TCP cases; any TinyTuya update must repeat qualification.

## Public-control policy

An explicit reserved IP and validated version prevent automatic LAN discovery.
The adapter configures persistent sockets, disables receive retries, removes the
send wait, and uses one socket-opening attempt during synchronization. Once
synchronized, it sets the public socket retry limit to zero. The established
socket remains usable; a lost socket cannot reopen inside an operation. A new
application-level connection creates a new library `Device`.

Writes call the public `generate_payload` and `send` methods. TinyTuya continues
to own the packet format and crypto. The synchronous `set_multiple_values`
helper has an additional failed-write fallback to individual writes, so it is
excluded from the adapter. A send-only `None` result is transport completion,
not confirmation. The controller worker must confirm requested values from fresh
reports.

Device22 detection resets the requested DP list inside TinyTuya. An incomplete
initial report causes the adapter to restore the complete configured DP set and
request status again. The worker remains responsible for checking the usable
baseline and freshness before advertising availability.

## Maintained cases

| Protocol          | Cases                                                                                                                                                                                                                                                       |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 3.3, 3.4, and 3.5 | Initial status; both zone writes; timer/preheat writes; empty heartbeat ACK; idle receive timeout; unsolicited partial push; wrong key; dropped write response; empty write ACK; silent device; connection closure before/after a write; explicit reconnect |
| 3.3 and 3.4       | Device22 status detection and restoration of the complete requested DP set                                                                                                                                                                                  |
| 3.4 and 3.5       | Stalled session negotiation                                                                                                                                                                                                                                 |

A delayed-response case exceeds the request deadline and checks that reconnect
reads the applied state without another write. A status poll tolerates a queued
empty heartbeat ACK by receiving until a report or its monotonic deadline. The
ACK itself does not update state or freshness.

Instrumented existing simulator handlers count decoded concrete writes,
excluding all-null device22 status queries. Fault cases assert one decoded
multi-DP write, no implicit reconnect, rejection of further operations after the
connection fails, and observation of applied state after explicit reconnect
without a second write. The simulator uses only synthetic keys and DP fixtures
with no device identifiers.

Socket timeouts are two seconds in production and idle receive slices are 250
milliseconds. The integration tests shorten request timeouts for injected faults
and use bounded events for synchronization. Final worker shutdown bounds,
command-confirmation semantics, and actual hardware behavior remain separate
acceptance gates.
