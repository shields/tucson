# Implementation status

The [implementation plan](implementation-plan.md) remains the acceptance
contract. This file records implementation evidence and remaining work without
changing its requirements.

| Phase                                   | Current evidence                                                                                                                                                                                                                                                    | Remaining                                                                                                 |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Controller and dependency qualification | Supplied manual reviewed; [TCP qualification](adapter-qualification.md) with decoded write counts on Python 3.15.0/TinyTuya 1.20.0; [pairing runbook](setup.md)                                                                                                     | Actual hardware identity/protocol/map, deployed MQTT 5 qualification                                      |
| Foundation and profile                  | Frozen strict JSON configuration with secret references; typed state/command/result model; candidate dual-zone conversion; validation/inspection CLI; locked dependencies; Make, Lefthook, lint/types, and 100% line/branch coverage                                | Measured hardware capabilities                                                                            |
| Controller worker                       | [Owner thread and state machine](controller-worker.md), bounded request/result admission, generation/session baselines, maintenance, confirmation, reconnects, and fatal shutdown/failure boundaries; deterministic and real-adapter TCP cases pass                 | Measured hardware capabilities                                                                            |
| MQTT bridge                             | Run CLI, MQTT 5/session/command/snapshot coordination, 31 deterministic bridge cases, and 10 real-Mosquitto/controller process cases including broker persistence, command replay prevention, independent controller faults, and shutdown                           | Deployed-broker qualification                                                                             |
| Operations and release                  | JSON logging/redaction, signals, metrics, HTTP/SSE/status supervision; pinned non-root Distroless images, per-platform smoke cases, artifact promotion/immutable-tag guards, same-revision CI release dependencies, Renovate, and deployment/troubleshooting guides | Native CI and full online audit, repository required-check/auto-merge prerequisite, Renovate installation |
| Hoboken                                 | Shared TypeScript tooling and coverage gates implemented in commit `0a9c84c`; 591 tests pass in the pinned screenshot image with zero pixel differences and configured 100% coverage thresholds; 11 real HAP/MQTT round-trip cases pass on Node 24                  | Implement mattress source/accessories and command confirmations; test Home/Siri on hardware               |
| Offline acceptance                      | [Hardware record](hardware-acceptance.md) created with explicit pending results                                                                                                                                                                                     | Functional matrix, failure recovery, and seven-day offline soak                                           |

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
audit, including online checks. This environment’s GitHub API proxy rejects the
advisory endpoint with “unsupported GitHub API operation”; the full online audit
remains unverified here and must pass in CI before release.

JSON configuration uses the standard library’s duplicate-key check and
Pydantic’s strict, frozen models. Configuration and JSON errors omit supplied
values and secret-file contents. Controller zones are ordered A then B; this
records protocol side identifiers while physical orientation remains a
qualification requirement.

Writes default to disabled. The worker supports explicit multi-DP and individual
level-before-power policies for qualification; production selection follows
measured capabilities. The manual and candidate mapping do not establish
hardware compatibility.
