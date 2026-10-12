# Controller setup

## Pairing and credentials

Pair the bedding with the Sunbeam app using the supplied manual. It requires a
2.4 GHz Wi-Fi network. The manual’s AP pairing procedure is the fallback if
automatic pairing fails. Pairing and initial local-key retrieval need the
existing vendor/Tuya setup tools; the daemon has no cloud fallback or discovery
loop.

Use TinyTuya’s interactive wizard in a private directory outside this
repository. For example, create an owner-only setup directory, change to it, and
run `uv tool run --from tinytuya==1.20.0 python -m tinytuya wizard`. Supply
cloud credentials interactively, not in command arguments. Follow
[TinyTuya’s setup instructions](https://github.com/jasonacox/tinytuya#setup-wizard)
for linking the paired device to the appropriate Tuya account/project.

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
zones in A/B order. The example protocol is illustrative; the actual device’s
protocol is still pending qualification. Each controller has one connection
owner, even when it has two zones. Run only one bridge instance for that set of
controllers. Each controller’s optional TCP `port` defaults to 6668.

Use `{"env": "VARIABLE_NAME"}` or `{"file": "/mounted/secret"}` for each local
key and MQTT password. A local key must be exactly 16 ASCII bytes. Secret files
may end in one newline. TLS supports a custom CA and a client certificate/key
pair; verification is mandatory. Bind HTTP explicitly to the intended management
interface. Configuration changes require a restart.

Writes default to disabled. The [controller worker guide](controller-worker.md)
describes the explicit `write_mode` choices used during device qualification.
Select `multi_dp` only after verifying combined writes, or `level_before_power`
after verifying the documented individual-write order. Complete the hardware
acceptance record before production use.

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
version in JSON logs. Known local keys and MQTT passwords are redacted from
strings; credential-bearing objects are refused before serialization. Unexpected
application failures log their exception type and exit nonzero.

SIGINT and SIGTERM stop command admission, publish controller and bridge offline
while MQTT is connected, and join the controller owners before exiting. A
violated worker shutdown bound is fatal. A crash triggers the bridge’s offline
Last Will; consumers also require the live session heartbeat.

HTTP diagnostics and container deployment remain in progress; the
[implementation status](implementation-status.md) records those remaining gates.
