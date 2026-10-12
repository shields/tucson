# Tucson

A local Python bridge for Sunbeam mattress-pad controllers, using TinyTuya and
MQTT 5. Implementation follows the
[implementation plan](docs/implementation-plan.md) and the
[shared development conventions](https://github.com/shields/right-answers).

Development requires Python 3.15, uv, Bun, and Lefthook. Install locked
dependencies with `make setup`; format with `make fmt`, check with `make lint`,
and run the coverage gate with `make test`.

Hardware qualification is required before production use. The built-in profile
starts from the published dual-zone mapping; physical behavior and write
ordering must be verified on the deployed controller.
