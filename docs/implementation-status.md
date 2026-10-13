# Implementation status

The [implementation plan](implementation-plan.md) remains the acceptance
contract. This file records implementation evidence and remaining work without
changing its requirements.

| Phase                                   | Current evidence                                                                                                                                                                                                                                                                      | Remaining                                                       |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Controller and dependency qualification | Supplied manual reviewed; [TCP qualification](adapter-qualification.md) with decoded write counts on Python 3.15.0/TinyTuya 1.20.0; [pairing runbook](setup.md); [hardware record](hardware-acceptance.md) of identity, protocol 3.3, the DP map, timers, preheat, and write behavior | Commands run by the daemon, deployed MQTT 5 qualification       |
| Foundation and profile                  | Frozen strict JSON configuration with secret references; typed state/command/result model; candidate dual-zone conversion; validation/inspection CLI; locked dependencies; Make, Lefthook, lint/types, and 100% line/branch coverage                                                  | Measured hardware capabilities                                  |
| Controller worker                       | [Owner thread and state machine](controller-worker.md), bounded request/result admission, generation/session baselines, maintenance, confirmation, reconnects, and fatal shutdown/failure boundaries; deterministic and real-adapter TCP cases pass                                   | Measured hardware capabilities                                  |
| MQTT bridge                             | Run CLI, MQTT 5/session/command/snapshot coordination, 32 deterministic bridge cases, and 10 real-Mosquitto/controller process cases including broker persistence, command replay prevention, independent controller faults, and shutdown                                             | Deployed-broker qualification                                   |
| Operations and release                  | JSON logging/redaction, signals, metrics, HTTP/SSE/status supervision; pinned non-root Distroless images, per-platform smoke cases, artifact promotion/immutable-tag guards, same-revision CI release dependencies, Renovate, and deployment/troubleshooting guides                   | Required container checks, Renovate installation                |
| Hoboken                                 | Mattress source, accessories, MQTT 5 sessions, configuration, metrics/status, and combined qualification pushed through `f526fd1`; native CI passes; 792 tests pass in the pinned screenshot image, 20 paired HAP/MQTT cases pass, and three complete controller-path cases pass      | Actual Home/Siri qualification                                  |
| Offline acceptance                      | [Hardware record](hardware-acceptance.md) with observations, adapter write results, and explicit pending results                                                                                                                                                                      | Functional matrix, failure recovery, and seven-day offline soak |

The
[CI run for revision `301e0da`](https://github.com/shields/tucson/actions/runs/38293184929)
passed lint, application coverage, integration, the full online workflow audit,
and native amd64 and arm64 image builds and smoke tests. Image publication was
skipped for this branch run, as required by the release policy.

The active `main` ruleset requires lint/coverage and workflow security.
Container checks still need to be added. Repository auto-merge is enabled, as
verified through the repository API. The VM’s GitHub proxy rejects the ruleset
update API as an unsupported operation; required container checks need an
external update or proxy support.

Hoboken’s reviewed commits through `f526fd1` are pushed to the
[`task/f39d6bee-3dfc-4a1a-a8db-4e8b20ec9eb7/sunbeam-homekit` branch](https://github.com/shields/hoboken/tree/task/f39d6bee-3dfc-4a1a-a8db-4e8b20ec9eb7/sunbeam-homekit).
Tucson’s task branch is also pushed.

The
[Hoboken CI run for revision `f526fd1`](https://github.com/shields/hoboken/actions/runs/38338739162)
passed lint, unit tests, paired HomeKit/MQTT integration, the screenshot test,
and the production image build. The
[workflow-security run](https://github.com/shields/hoboken/actions/runs/38338739105)
also passed. Image publication was skipped for the branch run.

Hoboken's integration suite is committed in `316e995`. Its nine mattress cases
and the existing eleven WLED cases pass together on Node 24.21.0. The mattress
cases use a pinned Mosquitto 2.1.2 build and actual paired HAP HTTP connections.
They cover both zones, off precedence, stateful preheat, rejected and retained
results, session changes, the full HAP write deadline, and broker restart
without command replay. Workflow checks and the broker's Renovate extraction
pass locally, with native RE2 enabled for validation. These cases simulate the
MQTT controller contract; they do not exercise Sunbeam hardware.

`make homekit` additionally exercises the actual Tucson and Hoboken processes
together against the TCP simulator on protocols 3.3, 3.4, and 3.5. It checks
decoded writes, physical-style state pushes, and shutdown-driven HomeKit
unavailability. This target requires both checkouts and Node.js 24; it does not
silently skip missing prerequisites.

## Shared configuration differences

Ruff starts from the current
[shared configuration](https://github.com/shields/right-answers/blob/main/.ruff.toml).
`CPY001` is disabled because this session’s repository instructions prohibit
adding copyright or authorship information. Existing notices in adopted shared
files are preserved. Test assertions and literal expected values have targeted
exclusions for `S101` and `PLR2004`.

The adapter and its test double preserve TinyTuya’s public method names, with a
targeted `N802` exclusion. Simulator instrumentation uses an existing decoded
control handler and transport socket for fault injection, with a file-specific
`SLF001` exclusion; application code uses only TinyTuya’s public API.

Local `make lint` runs zizmor’s pedantic audit offline so hooks require no
GitHub credentials. The separate required CI workflow-security job runs the full
audit, including online checks.

JSON configuration uses the standard library’s duplicate-key check and
Pydantic’s strict, frozen models. Configuration and JSON errors omit supplied
values and secret-file contents. Controller zones are ordered A then B; this
records protocol side identifiers while physical orientation remains a
qualification requirement.

Writes default to disabled. The worker sends each command as one multi-DP write
once `write_mode` is `multi_dp`, which hardware testing verified on the PP21KQ.
The manual and candidate mapping do not establish hardware compatibility.
