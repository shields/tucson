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
send wait, and uses a socket retry limit of one during synchronization, which
lets TinyTuya reconnect and send a query once more after a timeout or reset.
Once synchronized, it sets the public socket retry limit to zero. The
established socket remains usable; a lost socket cannot reopen inside an
operation. A new application-level connection creates a new library `Device`.

Writes call the public `generate_payload` and `send` methods. TinyTuya continues
to own the packet format and crypto. The synchronous `set_multiple_values`
helper has an additional failed-write fallback to individual writes, so it is
excluded from the adapter. A send-only `None` result is transport completion,
not confirmation. Profiles normally confirm requested values from fresh reports.
The qualified BCD300FB position path instead requires a successful control
acknowledgment whose command and sequence match the current write. The adapter
captures the sent header through TinyTuya’s `raw_sent` and checks parsed
`raw_recv` messages for the matching identity, valid CRC, and zero return code.
It retains that identity across intervening queries, clears it for each write
and connection, and rejects invalid matching acknowledgments. It checks the
frames of every call that reads, including those a status query reads while it
waits for its reply, because each call replaces `raw_recv` and the
acknowledgment can be the frame read ahead of the reply. The exception is the
frame TinyTuya’s `status()` reads before it repeats the query on first detecting
device22. Only a reply containing `data unvalid` triggers that, the observed
BCD300FB acknowledgments are empty, and such a reply would leave the write
unconfirmed until its deadline. This profile permits only the observed protocol
3.3.

Device22 detection resets the requested DP list inside TinyTuya. An initial
report missing any DP the profile requires causes the adapter to restore the
profile’s complete requested DP set and request status again. For the Sunbeam
that is every mapped DP except the optional fault bitmap (DP 13); for the
BCD300FB it is DP 101. The worker remains responsible for checking the usable
baseline and freshness before advertising availability.

## Status reply matching

TinyTuya reads one frame for each `status()` or `receive()` call and does not
match frames to commands. A controller’s unsolicited status push (command 8)
therefore can come back in place of the reply to a status query, with the reply
still queued behind it. On the PP21KQ (protocol 3.3), two of 88 queries made
over three minutes while countdown timers ran were answered first by a push
holding one countdown DP, and the reply followed within 5 to 8 ms. The other 86
were answered directly, and no reply was missing.

Every status query, in synchronization and in later polls, returns the reply to
the query it sent. After `status()` the adapter keeps calling `receive()` until
the last frame TinyTuya read has the command of the query just sent and holds a
report, or the two-second request deadline passes and the query fails with
`status report missing`. Each `receive()` waits for the time left until the
deadline, not for a short slice of it: a read that times out between the pieces
of a frame discards the part it had read, so a reply that arrives in pieces
would be lost at the slice boundary. After synchronization the deadline starts
before the query is sent, so a poll waits about one request timeout for its
reply. During synchronization it starts when the first call returns, because
that call also connects and negotiates the session and so does not use up the
time allowed for the reply. Asking again, after a device22 rejection read during
the wait or a reset during synchronization, sends the query with a full request
timeout. A reset also starts a new deadline, so it can add up to two more
request timeouts; a rejection keeps the current deadline, which ends the wait if
rejections repeat. The reply supersedes the pushes read in the meantime, so they
are discarded. A queued empty heartbeat or write ACK, a read that times out, and
an empty frame with the query’s command are skipped the same way. Before a frame
read during the wait is discarded, the adapter checks it against the pending
BCD300FB write, so the wait cannot lose that profile’s control ACK. When
TinyTuya cannot say which query was sent, no frame is accepted and the deadline
ends the wait, because accepting an unrelated frame would return a push as the
status.

The adapter runs each `receive()` of the wait with the socket retry limit at
zero and restores the limit before it asks again. Synchronization uses a limit
of one. With a limit above zero, a connection reset during `receive()` makes
TinyTuya reconnect and wait on the new connection, where no query was sent,
until the deadline passes. At zero the reset is reported at once. After
synchronization the poll fails with `device operation failed` and the worker
disconnects and reconnects. During synchronization the adapter asks again once,
which reconnects and sends the query, so synchronization recovers from the
reset. A second reset fails synchronization.

The adapter matches the command and not the sequence number. TinyTuya itself
checks the echo only below protocol 3.5, tuyamock documents that real devices
echo the request, count globally, or send zero, and the pushes and replies seen
on the PP21KQ already differ in command. Requiring the echo would make a device
that does not send it unable to synchronize, while a stale reply with the same
command cannot arrive on a connection the adapter keeps: it discards the
connection on any error or timeout and sends a new query only after reading the
reply to the last one.

Protocol 3.4 with device22 is the exception to the premise that only the reply
carries the query’s command: TinyTuya sends both writes and status queries as
command 13 there. A write response that is still queued when a query is sent is
taken for its reply, and the real reply stays queued behind it until the next
read. The worker normally reads a write’s response before it polls, but a poll
or refresh that falls due in the same tick comes first. The PP21KQ is protocol
3.3 and not device22, and no maintained case writes on a 3.4 device22 device.

