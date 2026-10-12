# Implementation status

The [implementation plan](implementation-plan.md) remains the acceptance
contract. This file records implementation evidence and remaining work without
changing its requirements.

| Phase                                   | Current evidence                                                                                                                                                                                                                    | Remaining                                                                                                                                                  |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Controller and dependency qualification | Supplied manual reviewed; Python 3.15.0 and TinyTuya 1.20.0 selected from current stable releases                                                                                                                                   | TCP harness, decoded write counts, retry qualification, pairing runbook, actual hardware identity/protocol/map, deployed MQTT 5 qualification              |
| Foundation and profile                  | Frozen strict JSON configuration with secret references; typed state/command/result model; candidate dual-zone decoding and command encoding; validation CLI; locked uv/Bun dependencies, Make, Lefthook, and lint/type/coverage CI | Read-only inspection CLI and measured hardware capabilities                                                                                                |
| Controller worker                       | Pending                                                                                                                                                                                                                             | Thread ownership, bounded queues, synchronization, push/poll/heartbeat maintenance, deadlines, confirmation, reconnects, shutdown, and adapter tests       |
| MQTT bridge                             | Pending                                                                                                                                                                                                                             | MQTT 5 contract, subscriptions, retained snapshots, result routing, duplicates, session heartbeats, publication failures, and real-Mosquitto process tests |
| Operations and release                  | Pending                                                                                                                                                                                                                             | Structured logging, metrics, HTTP/SSE, Distroless images, smoke tests, release gating, Renovate, and deployment/troubleshooting documentation              |
| Hoboken                                 | Pending                                                                                                                                                                                                                             | Read current instructions, implement mattress source/accessories, enforce TypeScript gates, and test Home/Siri on hardware                                 |
| Offline acceptance                      | [Hardware record](hardware-acceptance.md) created with explicit pending results                                                                                                                                                     | Functional matrix, failure recovery, and seven-day offline soak                                                                                            |

## Shared configuration differences

Ruff starts from the current
[shared configuration](https://github.com/shields/right-answers/blob/main/.ruff.toml).
`CPY001` is disabled because this session’s repository instructions prohibit
adding copyright or authorship information. Existing notices in adopted shared
files are preserved. Test assertions and literal expected values have targeted
exclusions for `S101` and `PLR2004`.

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

The profile deliberately has no firmware write-order assumption. Command
encoding describes requested DPs only; production write policy will follow
measured capabilities. The manual and candidate mapping do not establish
hardware compatibility.
