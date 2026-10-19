# Local Tuya MQTT bridge implementation plan

Plan dated October 11, 2026. This records the architecture and acceptance
requirements. The [implementation status](implementation-status.md) tracks
verified work and remaining qualification. The
[design and device research](tuya-mqtt-homekit.md) records the hardware findings
and HomeKit decision. The [quality review](bridge-quality-review.md) explains
the Rockville baseline and why the existing bridges were not selected.

Build a small Python bridge using TinyTuya for local device communication and
aiomqtt for MQTT. Extend Hoboken separately to present each mattress-pad zone as
a HomeKit fan with a stateful preheat switch. The bridge owns the controller;
Hoboken owns the HomeKit presentation; existing MQTT automation owns schedules.

The central engineering task is reliable behavior around an established protocol
library: configuration, state interpretation, command handling, recovery,
diagnostics, and tests. TinyTuya owns packet framing, encryption, session
negotiation, and protocol quirks throughout.

## Shared project conventions

Follow [shields/right-answers](https://github.com/shields/right-answers) as the
canonical source for development conventions. Rockville supplies the bridge
architecture and operational baseline; right-answers supplies the general,
language, CI, container, and documentation rules. The project’s
[agent instructions](../AGENTS.md) link to the applicable guides so this
requirement persists through implementation.

This plan incorporates the guides at
[revision f7ec496](https://github.com/shields/right-answers/tree/f7ec4968396640519e436962bcebd692199a402d).
Recheck the current guides and their shared configurations when implementing
each area. Use the shared configuration as the starting point and document
project-specific deviations; do not infer an exception merely because a
reference project uses a different setting. Preserve existing license and
authorship information when adopting shared files.

Apply the general prose and Markdown conventions to project documentation,
including sentence-case headings, the prescribed typography, Prettier with
`proseWrap: "always"`, and Mermaid diagrams. Reference the canonical guides
instead of duplicating their full contents here.

## Scope and completion criteria

The first release supports the actual Sunbeam controller and its verified zones.
It must provide:

- Local power, ten heat levels, preheat, and supported auto-off settings, with
  physical-controller changes reflected in MQTT and HomeKit.
- Separate reported state and command results, truthful availability, and
  automatic recovery from controller, Wi-Fi, broker, and process interruptions.
- Rockville-style structured logs, Prometheus metrics, HTTP health, a live
  status page, reproducible containers, and enforced quality checks.
- 100% line and branch coverage of our Python application code, real-library TCP
  tests, real-Mosquitto tests, and recorded hardware acceptance.
- Ordinary operation with the controller and bridge denied Internet access,
  after pairing and obtaining the local key.

Start with static configuration and a built-in Sunbeam profile. A single-zone
profile is included only if needed by the actual hardware and verified against
it. The initial release does not need arbitrary device templates, a scheduler,
Home Assistant discovery, a cloud API client, or firmware replacement. Cloud
pairing and key retrieval remain a documented setup procedure using existing
tools. There is no automatic cloud fallback.

## Application structure

Use `tuya-mqtt` as a working command name and `tuya_mqtt` as the Python package.
Model the structure and development commands on
[Rockville](https://github.com/shields/rockville/tree/245cd220889bf033faf1e152e94637b3e6a17b9f),
without copying its vacuum-specific behavior.

| Module                                   | Responsibility                                                                                                 |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `config.py`                              | Strict parsing into immutable typed configuration; secret resolution.                                          |
| `domain.py`                              | Controller and zone identifiers, reported state, commands, results, connection states, and typed errors.       |
| `profiles/sunbeam.py`                    | Pure DP decoding, enum conversion, capability validation, and write encoding.                                  |
| `tuya_client.py`                         | The only application module importing TinyTuya; serialized device access and normalization of library results. |
| `controller.py`                          | Per-controller worker lifecycle, polling, heartbeats, command deadlines, and reconnect policy.                 |
| `bridge.py`                              | MQTT connection lifecycle, subscriptions, retained snapshots, availability, and command/result routing.        |
| `metrics.py`, `status_page.py`, `log.py` | Operational reporting from the same internal state used by the bridge.                                         |
| `__main__.py`                            | Configuration validation, inspection, startup, and coordinated shutdown.                                       |

Keep conversion functions independent of sockets, MQTT, and wall-clock time.
Expose a small typed device interface that tests can replace. Inject the MQTT
factory, monotonic clock, wall clock, sleep, and jitter source where relevant.
Use monotonic time for deadlines and ages; wall-clock timestamps are for humans.

The asyncio loop owns MQTT, HTTP, and coordination. One dedicated worker thread
owns each physical controller’s synchronous TinyTuya `Device`, including all
reads, writes, heartbeats, and close operations. A dual-zone controller still
gets one worker and one socket. Do not issue concurrent `asyncio.to_thread`
calls against a shared `Device`. Use bounded queues for communication with the
worker; a stalled device must not block MQTT or the other controllers.

TinyTuya 1.20.0 is the reviewed baseline. At implementation, check the current
stable release, qualify it, and pin the tested dependency graph in `uv.lock`.
Document a specific compatibility reason if an older release is necessary. Use
the established synchronous API. The newer experimental Monitor API is
unnecessary here. [Release](https://pypi.org/project/tinytuya/1.20.0/),
[TinyTuya design](https://github.com/jasonacox/tinytuya/blob/v1.20.0/DESIGN.md).

TinyTuya reads one frame for each `status()` or `receive()` call and does not
match frames to commands, so a controller’s unsolicited status push can come
back in place of the reply to a status query while the reply stays queued behind
it. The adapter treats a frame as the reply only when its command equals the
command it sent, and keeps receiving until that frame arrives or the request
deadline passes. Each wait lasts for the time left until the deadline, not for
an idle receive slice, because a read that times out inside a frame discards the
part it had read. The deadline starts before the query is sent once the
connection is synchronized, and after the first call during synchronization,
which also connects and negotiates. The waits run with the socket retry limit at
zero, so a connection reset is reported at once and TinyTuya does not reconnect
and wait for a reply to a query it did not send again; synchronization, which
otherwise runs with a limit of one, asks again once after such a reset. It does
not also require the sequence number to match; the
[adapter qualification](adapter-qualification.md) records why. This relies on
the `raw_sent` and `raw_recv` attributes of TinyTuya’s `Device`, which are
undocumented, so an update to the pinned version must repeat the adapter
qualification. The synchronization query and every later status query return
only the reply.

## Configuration and setup

Provide `validate-config`, `inspect`, and `run` subcommands. `inspect` performs
a read-only status query through the same adapter and prints sanitized DP data:
the controller’s reply to that query, with any unsolicited pushes that precede
it skipped. If no reply arrives it prints no data, reports the error on standard
error, and exits nonzero. The Sunbeam PP21KQ (firmware main V2.1.6, MCU V1.0.10,
protocol 3.3) accepted a second local session while another held its connection,
so on that controller `inspect` can run alongside the bridge. That evidence
comes from one controller, and TinyTuya’s README and the tuyamock simulator both
say Tuya devices generally accept one local connection. Confirm that any other
device tolerates a second session before running `inspect` beside the bridge.

Configuration includes MQTT host/port, a stable unique client ID, topic prefix,
TLS settings when applicable, HTTP bind address/port, and a list of controllers.
Each controller supplies a logical name, device ID, reserved IP address,
explicit protocol version, local-key reference, profile, and zone names. Store
protocol versions as validated strings rather than configuration floats.

Resolve local keys and MQTT passwords from named environment variables or
mounted secret files. Do not place secrets in example configuration, command
arguments, logs, HTTP responses, or test artifacts. TinyTuya’s device `repr`
includes its local key in the reviewed version, so logging device objects is
specifically forbidden and covered by redaction tests.
[Reviewed implementation](https://github.com/jasonacox/tinytuya/blob/v1.20.0/tinytuya/core/XenonDevice.py).

Reject unknown configuration keys, duplicate device IDs or topic paths, MQTT
wildcards in configured names, missing secrets, invalid ranges, and unsupported
profiles before opening network connections. Configuration errors exit nonzero.
Use a DHCP reservation for the controller. Discovery and key retrieval use
TinyTuya’s existing setup tools; the daemon does not scan the LAN or discover
credentials during normal operation.

Document where setup tools save credentials and keep those files outside the
repository. Back up the device identity, key, verified protocol, and profile.
After re-pairing, replacing the local key is an explicit configuration update.

## Device profile and state model

Begin with the documented dual-zone mapping, then qualify every writable field
on the actual controller:

| Domain field                | Side A DP | Side B DP | Encoding                              |
| --------------------------- | --------- | --------- | ------------------------------------- |
| `power`                     | 14        | 15        | Boolean                               |
| `heat_level`                | 20        | 21        | `level_1` through `level_10`          |
| `preheat`                   | 24        | 25        | Boolean                               |
| `auto_off_seconds`          | 26        | 27        | Per-side lookup table of timer enums  |
| `remaining_seconds`         | 28        | 29        | Nonnegative seconds; read-only        |
| `preheat_remaining_seconds` | 102       | 103       | Nonnegative seconds; read-only        |
| `fault_code`                | 13        | 13        | Nonnegative bitmap; read-only; shared |

DP 13 is one controller-wide fault bitmap, so both zone snapshots carry the same
`fault_code`. The meaning of its bits is unknown: Tuya’s schema labels 13 bits
`e1` through `e13` and nothing more. Publish the value as reported, log a
nonzero value when it is first reported or changes, log its return to zero, and
export it as a metric, but do not let it alter availability, readiness, or
command admission until the bits are understood. A non-integer, negative, or
larger-than-9,007,199,254,740,991 value is a compatibility error on this
optional field, not a fault code, so neither JSON nor Prometheus loses precision
or fails on it. The two countdown fields are validated the same way.

The starting source is the
[Sunbeam dual-zone definition](https://github.com/make-all/tuya-local/blob/main/custom_components/tuya_local/devices/sunbeam_dual_bedwarmer.yaml).
Combined controls at DPs 1, 4, and 8 are not used for independent zones. Record
which physical side corresponds to each configured zone. Map `Stay_On` to 86,400
seconds, matching the manual’s 24-hour setting; never call it unlimited.
Enumerate the actual timer choices rather than accepting arbitrary durations.

Separate three kinds of information:

1. **Reported state:** values received from the controller. Missing means
   unknown, not false, zero, or a guessed default.
2. **Connection and freshness:** current connection generation, last successful
   full status, last valid traffic, and whether required fields are known.
3. **Pending command:** requested values, receipt time, deadline, and result.
   These values do not become reported state merely because a write returned.

Merge partial reports within the current device connection. On reconnect,
invalidate freshness and collect a fresh baseline before marking the controller
online. Keep the previous snapshot for diagnostics, but do not merge its old
fields into the new baseline. Require power, level, and preheat for each exposed
zone; timer telemetry may remain unknown until received. Publish only reported
countdowns, without inventing decrements between reports.

An unknown unrelated DP can be retained for sanitized inspection. Invalid types
or unknown enums on mapped fields are compatibility errors: invalidate the
affected state, refuse dependent commands, and expose the error. Do not silently
coerce `true` to heat level one or an unknown timer to a default.

## MQTT contract

This contract is the implementation target. The prefix is configurable; these
examples use `tuya`, controller `bed`, and zones `side_a` and `side_b`. Use
`tuya/bridge/availability` consistently with Rockville’s prefix convention.

| Topic                      | Payload / purpose                                                          | Retained |
| -------------------------- | -------------------------------------------------------------------------- | -------- |
| `tuya/bridge/availability` | `online` or `offline`; bridge Last Will is `offline`.                      | Yes      |
| `tuya/bridge/heartbeat`    | Current MQTT connection’s random `session_id`.                             | No       |
| `tuya/bed/availability`    | `online` only after a fresh usable controller baseline.                    | Yes      |
| `tuya/bed/side_a`          | Complete merged reported snapshot for this zone, including `session_id`.   | Yes      |
| `tuya/bed/side_a/set`      | Validated JSON command, optionally with `request_id`.                      | No       |
| `tuya/bed/side_a/get`      | Empty payload requests a fresh status read and republication.              | No       |
| `tuya/bed/side_a/result`   | Command result with `request_id`, outcome, and error code when applicable. | No       |

Use the same zone topics for `side_b`. Example state:

```json
{
  "session_id": "56356013-909d-4ae8-a62e-0c775d9c7a38",
  "power": true,
  "heat_level": 4,
  "preheat": false,
  "auto_off_seconds": 3600,
  "remaining_seconds": 3520,
  "preheat_remaining_seconds": 0,
  "fault_code": 0
}
```

Example command and terminal result:

```json
{
  "request_id": "bed-left-42",
  "power": true,
  "heat_level": 6
}
```

```json
{
  "request_id": "bed-left-42",
  "outcome": "confirmed"
}
```

Commands may contain `power`, `heat_level`, `preheat`, and `auto_off_seconds`.
At least one writable field is required. Reject unknown fields, read-only
fields, duplicate JSON keys, invalid types, nonfinite numbers, out-of-range
levels, unsupported timers, and oversized payloads. Validate the entire command
before making any write. Define supported field combinations in the profile;
initially keep preheat changes separate from other commands and reject
contradictory requests such as power-off combined with preheat-on.

The Sunbeam PP21KQ firmware turns a side on whenever a level is written, even in
a packet that also says power off, so a level cannot be selected while the side
stays off. Power off therefore stands alone. A command with `heat_level` and no
`power` is sent as a level write for a side that is on, and as an explicit power
on plus the level for a side that is off, decided from fresh state just before
the write. The explicit power write matters because only it starts the
controller’s presence-confirmation window. A `power: true` command is sent as
given. The firmware ignores a timer for a side that is off, so
`auto_off_seconds` without `power` or `heat_level` is rejected with `zone_off`
for a side that is off. Hoboken explicitly sends both fields for a positive
speed request. Enabling or disabling preheat writes its verified DP and reflects
any firmware side effects in reported state, which includes turning the side on
at level 10.

Use a terminal outcome of `confirmed`, `rejected`, or `unconfirmed`:

- `confirmed` means a fresh controller report satisfies the requested fields. It
  does not prove that the controller will keep those values indefinitely.
- `rejected` means validation, unavailability, or queue admission prevented any
  write. Include a stable error code.
- `unconfirmed` means a write may have taken effect but the complete request
  could not be verified. Include the reason; never automatically replay it.

An absent `request_id` is generated by the bridge. Bound supplied IDs to 128
characters. Keep an in-memory record of pending IDs and completed results per
zone and MQTT session, initially capped at 1,024 entries with completed results
expiring after 60 seconds. A duplicate ID with the same command joins the
pending operation or reuses its result; the same ID with different content is
rejected. Do not evict pending entries to admit more work. This is duplicate
suppression, not durable exactly-once delivery. Do not persist pending commands
or results.

### Delivery, freshness, and reconnect behavior

Use MQTT 5 with a clean start and zero session expiry. Commands and results use
QoS 0; state and availability use QoS 1. Hoboken and other publishers must not
buffer commands while disconnected. Configure command subscriptions with Retain
Handling 2 and Retain As Published, and reject retained live commands. This
covers both old retained commands delivered at subscription time and a publisher
incorrectly retaining a new command. Verify these options through aiomqtt/Paho
against the real broker; do not silently fall back to weaker subscription
behavior. MQTT 5 is a deployment prerequisite for this contract.
[MQTT 5 subscription semantics](https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html).

Create a fresh MQTT client object for each connection attempt so library-owned
outgoing queues cannot survive a reconnect. Use an explicit finite keepalive,
initially 15 seconds, and the same bounded backoff policy as device connections.
Only advertise bridge-online after subscriptions succeed and initial offline
controller availability has been published. Check broker acknowledgments for
subscription and QoS 1 publication failures.

Publish changed state only, with explicit republication after `/get` and after
reconnect even when values are unchanged. Keep timestamps in diagnostics so
polling does not defeat state deduplication. A reconnect creates a new
`session_id`, invalidates queued requests from the previous connection, and
forces fresh controller reads before publishing usable snapshots.

Retained `online` values alone are insufficient after a broker restores its
database. Publish a nonretained QoS 0 heartbeat every 15 seconds, encoded as a
JSON object containing `session_id`. Hoboken starts unavailable after its own
MQTT reconnect and requires a fresh heartbeat, a snapshot carrying that session
ID, and online bridge/controller availability. If heartbeats stop for 45
seconds, it becomes unavailable even if a Last Will has not arrived. Send a
heartbeat immediately after bridge connection setup. Publish controller-offline
state before accepting commands during startup.

Set a retained bridge-offline Last Will before connecting. On orderly shutdown,
stop admitting commands, publish controller and bridge offline while MQTT is
still connected, then disconnect. A process crash may leave retained controller
availability online; consumers must always combine it with bridge liveness.
Treat MQTT publication failure as a connection failure, not success. Keep only
the latest reported snapshot during broker outages, with no outbound command or
result replay queue.

## Controller lifecycle and command execution

Use explicit states: disconnected, connecting, synchronizing, online, and
stopping. Configuration/profile faults remain visible errors. A successful TCP
connect alone does not make the controller online.

On connection, request the complete configured DP set through TinyTuya, validate
the reply to that request, publish a baseline, and begin accepting commands.
While online, process device pushes, schedule heartbeats, and periodically query
status. Command traffic must not starve reads or heartbeats. On disconnect or
stale required state, publish offline, invalidate pending work, and retry
connection with bounded exponential backoff and jitter. Reset backoff after
synchronization, not merely after a socket opens.

The following are starting defaults to measure and tune during qualification:

| Setting                                                  | Initial target                                                 |
| -------------------------------------------------------- | -------------------------------------------------------------- |
| Device connect and request socket timeout                | 2 seconds per blocking operation                               |
| Idle receive slice                                       | At most 250 ms, with no command retries hidden in an idle read |
| Device heartbeat interval                                | 10 seconds                                                     |
| Full status poll                                         | 30 seconds                                                     |
| Maximum age of required state without successful refresh | 90 seconds                                                     |
| Reconnect backoff                                        | 1 second doubling to a maximum of 60 seconds, including jitter |
| Pending command queue                                    | 16 requests per physical controller                            |
| Maximum time waiting to begin a command                  | 2 seconds                                                      |
| Command confirmation deadline                            | 8 seconds from receipt                                         |
| Worker shutdown target                                   | 10 seconds                                                     |

Backoff uses injected jitter, for example up to 25% of the base delay capped at
the stated maximum. A failed explicit poll can take the controller offline
sooner than the freshness limit. An idle `receive()` returning `None` is normal;
it does not by itself establish an outage. Likewise, a send-only method
returning `None` is not confirmation. Normalize TinyTuya error dictionaries and
exceptions into explicit adapter errors.

Before executing each command, recheck availability, connection generation,
deadline, and required state. Reject full or expired queues visibly. Serialize
commands for both zones through the controller worker. Send each command as one
multi-DP write, which the PP21KQ firmware was verified to support for power,
timer, and level together. A write that is sent and then fails or loses its
connection may have taken effect, so the result is `unconfirmed`, followed by
state readback, with no speculative rollback.

For confirmation, process reports received after the write and perform a fresh
status query when needed. A prior cached match or an empty transport ACK is not
enough. On timeout or connection loss, record an unconfirmed result. Late
reports still update state but do not change an already returned result. If MQTT
has disappeared, retain the outcome only in diagnostics; the caller times out
and must read current state before deciding what to do next.

### Qualify TinyTuya’s retry behavior first

TinyTuya 1.20.0 has internal reconnect/resend paths. `set_retry(False)` controls
some receive retries; it does not disable all write retries. Setting the socket
retry limit to zero globally also prevents opening a new connection. Therefore,
“the bridge never requeues commands” is not sufficient evidence that a write
cannot be resent inside the library.
[Retry and connection implementation](https://github.com/jasonacox/tinytuya/blob/v1.20.0/tinytuya/core/XenonDevice.py).

Make adapter qualification an early gate. With an already established socket,
use the library’s public controls to bound write attempts and prevent automatic
write replay across connection loss. Confirm the behavior by counting decoded
writes at the simulated device when replies are dropped and connections fail.
Qualify reads and initial connection separately. A command may have reached the
device before a failure, so preserve the unconfirmed outcome even when resending
has been disabled.

If the public controls cannot implement the required policy, isolate the issue
and seek a small upstream fix or a reviewed pinned dependency patch. Do not
solve it by copying protocol internals or creating another Tuya implementation.
Do not enable production heating commands until this gate passes.

Shutdown must respect the synchronous worker: cancelling an asyncio task does
not stop an executing thread. Stop admission, expire queued commands, let
bounded operations finish, close from the owner thread, and join it. Test the
worst case, including stalled negotiation. An unexpected worker failure or a
violated shutdown bound is a fatal error with a nonzero exit, not a logged
warning followed by a falsely healthy process.

## Error policy and operations

Invalid startup configuration fails immediately. Expected network outages are
explicit operating states: log the transition at error level, mark availability,
increment failure metrics, and retry within the stated bounds. Repeated attempts
can be rate-limited in logs, but the persistent error remains visible in status
and metrics. A healthy controller continues working when another is offline.
Unexpected application exceptions terminate the process; do not hide failed
background tasks in a broad catch-and-continue loop.

Use structlog for structured logs with controller, zone, operation, request ID,
error code, duration, and recovery transition. Do not log raw credential-bearing
objects or enable TinyTuya’s verbose protocol logging in ordinary operation.
Include application revision, Python version, and TinyTuya version in startup
diagnostics.

Provide these HTTP routes through aiohttp:

| Route             | Behavior                                                                                                                       |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `/healthz`        | Liveness: the process and its required coordination loops are running. Dependency outages alone do not trigger a restart loop. |
| `/readyz`         | Readiness: MQTT is connected and every configured controller has a fresh usable baseline; otherwise HTTP 503 with reasons.     |
| `/metrics`        | Prometheus exposition with bounded labels.                                                                                     |
| `/` and `/events` | Read-only live status page and SSE updates, following Rockville’s model.                                                       |

Readiness is an addition to the reviewed Rockville implementation. A ready
process is not required for a healthy controller to serve requests while another
is offline. Bind HTTP explicitly to the intended management interface.

Metrics cover bridge/controller connectivity, last full-status age, reconnects,
operation failures, command outcomes and latency, queue depth, MQTT publish
failures, the reported fault bitmap, and event-loop/worker progress. Labels use
configured controller and zone names plus bounded enums; request IDs and raw
error text are not labels. The status page shows current state, stale-state age,
connection status, pending work, last error, and versions. It does not become a
second control interface.

## Hoboken integration

Implement this in the Hoboken repository after reading its current repository
instructions. Add a distinct MQTT source/profile for the mattress pad,
preserving the existing three-speed fan conversion for its current users. Keep
Tuya IDs, keys, DPs, and connection logic out of Hoboken.

Create stable accessory identities from the configured controller and zone
names. Expose `Fanv2` with `Active` and `RotationSpeed`, plus a separate
stateful `Switch` for preheat. Do not expose automatic fan mode or fabricate a
temperature. Use the existing HAP libraries and follow Hoboken’s repository
instructions and the
[right-answers TypeScript guide](https://github.com/shields/right-answers/blob/main/typescript.md).
Use Bun with `bun.lock` for packages and tests, strict type-checking-only
TypeScript, the shared ESLint/TypeScript configuration as the baseline, and
Prettier. Enable Bun coverage collection and its 100% line, function, and
statement thresholds. Use pino for any new service logging. Resolve any
differences with Hoboken’s current setup during its implementation phase.

| HomeKit action           | MQTT command                                      |
| ------------------------ | ------------------------------------------------- |
| `Active = INACTIVE`      | `{"power": false}`; preserve selected level.      |
| `Active = ACTIVE`        | `{"power": true}` using the known selected level. |
| `RotationSpeed = 0`      | `{"power": false}`; preserve selected level.      |
| Positive `RotationSpeed` | `{"power": true, "heat_level": N}`.               |
| Preheat switch changes   | `{"preheat": true}` or `{"preheat": false}`.      |

For a finite percentage `p` in `(0, 100]`, calculate
`N = min(10, max(1, floor(p / 10 + 0.5)))`. This defines half-step rounding
explicitly. Reject percentages outside 0–100 and nonfinite values. Configure a
10% characteristic step, but normalize incoming values as well. Read back
`10 * heat_level` independently of `Active`, including when power is off.

HomeKit may send Active and speed writes together. Test their ordering and
coalescing so an explicit off is not undone by publishing a remembered positive
speed. Within one coalescing window, explicit Active-off or zero speed takes
precedence; send power-off alone. Otherwise a positive speed produces level and
power together, and Active-on alone produces power-on. Subsequent windows are
new requests. Use distinct request IDs and avoid MQTT offline buffering.

Subscribe to results before publishing. Complete a HomeKit write successfully
only after confirmation; rejection, timeout, and unconfirmed outcomes produce an
appropriate HAP error and preserve reported state. Do not optimistically update
the characteristic. Match responses to pending request IDs and discard late
responses after the caller’s deadline. Qualify the full deadline against HAP’s
write timeout during integration.

Gate getters and setters on the bridge heartbeat, matching session ID,
controller availability, and required reported fields. Missing state becomes
HomeKit “No Response,” not an invented default. Preheat must follow its reported
Boolean until the controller ends it; the existing momentary scene-switch
behavior is unsuitable. Test actual Home app and Siri behavior on both sides.

## Test design and quality gates

### Deterministic application tests

Follow Rockville’s injected dependencies and enforce 100% line and branch
coverage of `src/tuya_mqtt`. Include startup, CLI, HTTP, and shutdown paths in
the measured application. Dependency internals are outside that denominator; do
not claim 100% coverage of TinyTuya. Missing branches should drive meaningful
failure tests or removal of unreachable code, not broad coverage exclusions.

Test the following observable behavior:

| Area          | Required cases                                                                                                                               |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Configuration | Unknown keys, duplicate identities, missing secrets, invalid versions and ranges, sanitized errors.                                          |
| DP conversion | Every supported level and timer, both zones, unrelated DPs, invalid mapped fields, partial merges, reconnect baseline reset.                 |
| Commands      | Full validation before writes, supported combinations, queue bounds, expiry, duplicate IDs, write failure, empty ACK, confirmation, timeout. |
| Lifecycle     | Initial failure, repeated failure, jitter bounds, reset only after synchronization, stale state, receive timeout, cancellation and shutdown. |
| MQTT          | Changed-state deduplication, explicit refresh, session changes, rejected retained commands, unavailable ingress, publication failures.       |
| Operations    | HTTP status codes, meaningful metrics, SSE lifecycle, secret redaction, fatal task failures.                                                 |

Write pytest tests as plain functions with bare assertions and fixtures, as
specified by the
[Python guide](https://github.com/shields/right-answers/blob/main/python.md).
Use fake time for scheduling and deadline tests. Assert transitions and effects,
not narrow real-time tolerances. Hoboken tests cover percentage boundaries,
power preservation, coalescing in both orders, preheat persistence, session and
heartbeat gating, and command-result timeout/error behavior.

### Real TinyTuya and simulated-device tests

Run the actual adapter and pinned TinyTuya against a separate TCP device
simulator. Begin by qualifying a pinned existing emulator for the discovered
protocol and device dialect. `tuyamock` is a useful candidate for pushes,
heartbeats, stateful writes, and connection testing, but it uses TinyTuya’s own
framing and cryptographic primitives. It is not an independent protocol oracle
for this client. Record that limitation; do not claim these tests prove hardware
compatibility.
[Emulator implementation and limitations](https://github.com/3735943886/tuyamock).

Cover initial status, unsolicited partial updates, pushes queued ahead of a
status reply, simultaneous zone activity, empty acknowledgments, delayed/missing
responses, wrong key, connection closure before and after a write, stalled
negotiation where applicable, and reconnect. Instrument decoded write counts and
order to enforce the retry policy. Exercise all methods the adapter uses,
including timer and preheat commands. Test at least the actual protocol version
on the deployed Python version; advertise additional protocol support only when
it has a maintained test case.

Use existing emulator controls or a transport fault injector for stalls and
drops. Add small test support around them if needed, without implementing a
second packet encoder or cipher. Independent hardware observations and sanitized
DP fixtures establish the device mapping and catch assumptions shared by the
client and simulator. Fixtures record firmware/profile provenance and contain no
real key or account identifiers.

### Real broker and process tests

Launch an isolated Mosquitto with an allocated test port and explicit readiness
check. Run the bridge as a process with simulated devices and independent MQTT
clients. Verify:

1. A fresh subscriber receives the complete retained snapshot and can determine
   whether it belongs to the live bridge session.
2. A command produces the expected device writes, reported state, and result;
   physical-style pushes update MQTT without another command.
3. Retained commands are ignored on subscription, and retained live publications
   are rejected. Old heating commands never execute after reconnect.
4. Broker restart, including restored retained data, does not create false
   liveness or replay queued work. Require a fresh heartbeat and snapshot.
5. Abrupt bridge termination triggers Last Will; graceful shutdown publishes
   offline before disconnect. Startup works when the broker or device is absent.
6. A controller outage during a command produces an unconfirmed result when
   appropriate; reconnect observes actual state without replaying the command.
7. Queue overload, stalled subscribers, and SSE clients cannot create unbounded
   memory growth or starve device maintenance.
8. HTTP readiness, metrics, and logs agree with observed MQTT/device behavior.

Use events and bounded eventual assertions for integration synchronization.
Missing Mosquitto or the simulator fails the required CI job; it must not turn
the suite into a successful skip. Always clean up processes and sockets. A flaky
test is a defect to fix, not a reason to retry until a release turns green.

## Build, deployment, and maintenance

At implementation time, verify the latest stable language, tool, and dependency
versions under the
[version policy](https://github.com/shields/right-answers#version-policy). The
reviewed Python guide targets Python 3.15 whenever possible; verify the current
stable release and compatibility before choosing the runtime. Use uv for Python
project and package management. Keep runtime dependencies limited to TinyTuya,
aiomqtt, configuration/CLI support, structlog, aiohttp, and Prometheus support.
Start Ruff from the shared `.ruff.toml`, with `ALL` and its targeted exclusions;
use ty, pytest, and coverage for the other Python checks.

Make is the standard entry point: provide `make build`, `make run`, `make fmt`,
`make lint`, `make test`, and `make integration`, plus a container image target.
Formatting and linting cover code and non-code files, including Markdown, JSON,
YAML, and TOML. Use Prettier with `prettier-plugin-toml`; manage
JavaScript-based tooling through Bun and `bun.lock`. Configure Lefthook with one
pre-commit command, `make lint`, so hooks and CI run the same checks. Keep plain
imperative commit messages and respect hooks.

Follow the [CI guide](https://github.com/shields/right-answers/blob/main/ci.md):

- Run checks on pushes to every branch and on pull requests, using an explicit
  Ubuntu runner version. Use the prescribed concurrency key containing the
  workflow, event name, and ref; cancel superseded branch runs while protecting
  main.
- Pin GitHub- or vendor-maintained actions to freshly verified release SHAs with
  version comments. Use `persist-credentials: false` for checkout and pin
  test-service images by digest.
- Set workflow permissions to `contents: read`. Lint, tests, integration, and
  container smoke checks run without publication credentials. Grant additional
  permissions only to dependent jobs that need them, with the required
  explanatory comments.
- Audit workflows with zizmor’s pedantic persona and `advanced-security: false`,
  failing directly on findings. Include this required check in release gating.

CI installs the frozen lockfiles and calls Make targets for format, lint, types,
100% coverage, TCP adapter tests, broker tests, and container startup/shutdown
checks. Container publication depends on every required check for the exact
source revision being published. Images are published only from main and tagged
with the commit’s GitCalVer version, computed by `scripts/gitcalver.sh`; pull
requests and other branches cannot publish.

Follow the
[container guide](https://github.com/shields/right-answers/blob/main/containers.md):
build non-root amd64 and arm64 images using a Distroless Debian 13 runtime with
the `:nonroot` tag and an immutable digest. Select `base` or `cc` according to
the packaged Python runtime’s shared-library requirements and verify those
requirements in the container smoke test. Also verify the runtime user, HTTP
behavior, signal handling, and shutdown. Use read-only configuration/secret
mounts and document the required LAN/broker connectivity; do not depend on a
shell or curl being installed in the runtime image.

Use Renovate with `.github/renovate.json5`, `config:best-practices`,
`:semanticCommitsDisabled`, and platform automerge behind required checks.
Follow the shared non-major and development-dependency update rules, explicitly
covering the dependency groups used here. Keep required checks configured so
automerge cannot bypass them. Do not automerge TinyTuya updates, because merging
to main publishes. Complete the device qualification below before deploying
them.

Run one bridge instance for each set of controllers. Keep immutable version tags
and a documented rollback to the previous image and configuration. There is no
persisted desired-state database to restore or replay. Dependency updates run
the full gate; TinyTuya changes additionally require review of retry/timeout
behavior and a short physical-controller smoke test before deployment. A
protocol-related fix needs a regression test at the adapter boundary even if the
eventual correction is upstream.

## Implementation sequence

Phase two establishes the shared configurations, Make targets, Lefthook hook,
and CI conventions alongside the application scaffolding. Phase five completes
the Distroless release pipeline, Renovate configuration, and workflow audit.
Phase six applies the TypeScript conventions to the Hoboken changes.

Each phase should leave a reviewable change with its tests and documentation.
Software scaffolding can proceed while hardware details are collected, but
unverified profile behavior must not become an assumed production capability.
Phase one also verifies that the deployed broker supports the required MQTT 5
subscription options; broker compatibility must be settled before implementing
Hoboken’s command path.

| Phase                                      | Deliverables                                                                                                                                                  | Exit condition                                                                                                                |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 1. Controller and dependency qualification | Device/firmware identity, protocol and sanitized DP observations; pairing/key runbook; initial real-TinyTuya emulator harness; measured write retry behavior. | Local reads work; zones and writable fields are identified; write replay can be controlled without protocol reimplementation. |
| 2. Project foundation and profile          | uv project, strict typed configuration/domain model, Sunbeam conversions, CLI validation/inspection, lint/type/coverage CI.                                   | Pure behavior and failure paths pass the quality gate; profile unknowns remain explicit.                                      |
| 3. Controller worker                       | Persistent connection ownership, push/poll/heartbeat loop, command serialization/confirmation, bounded backoff and shutdown.                                  | Real adapter tests pass, including decoded write-count assertions and stalled-device recovery.                                |
| 4. MQTT bridge                             | Final topics, validation/results, retained snapshots, session heartbeat, availability, disconnect handling.                                                   | Real-Mosquitto tests prove reconnect, Last Will, retained-command rejection, and absence of application replay.               |
| 5. Operations and release                  | Logs, metrics, health/readiness, live status page, container, release gate, deployment and troubleshooting docs.                                              | Container tests pass and publication cannot bypass required checks.                                                           |
| 6. Hoboken                                 | Mattress source, per-zone Fanv2 and preheat switch, result handling, liveness gating.                                                                         | Hoboken’s full required checks pass; Home app and Siri smoke tests work with the actual controller.                           |
| 7. Offline acceptance                      | Recorded functional matrix, fault recovery, and seven-day operation with Internet blocked.                                                                    | Hardware acceptance passes, with exact versions and any firmware limitations documented.                                      |

Do not defer the transport/retry tests until the MQTT bridge is complete: they
determine whether the adapter can deliver the promised command semantics.
Likewise, validate HomeKit’s inactive fan display and write timing before
polishing the status UI.

## Hardware acceptance and remaining decisions

Record results in `docs/hardware-acceptance.md` during implementation. Include
date, controller model, firmware, TinyTuya version, bridge revision, network
conditions, and the verified DP map. The actual protocol version, zone mapping,
supported timer enum list, multi-DP behavior, and command ordering are measured
facts to collect in phase one, not choices to guess now.

Verify power, all ten levels, timer choices, and preheat independently for each
zone. Observe changes from the physical controller as well as MQTT. Check off/on
preserving the selected level, preheat completion returning to normal operation,
timer expiry, and the manual’s remote-activation presence rule that reduces
levels above four when physical confirmation is absent. Firmware changes must
appear as actual state; no periodic desired-state loop should fight them.

Block Internet access for both controller and bridge for seven consecutive days
while preserving their required LAN access. Exercise controller cold boots,
Wi-Fi interruption, bridge restart, broker restart, and changed physical
settings during an outage. Restore connectivity and verify fresh synchronization
without unexpected writes. Record any controller degradation over time, not just
whether one offline command succeeded.

Use Home and Siri for both zones: on/off, percentages, independent sides,
preheat, unavailable display, and recovery. Confirm the remembered positive
speed while inactive is usable and that coalesced HomeKit writes match intent.

Completion requires the automated gates and this hardware record. Local runtime
independence is the target; recovery after a factory reset still relies on the
separately documented pairing/key procedure unless an offline provisioning
method is established. If stock firmware fails the offline soak, record that as
a controller limitation and revisit firmware alternatives as a separate
decision.

## Curtain infrastructure

Curtains use the same controller ownership, MQTT session, availability, and
request/result contract as heated bedding. Device profiles define endpoint
count, state shape, requested datapoints, required baseline fields, command
validation and encoding, state-dependent preparation, and confirmation. The
existing Sunbeam configuration and topics retain their meaning.

A curtain has one endpoint, addressed as `<prefix>/<controller>/<zone>`, with
the same `/set`, `/get`, and `/result` suffixes. Its snapshot contains
`session_id`, `current_position`, `target_position`, and `motion`. Positions are
integer percentages: zero is closed and 100 is open. Motion is `opening`,
`closing`, `stopped`, or null. Missing reports remain null. Both positions are
required for availability; motion telemetry is optional.

The position-only command is `{"target_position":80}`, optionally with a
`request_id`. The entire command is validated before writing. Start and stop
commands, calibration, motor direction settings, and scheduling are outside this
phase. A confirmed result requires fresh device evidence that the target was
accepted, without waiting for physical arrival. Current position always comes
from device reports.

Hoboken exposes `CurrentPosition`, `TargetPosition`, and `PositionState` on a
HomeKit `WindowCovering` service. It prefers reported motion; when motion is
unknown, it infers direction from the reported positions. Inferred movement
cannot detect a stalled motor or a physical stop that leaves an old target in
the device. It never estimates position from elapsed time. HomeKit writes
require a confirmed result and usable reported state from the active MQTT
session. Existing bedding accessory identities remain stable.

The `zemismart-bcd300fb` profile implements the observed Wi-Fi motor on protocol
3.3. DP 101 is a writable destination but reports only the last completed
position; normalized positions use `100 - raw`. DP 102 remains `STOP` throughout
position-based movement and is not motion telemetry. DP 103 is a reversal
setting, is not reported by this unit, and is never written.

The user approved a hardware-specific state policy on October 18, 2026. A
successful control acknowledgement, matched by command and sequence with valid
CRC and return code zero, confirms acceptance and sets the tracked target. It
does not change current position. Repeated reports of the old position preserve
the target; a changed reported position reconciles it. Every new connection
initializes both positions from the reported position and discards any
remembered target. HomeKit can temporarily show no movement across a reconnect
while the motor continues moving. This is an explicit exception to the
reported-target baseline, not a claim that the firmware reports a separate
target. Late ACKs cannot confirm expired or cancelled requests, and commands are
never replayed.

The [hardware record](hardware-acceptance.md#zemismart-bcd300fb-qualification)
records the schema, firmware, orientation, acknowledgements, and live HomeKit
qualification. Internet-blocked testing is deferred at the user’s request;
production deployment remains pending. Physical controls are not installed, so
that acceptance check cannot be exercised here. Writes still default to
disabled. Do not infer other hardware support from the Zemismart brand.

The test-only `test-curtain` profile retains synthetic datapoints 201–203 and
separate target/current reports to exercise the general curtain contract on
protocols 3.3–3.5. Production configuration rejects this test profile.