When a push precedes the device22 rejection, TinyTuya’s `status()` returns the
push and the rejection reaches `receive()`, which switches the library to
device22 and returns its device-type error (908) without asking again. The
adapter asks again after that error, which restarts detection as if the
rejection had come first, instead of treating it as a device failure.

The adapter reads the `raw_sent` and `raw_recv` attributes of TinyTuya’s
`Device` to know which command it sent, which frame it just read, and whether
that frame acknowledges a BCD300FB write. These are undocumented internals, but
`uv.lock` pins the version and a TinyTuya update already requires repeating this
qualification. Renaming either attribute fails every status query immediately.

## Maintained cases

| Protocol          | Cases                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 3.3, 3.4, and 3.5 | Initial status; both zone writes; timer/preheat writes; empty heartbeat ACK; idle receive timeout; unsolicited partial push; pushes ahead of a status reply; status reply arriving in pieces; connection reset while waiting for a status reply; wrong key; dropped write response; empty write ACK; silent device; connection closure before/after a write; explicit reconnect |
| 3.3 and 3.4       | Device22 status detection and restoration of the complete requested DP set, including a push ahead of the rejection                                                                                                                                                                                                                                                             |
| 3.4 and 3.5       | Stalled session negotiation                                                                                                                                                                                                                                                                                                                                                     |

A delayed-response case exceeds the request deadline and checks that reconnect
reads the applied state without another write. A status query tolerates a queued
empty heartbeat ACK by receiving until the reply to the query or its monotonic
deadline. The ACK itself does not update state or freshness. The BCD300FB
control ACK can update the accepted target, but never current position or status
age.

Test support queues zero, one, two, four, or eight countdown pushes, or one
complete but stale push, ahead of the simulator’s reply to a status query. It
builds them with the simulator’s own push framing, so it adds no packet encoder
or cipher. For each protocol, `connect()`, `status()`, `inspect`, and a
controller’s first tick must return or act on the complete reply, and every case
asserts that the simulator consumed the pushes queued for its query. A device22
case on 3.3 and 3.4 puts pushes ahead of the rejection. Pushes with no reply
fail with `status report missing` at the request deadline. The simulator can
also send the reply in two writes with a pause longer than the adapter’s receive
slice between them. A case can reset the connection with the reply withheld
while the adapter waits: synchronization then recovers on a second connection,
and a later poll fails at once without reconnecting. Deterministic cases with a
fake clock and a bounded fake device cover the same loops, the retry limit
around each wait, and the start of the deadline, so a regression in the deadline
fails instead of hanging.

BCD300FB cases also exercise delayed position reports with an immediate empty
control ACK, preservation of the acknowledged target across stale position
polls, and reconnect during movement without replay. A fresh position report
cannot replace a missing control ACK for this profile. Deterministic cases put
the control ACK ahead of, behind, and between the frames a status query reads
while it waits for its reply, and check that a rejected ACK read there fails the
connection. A case with real TinyTuya over TCP has a status query read the ACK
ahead of its reply. Sanitized observations are stored in
`tests/fixtures/zemismart-bcd300fb.json`.

Instrumented existing simulator handlers count decoded concrete writes,
excluding all-null device22 status queries. Fault cases assert one decoded
multi-DP write, no implicit reconnect, rejection of further operations after the
connection fails, and observation of applied state after explicit reconnect
without a second write. The simulator uses only synthetic keys and DP fixtures
with no device identifiers.

Socket timeouts are two seconds in production and idle receive slices are 250
milliseconds; the wait for a status reply uses the time left until its deadline
instead of a slice. The integration tests shorten request timeouts for injected
faults and use bounded events for synchronization. Final worker shutdown bounds,
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
checkouts’ frozen dependencies installed. The selected target requires Hoboken’s
`integration/tucson-roundtrip.ts` driver, the Node.js major version that
`node-version` sets in Hoboken’s `.github/workflows/build.yml`, and the pinned
Mosquitto executable. It fails when prerequisites are absent.

Each protocol runs a Sunbeam case and a curtain case, both through the actual
Tucson process and Hoboken bridge, paired HAP HTTP requests, MQTT 5, and the
real TinyTuya/TCP simulator. The curtain case starts Tucson through
`tests.curtain_daemon`, which registers the test-only `test-curtain` profile.
The Sunbeam cases verify independent levels on both zones, stateful preheat, and
off precedence with selected-level preservation; the curtain cases verify
position targets. Decoded device writes must match the expected payloads
exactly: the level, preheat, and off writes for the Sunbeam, and the position
writes for the curtain. A controller-originated push updates HomeKit without
another write; Tucson shutdown then makes every zone unavailable. Process,
socket, and pairing-state cleanup runs on failures as well as successful exits.

The assertions also check that synthetic key/password values do not appear in
the collected application logs. These cases qualify interoperability between the
two applications and share the simulator limitations above. They do not
establish the physical Sunbeam mapping, the BCD300FB mapping (the curtain cases
use synthetic datapoints), firmware behavior, or Home/Siri UI compatibility. The
two repositories’ required checks remain separate; run this additional target
when qualifying their combined revisions.
