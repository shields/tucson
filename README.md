# Tucson

A local Python bridge for Sunbeam mattress-pad controllers, using TinyTuya and
MQTT 5. Implementation follows the
[implementation plan](docs/implementation-plan.md) and the
[shared development conventions](https://github.com/shields/right-answers).

Development requires Python 3.15, uv, Bun, and Lefthook. Install locked
dependencies with `make setup`; format with `make fmt`, check with `make lint`,
and run the coverage gate with `make test`.

Use the [setup guide](docs/setup.md) for credential storage, configuration
validation, and read-only inspection. `make integration` runs the
[real-library TCP and MQTT qualification](docs/adapter-qualification.md). The
tests require Mosquitto; `make test-broker` builds the pinned test version with
CMake, a C/C++ compiler, OpenSSL, and cJSON development files. On Debian or
Ubuntu, `make test-broker-deps` installs those build prerequisites. Test targets
prefer the built broker, then a Mosquitto executable on `PATH`.

With a Hoboken checkout and its frozen dependencies installed, run
`make homekit HOBOKEN_SOURCE=/path/to/hoboken NODE=/path/to/node` for
controller-through-HomeKit qualification. This runs Tucson’s actual daemon,
Mosquitto, the real TCP device simulator, and paired HAP HTTP clients on
protocols 3.3, 3.4, and 3.5. Missing checkouts or runtimes fail the target. The
[prerequisites](docs/adapter-qualification.md#controller-through-homekit-qualification)
are in the qualification record.

Hardware qualification is required before production use. The built-in profile
starts from the published dual-zone mapping; physical behavior and write
behavior must be verified on the deployed controller.

`tuya-mqtt run --config /path/to/config.json` runs the MQTT daemon. The
[setup guide](docs/setup.md#http-diagnostics) describes the live status,
health/readiness, and Prometheus routes. The
[deployment guide](docs/deployment.md) covers Distroless images, release gates,
updates, and rollback; [troubleshooting](docs/troubleshooting.md) explains
common failure states. The
[implementation status](docs/implementation-status.md) tracks Hoboken and
physical acceptance work still in progress.

Curtain infrastructure and Hoboken's position-only HomeKit presentation follow
the [curtain contract](docs/implementation-plan.md#curtain-infrastructure). The
`zemismart-bcd300fb` profile supports the observed Wi-Fi motor on protocol 3.3,
with [documented target tracking](docs/setup.md#zemismart-bcd300fb). Endpoint,
intermediate-position, and local HomeKit tests passed on the real motor.
Internet-blocked qualification remains pending before production deployment.
Other Zemismart models are not implied to be compatible.
