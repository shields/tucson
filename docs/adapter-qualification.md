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

Device22 detection resets the requested DP list inside TinyTuya. An initial
report missing any mapped DP except the optional fault bitmap (DP 13) causes the
adapter to restore the complete configured DP set and request status again. The
worker remains responsible for checking the usable baseline and freshness before
advertising availability.

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

## MQTT adapter qualification

The transport uses aiomqtt 2.5.1 with MQTT 5, Clean Start, a zero session-expiry
interval, and bounded incoming/outgoing queues. Subscriptions request QoS 0,
Retain Handling 2, and Retain As Published. Incoming overflow fails the
connection; it cannot silently discard accepted work. Publication is serialized
and retained operational messages use QoS 1. Messages carry their monotonic
arrival time so deadlines include input-queue waiting. Empty MQTT payloads
remain empty bytes.

The pinned aiomqtt implementation ignores negative PUBACK reason codes. A small
adapter override detects a rejected publication, invalidates the connection, and
closes its socket outside Paho’s callback mutex. Socket cleanup also covers
failed/cancelled connection attempts and callback failures. The bridge will
treat these as connection failures and require a fresh session.

Real-broker cases use Mosquitto 2.1.2 and independent clients. They verify that
old retained commands are suppressed on subscription, live retained flags are
preserved, fresh subscribers receive retained state, a denied QoS 1 publication
fails and triggers the offline Last Will, and persisted broker data does not
preserve a nonretained heartbeat. These qualify the adapter; the process cases
below qualify daemon routing and recovery. An overflowing incoming queue also
closes the connection and triggers its Last Will, while preventing queued
commands from escaping that failed session. An unexpected application failure
also aborts the socket so the broker publishes the offline Last Will; a graceful
disconnect cannot leave retained online state behind after a fatal task failure.

CI builds the broker from the official
[2.1.2 source revision](https://github.com/eclipse-mosquitto/mosquitto/tree/99fa50f30e325609394c324c8ff71cfbbe95d8ab)
using `make test-broker`. Test targets prefer that binary over the system
broker. Missing Mosquitto fails the tests.

The cluster runs Mosquitto 2.0.22. On 2026-10-14 the whole suite, including
every real-broker and process case, passed against a broker built from the
upstream `v2.0.22` tag (commit `766fa2c`), and the broker and process cases
passed five more times in a row. This qualifies the MQTT 5 subscription options,
retained-message handling, session expiry, and Last Will behavior the bridge
depends on for that version. It is a source build, not the container image, so
the pod’s first connection to the deployed broker remains the final check.

## Daemon process qualification

The process suite starts the actual `tuya-mqtt run` entry point with allocated
controller/broker ports and independent MQTT clients. The shared simulator
fixture counts decoded writes and injects transport failures; only synthetic
keys and identities are used.

End-to-end command, retained-state, physical-style push, explicit-refresh, and
graceful-shutdown cases cover protocols 3.3, 3.4, and 3.5. They exercise both
zones, level-only selection, timer and preheat writes, and reported countdowns.
Additional cases verify live retained-command rejection, abrupt-process Last
Will, unavailable-controller admission, startup without a broker, and applied
but unconfirmed writes followed by observation without replay.

The broker-restart case pauses the daemon while Mosquitto persists/reloads its
database. A fresh observer receives the old retained snapshot without a
heartbeat. Resuming the daemon produces a new session and fresh controller
snapshots; the stored heating command never executes.

Silent 3.4/3.5 negotiation cases run beside a healthy controller. The healthy
controller serves commands, the silent peer remains offline, and process
shutdown completes with the production socket timeouts and worker join bound.
These tests qualify application/library coordination, not actual Sunbeam
firmware, physical zone orientation, or Home/Siri behavior.

## Controller-through-HomeKit qualification

Run `make homekit HOBOKEN_SOURCE=/path/to/hoboken NODE=/path/to/node` with both
checkouts' frozen dependencies installed. The selected target requires Hoboken's
`integration/tucson-roundtrip.ts` driver, the Node.js major version that
`node-version` sets in Hoboken's `.github/workflows/build.yml`, and the pinned
Mosquitto executable. It fails when prerequisites are absent.

The three protocol cases use the actual Tucson process and Hoboken bridge,
paired HAP HTTP requests, MQTT 5, and the real TinyTuya/TCP simulator. They
verify independent levels on both zones, stateful preheat, and off precedence
with selected-level preservation. Decoded device writes must match exactly four
expected payloads. A controller-originated push updates HomeKit without another
write; Tucson shutdown then makes both zones unavailable. Process, socket, and
pairing-state cleanup runs on failures as well as successful exits.

The assertions also check that synthetic key/password values do not appear in
the collected application logs. These cases qualify interoperability between the
two applications and share the simulator limitations above. They do not
establish the physical Sunbeam mapping, firmware behavior, or Home/Siri UI
compatibility. The two repositories' required checks remain separate; run this
additional target when qualifying their combined revisions.
