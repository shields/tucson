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
timestamps. The image records its source revision in an OCI label and
`TUYA_MQTT_REVISION`; startup and HTTP diagnostics report it. The Docker context
allows only the application, package metadata, lockfile, and README, keeping
private configuration and secret files out of the build.

Seven smoke cases verify runtime imports and the GCC library, UID/GID 65532, the
system CA store, read-only storage, mounted configuration/secret resolution,
sanitized validation failures, HTTP/SSE behavior, SIGINT/SIGTERM shutdown, and
real-Mosquitto command confirmation through simulated protocols 3.3, 3.4, and
3.5. Graceful shutdown must exit zero and publish both offline announcements.
The ordinary application gate separately enforces 100% line and branch coverage.

## Run on the LAN

Run one instance for each configured controller ownership set. Stop that
instance before read-only inspection or another program connects to the same
controller. Use the [setup guide](setup.md) to validate identities, protocol,
zone order, and write policy.

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

## Release gate

The `Checks` workflow runs on every branch push, pull request, and stable
version tag matching `v*.*.*`. Tag builds execute the entire gate again for that
exact source revision. Publication depends on:

- `Lint and coverage`, including application, real-library TCP, real-broker,
  integration, and wheel checks.
- `Workflow security`, with zizmor’s online pedantic audit.
- `Container (amd64)` and `Container (arm64)`, including all smoke cases.

These jobs have read-only repository permissions and no publication token. They
export the checked images as workflow artifacts. The dependent
`Publish tested images` job downloads those artifacts from the same run,
installs a checksum-pinned crane binary, and promotes their bytes to GHCR. It
does not rebuild the application. Each platform’s architecture and source
revision must match before version tags are written.

`make release` creates a multi-platform index and the `sha-<revision>` and
`v<major>.<minor>.<patch>` tags. It refuses to replace either existing tag with
a different digest. A rerun with identical artifacts is allowed. Staging tags
identify the workflow run and attempt; retain the referenced manifests because
the release index uses their digests. Do not use mutable tags for deployment.

Before tagging a release, update the project version and complete the required
controller qualification. Creating a tag is a publication action. The workflow
publishes only push events for version tags after all gates pass; pull requests
cannot publish images. Local promotion tests use an isolated loopback registry;
no release or image has been published to GHCR.

## Updates and rollback

The [Renovate configuration](../.github/renovate.json5) uses the shared
best-practice presets, plain commit messages, and GitHub platform automerge.
Non-major updates and development dependencies, including Python dependency
groups and build tooling, may automerge only behind required checks. Docker
digests, GitHub actions, Python packages, and Bun-managed tools are covered by
their standard managers.

GitHub must enforce all four check names above on the protected main branch and
have native auto-merge enabled before automerge is activated. This repository is
currently private, native auto-merge is disabled, and the rulesets API returns
HTTP 403 requiring GitHub Pro or public visibility. That account/visibility
prerequisite remains unresolved; the release workflow’s own dependency gate is
independent of it. Enable Renovate installation and platform automerge only
after required-check enforcement is available. No repository visibility or
account settings were changed during implementation.

Dependency updates run the full gate. TinyTuya updates also require review of
retry/timeout behavior and a physical-controller smoke test before deployment,
even if their dependency PR automerges. Update the crane version and its two
release-archive checksums together; checksum mismatches fail the tool install.

Keep the previous image digest and its compatible private configuration/secret
references. To roll back, stop and remove the current container, restore that
configuration, and run the previous digest with the same read-only mounts. Never
run both versions against the same controller at once. The restarted bridge
establishes a fresh MQTT session and controller baseline; there is no
desired-state database or stored command queue to restore or replay.
