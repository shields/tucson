# Deployment and releases

Complete [hardware qualification](hardware-acceptance.md) before production use.
The container tests use simulated controllers; they establish transport and
packaging behavior, not compatibility with the supplied bedding.

## Build and check an image

Install the locked development environment with `make setup` and build the test
broker with `make test-broker`. Docker Engine and Buildx are required for the
image targets:

```sh
make image
make test-image
make image-export
```

The default platform is the host’s amd64 or arm64 architecture. Set
`IMAGE_PLATFORM=linux/amd64` or `IMAGE_PLATFORM=linux/arm64` explicitly when
needed. Running a foreign platform locally requires emulation. CI builds and
checks each platform on its native Ubuntu 24.04 runner.

The Dockerfile pins Debian 13, uv, and Distroless by immutable index digests. uv
installs its checksum-verified Python 3.15.0 runtime and the frozen production
dependency graph. The runtime uses Distroless `cc-debian13:nonroot`: the
packaged Python requires `libgcc_s.so.1`, which the `base` variant does not
supply. Both variants have glibc; `cc` supplies the GCC runtime as well. The
image contains neither a shell nor a package manager.

Builds use the source commit time for `SOURCE_DATE_EPOCH` and rewrite layer
timestamps. The image records its source revision and its version in OCI labels
and in `TUYA_MQTT_REVISION` and `TUYA_MQTT_VERSION`; startup and HTTP
diagnostics report both. The version is a [GitCalVer](https://gitcalver.org)
number, `YYYYMMDD.N`: the commit’s UTC date and the count of commits that share
it, computed by `scripts/gitcalver.sh` from the git history. A build that is not
a clean revision of main carries a `-dev.<hash>` suffix and cannot be published.
`make image` and `make test-image` each compute the version, so run both on the
same commit and working tree. The computation needs the full history; a shallow
clone fails instead of producing a version. It also needs main’s history to stay
append-only and its committer dates never to go backwards: a force push can
reassign a published version, and a commit whose UTC date is earlier than an
ancestor’s cannot be versioned. The script is vendored unmodified from gitcalver
release v20261007.1, and a test pins its checksum, so update both together. The
Docker context allows only the application, package metadata, lockfile, and
README, keeping private configuration and secret files out of the build.

Seven smoke cases verify runtime imports and the GCC library, UID/GID 65532, the
system CA store, read-only storage, mounted configuration/secret resolution,
sanitized validation failures, HTTP/SSE behavior, SIGINT/SIGTERM shutdown, and
real-Mosquitto command confirmation through simulated protocols 3.3, 3.4, and
3.5. Graceful shutdown must exit zero and publish both offline announcements.
The ordinary application gate separately enforces 100% line and branch coverage.

## Run on the LAN

Run one bridge instance for each set of controllers: two bridges driving one
controller would fight over its state and each other's command confirmations.
Read-only inspection can run alongside the bridge, because the controller
accepts more than one local session. Use the [setup guide](setup.md) to validate
identities, protocol, zone order, and write policy.

Keep configuration and credentials outside the image. Make mounted files
readable by UID/GID 65532 and their directories traversable. For example,
root-owned files with group 65532 and mode `0640`, under directories with mode
`0750`, allow the runtime to read them without making them world-readable.
Reference local keys and MQTT passwords through secret-file paths, such as
`/run/secrets/sunbeam_local_key` and `/run/secrets/mqtt_password`.

Set `TUCSON_IMAGE` to the verified registry digest of the chosen release:

```sh
docker run --detach --name tucson \
  --network host \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --restart unless-stopped --stop-timeout 15 \
  --mount type=bind,source=/srv/tucson/config.json,target=/config/config.json,readonly \
  --mount type=bind,source=/srv/tucson/secrets,target=/run/secrets,readonly \
  "$TUCSON_IMAGE"
```

This deployment uses Linux host networking so controller and broker connections
use the host’s LAN routes. Preserve access to the configured controller TCP port
(default 6668), MQTT broker port, and any DNS resolver needed for the broker
hostname. MQTT 5 is required. Use verified TLS and read-only certificate mounts
when the broker requires them. Pairing and key retrieval remain separate setup
steps; normal daemon operation has no cloud dependency.

HTTP defaults to `127.0.0.1:8080`. Use the configured management address and
port for external probes. The diagnostic server has no authentication or control
routes, so bind it to a trusted management interface. Probe `/healthz` for
process liveness and `/readyz` for dependency readiness. Broker or controller
outages leave liveness healthy while readiness explains what is unavailable.
Readiness failures must not trigger restart loops. A probe inside the image can
use `/opt/app/bin/python` with the standard library; shell and curl are absent.

## Run in Kubernetes

The bridge makes only outbound connections: TCP to the controller's configured
address and port (default 6668), TCP to the MQTT broker, and DNS. The
configuration requires an explicit controller address, so Tuya's UDP discovery
never runs and the ordinary pod network is enough; host networking is not
needed. Set `http.bind` to `0.0.0.0` so probes and Prometheus reach the pod.

Mount secrets as files and reference them with `file` secret references. They
must be readable by UID/GID 65532, for example `fsGroup: 65532` with mode
`0440`, and one trailing newline is removed. Use `Recreate` updates, so two
bridges never drive the controller at once. Probe `/healthz` for liveness only;
`/readyz` reports broker and controller outages, which a restart would not fix.
Allow at least 30 seconds for shutdown. Leave `write_mode` at its default,
`disabled`, until the controller has passed the hardware qualification.

## Broker access control

Give Tucson its own broker user. The topics below follow the example
configuration's `topic_prefix` (`tuya`), controller name (`bed`), and zone names
(`side_a`, `side_b`). Tucson also needs `write` on its Last Will topic,
`tuya/bridge/availability`. Hoboken's heated-bedding client reads the state and
publishes only `/set` commands.

```text
user tucson
topic read tuya/bed/side_a/set
topic read tuya/bed/side_a/get
topic read tuya/bed/side_b/set
topic read tuya/bed/side_b/get
topic write tuya/bed/side_a
topic write tuya/bed/side_a/result
topic write tuya/bed/side_b
topic write tuya/bed/side_b/result
topic write tuya/bed/availability
topic write tuya/bridge/availability
topic write tuya/bridge/heartbeat

user hoboken
topic read tuya/bridge/availability
topic read tuya/bridge/heartbeat
topic read tuya/bed/availability
topic read tuya/bed/side_a
topic read tuya/bed/side_a/result
topic write tuya/bed/side_a/set
topic read tuya/bed/side_b
topic read tuya/bed/side_b/result
topic write tuya/bed/side_b/set
```

Mosquitto's ACL file never denies a subscription: a client without read
permission gets a successful SUBACK and silently receives nothing, and a denied
QoS 0 publish is dropped silently. Tucson treats a denied QoS 1 publish as a
connection failure and reconnects, and the broker checks the Last Will against
the ACL only when it fires. `tests/test_acl.py` runs the daemon and a
Hoboken-style client against a real broker with exactly this ACL, so a topic
change in Tucson fails that test until this list is updated.

## Release gate

The `Checks` workflow runs on every branch push and pull request. A push to main
runs the entire gate for that exact source revision, and publication depends on:

- `Lint and coverage`, including application, real-library TCP, real-broker,
  integration, and wheel checks.
- `Workflow security`, with zizmor’s online pedantic audit.
- `Container (amd64)` and `Container (arm64)`, including all smoke cases.

These jobs have read-only repository permissions and no publication token. They
export the checked images as workflow artifacts. The dependent
`Publish tested images` job downloads those artifacts from the same run,
installs a checksum-pinned crane binary, and promotes their bytes to GHCR. It
does not rebuild the application. Each platform’s architecture, source revision,
and version label must match before version tags are written.

`make release` creates a multi-platform index and the `sha-<revision>` and
`<YYYYMMDD.N>` tags. The job computes that version for the pushed revision and
fails if the revision is not on main. The version must equal the label the
tested images carry, so an image built from a dirty tree, which carries a `-dev`
label, receives neither tag. `make release` refuses to replace either existing
tag with a different digest. A rerun with identical artifacts is allowed.
Staging tags identify the workflow run and attempt; retain the referenced
manifests because the release index uses their digests. Do not use mutable tags
for deployment.

Merging to main is the publication action: the tip of each push that passes the
gate is published, documentation changes included. Pull requests and other
branches build and test images but cannot publish. Qualification therefore gates
operation, not publication. Controllers default to `write_mode: disabled`, and a
deployment enables writes for a controller only after the required hardware
qualification. Commits that are not the tip of a push, whose queued run GitHub
replaces, or whose gate fails get no image, so version numbers are not
contiguous. They increase along main as long as committer dates do not go
backwards.

## Updates and rollback

The [Renovate configuration](../.github/renovate.json5) uses the shared
best-practice presets, plain commit messages, and GitHub platform automerge.
Non-major updates and development dependencies, including Python dependency
groups and build tooling, may automerge only behind required checks. Docker
digests, GitHub actions, Python packages, and Bun-managed tools are covered by
their standard managers.

GitHub must enforce all four check names above on the protected main branch and
have native auto-merge enabled before automerge is activated. This repository is
private and native auto-merge is enabled. The active main ruleset currently
requires `Lint and coverage` and `Workflow security`; both container checks
still need to be added. The session's GitHub proxy rejects ruleset updates as an
unsupported API operation, so that change requires external administration or
proxy support. The release workflow's dependency gate already requires all four
checks. Enable Renovate installation and platform automerge after the main
ruleset enforces the same checks.

Dependency updates run the full gate. TinyTuya updates also require review of
retry/timeout behavior and a physical-controller smoke test. Renovate does not
automerge them, because merging to main publishes: run the smoke test with the
pull request’s image, which the container jobs export as a workflow artifact,
before merging. Update the crane version and its two release-archive checksums
together; checksum mismatches fail the tool install.

Keep the previous image digest and its compatible private configuration/secret
references. To roll back, stop and remove the current container, restore that
configuration, and run the previous digest with the same read-only mounts. Never
run both versions against the same controller at once. The restarted bridge
establishes a fresh MQTT session and controller baseline; there is no
desired-state database or stored command queue to restore or replay.
