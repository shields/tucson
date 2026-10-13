# Controller worker

Each physical controller has one dedicated owner thread. The device factory,
connection attempts, reads, writes, heartbeats, and close operations run on that
thread. Both configured zones share its socket and command queue. The asyncio
coordinator reads immutable snapshots and exchanges requests/results through a
bounded mailbox; it never calls a device method.

## State and freshness

The worker exposes disconnected, connecting, synchronizing, online, and stopping
states. A connection starts an empty baseline for its generation, preserving the
previous reported snapshot for diagnostics. Partial reports merge only within
that baseline. Online requires known power, heat level, and preheat on both
zones. Unknown optional telemetry remains unknown.

Complete reports refresh the full-status timestamp, whether received during a
query or as an unsolicited report. Partial reports update traffic time and
values without refreshing the full-status age. No countdown is decremented
between reports. Invalid required fields take the controller offline; invalid
optional fields remain visible and block commands dependent on those fields.

A new MQTT session invalidates queued/pending work and the usable baseline. The
owner closes the existing connection and establishes a fresh device generation,
discarding buffered reports from the prior session before synchronization. A
write already in progress may have taken effect; its result is unconfirmed, and
it is not replayed.

## Commands and maintenance

The pending queue holds 16 requests per physical controller. A second limit of
17 outstanding requests includes the active request and results not yet consumed
by the coordinator. Completion delivery cannot grow without bound or block the
owner thread when MQTT stalls; additional admission returns `queue_full`.

Commands must begin within two seconds and have an eight-second confirmation
deadline from receipt. Each step rechecks state, generation, session, required
fields, and deadlines. Only fresh reports after the final write can confirm the
complete request. Empty ACKs and a prior cached match do not confirm it. An
unconfirmed request schedules state readback without rollback or replay; late
reports update state while preserving the terminal result.

The worker processes reports after each write and prioritizes scheduled
heartbeats before refresh requests. Heartbeats run every 10 seconds, full status
queries every 30 seconds, and required state becomes stale after 90 seconds
without refresh. Reconnect backoff doubles from one to 60 seconds, with up to
25% injected jitter and a total delay capped at 60 seconds. It resets only after
synchronization succeeds.

## Write qualification

Controller configuration adds `write_mode`:

| Value      | Behavior                                                                       |
| ---------- | ------------------------------------------------------------------------------ |
| `disabled` | Default; report state and reject writes with `write_disabled`.                 |
| `multi_dp` | Send the entire validated command in one packet through the qualified adapter. |

Hardware testing on the PP21KQ verified `multi_dp`, including power, timer, and
level in one packet. Before writing, the worker decides from fresh state what to
send. A level without power for a side that is off gains an explicit power-on
ahead of the level, because the firmware powers a side on for any level write
and only an explicit power write starts the presence-confirmation window. A
timer alone for a side that is off is rejected with `zone_off`, because the
firmware ignores it. Confirmation checks the command as extended, so the
reported power must match as well.

Selecting a mode asserts the write behavior being qualified on that controller.
Production use requires the hardware record to verify the chosen mode, each
writable field, supported timer choices, and firmware side effects. The
simulator proves transport/application semantics and does not establish firmware
behavior. The example configuration leaves writes disabled.

## Shutdown and failure

Stop admission first, expire queued requests, and finish bounded operations.
Close runs on the owner thread, followed by a join with a 10-second bound.
Unexpected thread exceptions, close failures, and join-bound violations surface
as fatal errors to the coordinator. Threads are daemon threads so a violated
bound cannot prevent a fatal process exit. Ordinary dependency outages remain
offline states with error logs and bounded retries.

The deterministic suite uses fake time for deadlines, freshness, backoff, and
maintenance. TCP cases run the real adapter/worker on protocols 3.3, 3.4, and
3.5; count decoded writes through a write failure and reconnect; exercise
physical pushes; and show that stalled negotiation on one controller does not
block another. Stalled-negotiation shutdown also runs with the production
two-second socket timeouts.
