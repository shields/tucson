# Controller setup

## Pairing and credentials

Pair the bedding with the Smart Life app (“SmartLife: Smart Living”) or the Tuya
Smart app, not the Sunbeam app: TinyTuya’s wizard can link only devices
registered in one of those two. A controller already paired in the Sunbeam app
can be re-paired in Smart Life or Tuya Smart. The new pairing issues a new local
key, and the Sunbeam app then stops controlling it. Pairing requires a 2.4 GHz
Wi-Fi network, and the manual’s AP pairing procedure is the fallback if
automatic pairing fails. Pairing and initial local-key retrieval need the
existing vendor/Tuya setup tools; the daemon has no cloud fallback or discovery
loop.

Use TinyTuya’s interactive wizard in a private directory outside this
repository. For example, create an owner-only setup directory, change to it, and
run `uv tool run --from tinytuya==1.20.0 python -m tinytuya wizard`. Supply
cloud credentials interactively, not in command arguments. Follow
[TinyTuya’s setup instructions](https://github.com/jasonacox/tinytuya#setup-wizard)
for linking the paired device to the appropriate Tuya account/project. Choose
the Smart Home development method and the data center that matches the app
account’s region. The wizard saves only the standard instruction set; the
[hardware acceptance record](hardware-acceptance.md#available-evidence) shows
how to query the full data model, which declares the remaining DPs.

The wizard writes cloud credentials to `tinytuya.json`, device identities and
local keys to `devices.json`, and polling snapshots to `snapshot.json`; raw
cloud data can be saved to `tuya-raw.json`. Keep all of these outside the
repository and protect them as credentials. Do not copy their raw contents into
issues, logs, fixtures, or acceptance records.

Reserve the controller’s LAN address in DHCP. Record and back up its identity,
local key, measured protocol version, profile, firmware, and physical A/B zone
orientation. Re-pairing can change the key: replace the secret and restart the
bridge explicitly.

## Bridge configuration

Copy `config.example.json` to an untracked private configuration file. Replace
the example device identity/address, verify the protocol version, and name the
zones in A/B order. The example protocol, 3.3, matches the one observed on the
Sunbeam PP21KQ controller; confirm it for any other device. Each controller has
one connection owner, even when it has two zones. Run only one bridge instance
for that set of controllers. Each controller’s optional TCP `port` defaults
to 6668. Every address and port pair must be unique, so two controllers can
share an address only on different ports.

Use `{"env": "VARIABLE_NAME"}` or `{"file": "/mounted/secret"}` for each local
key and MQTT password. A local key must be exactly 16 ASCII bytes. Secret files
may end in one newline. TLS supports a custom CA and a client certificate/key
pair; verification is mandatory. Bind HTTP explicitly to the intended management
interface. Configuration changes require a restart.

Writes default to disabled. The [controller worker guide](controller-worker.md)
describes the explicit `write_mode` choices used during device qualification.
Select `multi_dp` only after verifying combined writes on the controller.
Complete the hardware acceptance record before production use.

Run `uv run --frozen tuya-mqtt validate-config --config /path/to/config.json`
before connecting. Validation checks identities, names, ranges, profiles,
protocol strings, duplicate JSON keys, and secret resolution without opening
network connections.

Stop the bridge before using
`uv run --frozen tuya-mqtt inspect --config /path/to/config.json --controller bed`.
Inspection uses the daemon’s read-only adapter and prints sanitized numeric DP
data. It issues status queries only; it does not change heat, preheat, or
timers. Compare the observed data with the
[hardware acceptance record](hardware-acceptance.md) before enabling production
writes.

## Running the daemon

Start with `uv run --frozen tuya-mqtt run --config /path/to/config.json`. The
daemon owns one thread and connection per controller, publishes MQTT 5 reported
state and results, and keeps observing devices during broker outages. It has no
desired-state database or outbound command replay queue.

Startup records the application version/revision, Python version, and TinyTuya
version in JSON logs. The application version and revision come from the
`TUYA_MQTT_VERSION` and `TUYA_MQTT_REVISION` environment variables that the
container image sets; a run from a source checkout reports both as `unknown`.
The version in `pyproject.toml` is a placeholder and is never reported. Known
local keys and MQTT passwords are redacted from strings; credential-bearing
objects are refused before serialization. Unexpected application failures log
their exception type and exit nonzero.

SIGINT and SIGTERM stop command admission, publish controller and bridge offline
while MQTT is connected, and join the controller owners before exiting. A
violated worker shutdown bound is fatal. A crash triggers the bridge’s offline
Last Will; consumers also require the live session heartbeat.

## HTTP diagnostics

The management server binds to the configured `http.bind` and `http.port`. Its
routes are read-only:

| Route      | Behavior                                                                                                                                                                            |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/healthz` | HTTP 200 while required coordination loops make progress; otherwise HTTP 503. Dependency outages alone do not make the process unhealthy.                                           |
| `/readyz`  | HTTP 200 when MQTT and all configured controller baselines are usable; otherwise HTTP 503 with reasons. A healthy controller can still serve commands while another is unavailable. |
| `/metrics` | Prometheus connectivity, freshness, reconnect, failure, command outcome/latency, queue-depth, progress, and fault-bitmap measurements.                                              |
| `/`        | Live controller/zone state, previous snapshots, freshness ages, pending requests, errors, and software versions.                                                                    |
| `/events`  | JSON status snapshots over Server-Sent Events.                                                                                                                                      |

The status stream admits at most 16 clients. Each keeps only its latest queued
snapshot; superseded diagnostics are replaced. Writes, including stream
termination, are bounded to two seconds. Idle connections receive a comment
heartbeat every 15 seconds so dropped readers are reaped. These limits do not
change command admission or reported countdowns.

Known secrets are redacted from status strings and labels. HTTP access logs are
disabled so request URLs cannot expose credentials. Unexpected handler or
publisher failures terminate the daemon with a nonzero exit.

Use the [deployment guide](deployment.md) for container mounts, LAN access,
release gates, and rollback. The
[implementation status](implementation-status.md) records Hoboken and hardware
acceptance still in progress.
