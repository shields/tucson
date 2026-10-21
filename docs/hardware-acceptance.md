<!--
Copyright © 2026 Michael Shields

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Hardware acceptance

Status: in progress. Read-only observations and physical-control tests from
2026-10-12, power, level, timer, and preheat writes through the adapter on
2026-10-13, and timer expiry, the presence window, preheat on side B, and every
level on both sides through the adapter on 2026-10-18 are recorded below. The
bridge daemon ran a command sequence against the controller on 2026-10-13,
recorded under [Bridge daemon smoke test](#bridge-daemon-smoke-test), but
production compatibility remains unproven until this record is complete.

## Available evidence

The controller’s label reads style PP21KQ, 120 VAC, 360 W, FCC ID 2ANDL-WBR3, IC
23243-WBR3. The supplied manual is `Heated blanket_mattress pad_21EFM1.pdf`, the
Sunbeam Connected Heated Bedding user manual. Its FCC declaration names the same
FCC ID and IC as the label but does not name style PP21KQ, so the match rests on
that ID.

The manual describes independent A/B zones on queen and king bedding (the left
side of the bed is A and the right side is B), 10 heat levels, and a rapid
preheat that holds the high setting for 30 minutes before returning to the last
heat setting. The wired controller has no timer control. The 10-hour default
auto-off changes only in the Sunbeam app, from 30 minutes to 24 hours, with
“Stay On – 24 hours” as the longest choice. Turning the bedding on from the app
requires pressing the controller’s check button within one hour to confirm
presence; otherwise a heat setting of level 5 or above is lowered to level 4
(manual pages E6, E9, E10, and E15).

The candidate DP map and timer enum table come from
[tuya-local’s Sunbeam definition](https://github.com/make-all/tuya-local/blob/main/custom_components/tuya_local/devices/sunbeam_dual_bedwarmer.yaml).
The read-only observations below agree with it for every mapped field, and the
controller reports `Stay_On` as 86,400 seconds, consistent with the manual’s
24-hour setting. Writes through the adapter are recorded under
[Write behavior](#write-behavior), the daemon’s first run under
[Bridge daemon smoke test](#bridge-daemon-smoke-test), the timer expiry and
presence window runs under [Live runs of 2026-10-18](#live-runs-of-2026-10-18),
and the level sweep under
[Level sweep of 2026-10-18](#level-sweep-of-2026-10-18).

Tuya’s cloud data model for this controller (the “thing model” returned by
`/v2.0/cloud/thing/<device id>/model` through TinyTuya’s `Cloud` class) declares
more than the standard instruction set that TinyTuya’s wizard saves:

- DP 13 is a read-only bitmap of 13 bits, labeled `e1` through `e13`.
- DPs 26 and 27 are enums of 21 values: 30 minutes to 10 hours in 30-minute
  steps, plus `Stay_On`. Side A’s list spells 7.5 hours `7h30`, while side B’s
  spells it `7h30m`. Hardware writes confirmed both spellings.
- DPs 28 and 29 accept 0 to 86,400 seconds and are declared read-write. DPs 102
  and 103 are read-only, 0 to 1,800 seconds.
- DP 101, `Schedule`, is a raw read-write value of up to 128 bytes. It holds the
  app’s on-device schedules, which run on the controller even when it is
  offline. No report has included it, so no schedule is set.
- DP 104, `Confirmation`, is a read-only enum: `User_Confirmation`,
  `45_Minutes_Before`, `15_Minutes_Left`, `One_Hour_Later`, and `No_Operation`.
- DP 105, `Zone`, is a read-write enum: `NO_ZONE`, `ZONE_A`, `ZONE_B`, and
  `DUAL_ZONE`. The manual’s “Both” button in the app presumably selects the
  last.

## Observations

| Item            | Value                                                                                                                                                                                                                                                                  |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Date            | 2026-10-12                                                                                                                                                                                                                                                             |
| Controller      | Sunbeam style PP21KQ, dual zone                                                                                                                                                                                                                                        |
| Firmware        | Main module V2.1.6 and MCU module V1.0.10, from the Smart Life Device Update screen; Auto Upgrade is off                                                                                                                                                               |
| Local protocol  | 3.3                                                                                                                                                                                                                                                                    |
| Bridge host     | macOS with Python 3.15.0, TinyTuya 1.20.0, and bridge revision `f89af41`, using the read-only `inspect` command and a scratch watcher built on the same adapter                                                                                                        |
| Network         | Bridge host and controller on one LAN with Internet access not blocked; the controller is paired with Smart Life, whose app was used during the tests; the DHCP reservation is applied and the controller answered at the reserved address during the 2026-10-13 tests |
| Reported fields | Power, heat level, and preheat for both zones were present in every report; timers and countdowns were present too                                                                                                                                                     |

### Observed DP map

| DP       | Meaning               | Observed values and notes                                                                                                                                                                                                           |
| -------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 14, 15   | Power, side A and B   | Boolean.                                                                                                                                                                                                                            |
| 20, 21   | Heat level, A and B   | `level_1` (the idle setting), `level_3`, `level_4` (after the one-hour presence rule), `level_5`, and `level_10` (during preheat); levels 3 and 5 set on the controller reported `level_3` and `level_5`.                           |
| 24, 25   | Preheat, A and B      | Boolean.                                                                                                                                                                                                                            |
| 26, 27   | Auto-off, A and B     | `10h` by default. Writes verified all 21 values on both sides; side A spells 7.5 hours `7h30` and side B `7h30m`.                                                                                                                   |
| 28, 29   | Remaining seconds     | Resets to the selected duration when the timer changes, then falls by 60 once a minute while the side is on (36,000, 35,940, …).                                                                                                    |
| 102, 103 | Preheat remaining     | 1,800 when preheat starts and 0 when it ends.                                                                                                                                                                                       |
| 1, 4, 8  | Combined controls     | DP 1 was true while either side was on, DP 8 while side B preheated, and DP 4 repeated the level last set on either side. Not used.                                                                                                 |
| 13       | Fault bitmap          | 13 bits with no known meanings; 0 throughout. Reported as `fault_code`.                                                                                                                                                             |
| 104      | Presence confirmation | `User_Confirmation` while idle. Turning side A on from the app stepped it through `45_Minutes_Before`, `15_Minutes_Left`, and `One_Hour_Later` (see below); the check button and a LAN power-off both restored `User_Confirmation`. |
| 105      | Selected zone         | `ZONE_A` or `ZONE_B`, following the side most recently operated on the controller or in the app.                                                                                                                                    |

The 2026-10-18 logs show how the combined DPs relate to the two sides. DP 1 was
true whenever either side was on (A alone, B alone, or both) and false when both
were off, and it stayed true when side A’s timer turned A off while B was on. DP
8 was true while side B was preheating and false otherwise; no preheat on side A
is in those logs, so whether side A sets it is not recorded there. DP 4 repeated
the level most recently set on either side, including the level the one-hour
rule lowered, and did not show the preheat level. None of them followed DP 105,
which read `ZONE_A` in all 88 status queries of the probe, after side B had been
turned on and set to preheat through the adapter, so writes to side B over the
LAN did not select it.

The profile does not use DPs 1, 4, 8, 104, or 105. DP 13 is published as
`fault_code` without interpretation and does not affect availability.

### Behavior

- Changes made on the controller arrive as unsolicited reports, with related DPs
  arriving within a fraction of a second of one another.
- Turning a side off at the controller preserved its selected level (level 3 on
  side A and level 5 on side B).
- Starting preheat on a powered side sets its preheat DP and reports 1,800
  seconds remaining. The reported heat level becomes `level_10` for the whole
  preheat. A second press cancels it and restores the previous level. This was
  repeated twice on side A and once on side B.
- The Smart Life app and the local connection operated at the same time without
  disconnecting one another.
- On 2026-10-18 a second local session connected while the live test held its
  own (see [Live runs of 2026-10-18](#live-runs-of-2026-10-18)). The second
  session read status every 10 seconds for 90 seconds, received pushes, and was
  not disconnected, and the live test’s polls kept completing every 30 seconds,
  so this controller accepted a second simultaneous local session. The live test
  reconnects without logging, so its own log cannot show whether the first
  session dropped.
- A probe on a second session during run 2 of the live test sent 88 DP queries
  over three minutes. For 86 of them, the reply (command 10, echoing the query’s
  sequence number) arrived first. For two, a countdown push (command 8, sequence
  number 0) arrived first, TinyTuya’s `status()` returned its single DP (103
  or 29) as the result, and the reply followed within 5 to 8 ms. No reply was
  missing.

### Remote activation and presence confirmation

Side A was turned on from the Smart Life app at 21:32:35 and raised to level 5
from the app at 21:35:34. The check button on the controller was not pressed.

| Time after power-on | Reported change                                                                                                                                             |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0 minutes           | DP 104 became `45_Minutes_Before`.                                                                                                                          |
| 45 minutes          | DP 104 became `15_Minutes_Left`.                                                                                                                            |
| 60 minutes          | DP 104 became `One_Hour_Later`, and within 0.11 s the level dropped from `level_5` to `level_4` on DP 20 (side A), DP 4 (combined), and DP 21 (**side B**). |

Side B was off and had not been activated remotely, yet its stored level was
lowered as well. The cap applies to every zone’s selected level, and the
controller reports it as ordinary state with no separate flag. Pressing the
check button returned DP 104 to `User_Confirmation`, after which level 6 could
be set. An explicit power write over the LAN also started the window, and the
cap followed an hour later (see
[Live runs of 2026-10-18](#live-runs-of-2026-10-18)). A level or preheat write
that turned a side on started no window, and a level write that did so held its
level past one hour (see [Write behavior](#write-behavior)).

### Write behavior

On 2026-10-13 a scratch script wrote through the adapter’s `write` method, with
the side being tested on at low levels for a few seconds, or up to about 80
seconds in the timer tests, preheat cancelled immediately, and both sides off at
the end, except for the long-running preheat and level observations below.
Writes were acknowledged by unsolicited reports within about 0.3 seconds.

- **Timers:** all 21 values were echoed with the matching remaining time on both
  sides, with `Stay_On` at 86,400 seconds. Side A accepts `7h30` and rejects
  `7h30m`, and side B accepts `7h30m`.
- **An unrecognized timer value is hazardous.** Writing `7h30m` to side A left
  its setting unchanged and set the remaining time to 86,400 seconds. A valid
  write restored it.
- **A timer for a side that is off is ignored,** with no report. A single packet
  with power, timer, and level applied all three, as did power followed by the
  timer. Turning a side off resets its timer setting to `10h`.
- **The remaining time is derived from the setting.** Writing 15 seconds was
  echoed, but the next once-a-minute tick restored the setting minus 60 seconds
  and the side stayed on, so arbitrary durations are not possible.
- **A level write turns a side on,** even in the same packet as power off. Power
  off alone turns it off, and the selected level survives.
- **Preheat** behaves as it does from the controller. Starting it on side A,
  which was off, turned the side on at level 10 and reported 1,800 seconds
  remaining. It ended exactly 30 minutes later: the preheat DP cleared, the
  level returned to the side’s stored level (level 1 here) within 35 ms, and the
  side stayed on. It was still on eight minutes later, when the test turned it
  off. The side’s auto-off timer counted down through the preheat and kept
  running afterwards, and a timer write during preheat was accepted. Preheat
  started no presence window. Side B is covered under
  [Live runs of 2026-10-18](#live-runs-of-2026-10-18).
- **The presence window starts on an explicit power write.** Writing power on
  over the LAN set DP 104 to `45_Minutes_Before`, and power off returned it to
  `User_Confirmation`. Turning a side on through a level or preheat write
  started no window. Side B, turned on by a bare level write to level 5, held
  that level for 62 minutes with DP 104 at `User_Confirmation` throughout, so
  the cap did not apply either. The worker still sends an explicit power-on
  ahead of a level for a side that is off, which leaves the firmware’s presence
  check in force; the cap after a window started that way is recorded under
  [Live runs of 2026-10-18](#live-runs-of-2026-10-18).

### Bridge daemon smoke test

On 2026-10-13 the daemon at revision `593bf00` ran with `write_mode: multi_dp`
on macOS with Python 3.15.0 and TinyTuya 1.20.0, against a private local broker
and the real controller on the same LAN with Internet access not blocked. An
MQTT client sent a short sequence of commands, each side on for a few seconds at
low levels. All checks passed, every command that reached the controller was
confirmed, and both sides ended off with their previous levels restored. The
daemon exited with code 0 on SIGTERM.

- A level without power for a side that was off was sent as one packet with
  power first, `{14: true, 20: level_2}`, and confirmed with the side on.
- Power off alone was confirmed, and the side kept its level.
- A timer for a side that was off was rejected with `zone_off`, and power off
  combined with a level was rejected with `invalid_command`. Neither wrote
  anything, and the side stayed off.
- Power, level, and timer in one command were applied in one packet and
  confirmed.
- Side A’s 7.5 hours was sent as `7h30` and side B’s as `7h30m`, and both were
  confirmed.
- Each `command_write` log event recorded the exact packet before it was sent.

### Live runs of 2026-10-18

On 2026-10-18 (local date; the logs are in UTC, from 23:01 that day to 00:42 the
next) two back-to-back runs wrote to the controller through the adapter’s
`write` method. They ran from the published image
`ghcr.io/shields/tucson:20261018.4` (revision `25d608b`) with Python 3.15.0 and
TinyTuya 1.20.0. A script sent a heartbeat every 10 seconds, polled status every
30 seconds, and logged every change except the countdowns. Both sides were off
at the start, with stored levels of 6 on side A and 4 on side B. The controller
and the test host kept their Internet access throughout the 2026-10-18 runs,
including the level sweep below.

**Run 1 (62 minutes).** The test wrote the packet the worker sends for a
power-on at level 5 to side B, `{15: true, 21: level_5}`, the only write in
either run with more than one DP. A few seconds later it wrote a 1h30m auto-off
to side B as a backstop, turned side A on with a level 1 write, and gave side A
a 30-minute timer. It then watched both sides for 62 minutes.

| Time after the power write | Reported change                                                                                                                                                            |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0 minutes                  | DP 104 became `45_Minutes_Before` within 0.4 s of the write, with the power and level reports.                                                                             |
| 45 minutes                 | DP 104 became `15_Minutes_Left`.                                                                                                                                           |
| 60 minutes                 | DP 104 became `One_Hour_Later`, and within 22 ms the level on DP 21 (**side B**) dropped from `level_5` to `level_4` and DP 4 (combined) rose from `level_1` to `level_4`. |
| About 62 minutes           | The test turned side B off. DP 104 returned to `User_Confirmation`, and DP 27 returned to `10h`.                                                                           |

The one-hour cap followed a power-on written over the LAN, as it did after one
from the app. Side B’s 1h30m backstop was still counting down when the test
turned the side off. Side A was at level 1 by then, so whether a window started
over the LAN also lowers the other side’s level was not tested.

Side A, turned on by the level 1 write, turned itself off 30 minutes after the
timer write, to within a second; DP 28 had counted down from 1,800 seconds, by
60 each minute. When the side turned off, DP 26 returned to `10h` within 5 ms,
DP 28 read 36,000, and the level stayed `level_1`.

**Run 2 (38 minutes).** The test wrote `level_1` to side B, which turned the
side on, then wrote power off and confirmed that side B was off at level 1. It
wrote preheat on (`{25: true}`) and, two seconds later, a 1h30m backstop.

- The level 1 write turned side B on and started no presence window.
- Preheat written while side B was off turned it on at level 10, set DP 8 and DP
  25, and started DP 103 at 1,800 seconds.
- Preheat ended 30 minutes after it started (29 minutes 59.7 seconds between the
  start and end reports): DP 25 became false, the level on DP 21 returned from
  `level_10` to the stored `level_1` within 12 ms, and DP 8 cleared. Side B
  stayed on, and the test turned it off eight minutes later.
- Side B’s 1h30m auto-off counted down through the preheat and kept running
  afterwards, from 5,400 seconds to 3,180 when the test turned the side off.
- DP 104 stayed `User_Confirmation` through the whole run, so neither the level
  1 write nor the preheat from off started a window. These are the only cases
  tried in this run. The power write with level 5 in run 1 started a window, and
  the level 1 write and the preheat from off did not.

At the end of the runs both sides were off, with the stored levels restored to
their starting values: level 6 on side A and level 4 on side B.

### Level sweep of 2026-10-18

Later that evening (01:13 to 01:49 UTC) a script wrote every level to each side
through the adapter’s `write` method. It ran from the published image
`ghcr.io/shields/tucson:20261018.4`, while the deployed bridge, with write mode
disabled, held its own session. In three passes (levels 7 to 9, then 10, then 1
to 6) it stepped side A through the pass’s levels in order, waiting for the
controller to report each level before writing the next, and then turned side A
off; it then did the same for side B. The first level written to each side in a
pass turned that side on, and the later levels found it already on. Every level
write was reported back within a second, and power off kept the last level
written. Each pass ended by writing the stored levels back and turning both
sides off, leaving level 6 on side A and level 4 on side B.

## Qualification record

Record the date, physical model, controller firmware, actual protocol version,
zone orientation, bridge revision, Python/TinyTuya versions, and network
conditions. Sanitized fixtures must include this provenance and omit
device/account identifiers and real local keys.

| Requirement                                                               | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Device identity, firmware, and local protocol                             | Verified: style PP21KQ, main module V2.1.6, MCU module V1.0.10, protocol 3.3                                                                                                                                                                                                                                                                                                                                                                          |
| A/B orientation and required reported fields                              | Partial: required fields are reported, and the controller’s A and B map to the A and B DPs. The manual puts A on the left of the bed and B on the right; which side of the bed each zone heats is not yet recorded.                                                                                                                                                                                                                                   |
| Power and all 10 levels independently on each side                        | Verified: power on both sides from the controller and the adapter, and every level from 1 to 10 written through the adapter and read back on each side (see [Level sweep of 2026-10-18](#level-sweep-of-2026-10-18)).                                                                                                                                                                                                                                 |
| Every supported timer enum and `Stay_On` duration                         | Verified: all 21 values were echoed with the matching remaining time on both sides, including `Stay_On` at 86,400 seconds. Side A spells 7.5 hours `7h30` and side B `7h30m`; an unrecognized value is ignored and resets the remaining time to 86,400 seconds.                                                                                                                                                                                       |
| Level selection for a side that is off                                    | Verified: the firmware turns a side on when a level is written, even with power off in the same packet, so a level cannot be selected while a side stays off. The worker therefore sends a level for a side that is off with an explicit power-on.                                                                                                                                                                                                    |
| Power-off/on preserves the selected level                                 | Verified: power off from the adapter kept the level, and power on alone resumed at it.                                                                                                                                                                                                                                                                                                                                                                |
| Verified individual-write ordering or multi-DP behavior                   | Verified: `multi_dp` applies power, timer, and level in one packet. In the smoke test the daemon sent every command that reached the controller as one packet. A timer must accompany or follow power because the firmware ignores it for a side that is off, and a level write turns the side on.                                                                                                                                                    |
| Preheat activation, cancellation, completion, and side effects            | Partial: activation and cancellation verified from the controller on both sides and from the adapter on side A. Preheat written from off turned either side on at level 10 with 1,800 seconds remaining, started no presence window, and ended 30 minutes later by returning to the stored level with the side still on and the auto-off timer running through it. Cancelling preheat through the adapter on side B is pending.                       |
| Physical controls reflected in reported state                             | Verified for power, heat level, and preheat on both sides                                                                                                                                                                                                                                                                                                                                                                                             |
| Timer expiry and remote presence-confirmation behavior                    | Partial: the one-hour rule lowered level 5 to level 4 after an app activation (both zones) and after a LAN power-on at level 5 (side B); the check button or power-off returned DP 104 to `User_Confirmation`. Level writes (level 5 held for 62 minutes) and a preheat from off started no window. A 30-minute timer expired on side A, turning it off and resetting its timer setting to `10h`. Expiry of other durations and on side B is pending. |
| Cold boot, Wi-Fi, bridge, and broker interruption/recovery                | Pending                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Home/Siri controls, inactive speed display, errors, and recovery          | Pending                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Seven consecutive days with controller and bridge Internet access blocked | Pending                                                                                                                                                                                                                                                                                                                                                                                                                                               |

For the soak, record start/end times, LAN firewall rules, interruptions
exercised, physical setting changes during outages, synchronization after
recovery, and any unexpected writes or degradation. Completion requires seven
consecutive days and the functional matrix above, as specified in the
[implementation plan](implementation-plan.md#hardware-acceptance-and-remaining-decisions).

## Zemismart BCD300FB qualification

Status: hardware observations on October 18, 2026 (local date). The user
identifies the hardware as a Zemismart BCD300FB Wi-Fi curtain motor, and TCP
port 6668 accepts connections at its reserved address. Tuya’s cloud product
metadata calls it “BCM700D WiFi,” in category `cl`; this name does not establish
compatibility with other motors using that product definition.

TinyTuya 1.20.0 authenticated with the newly retrieved local key using protocol
3.3, on Python 3.15.0. The user reports installed firmware V3.1.4 and V1.0.0
from Smart Life; the module labels were not supplied. The firmware-version API
request failed with code `28841101`. Device identity and local credentials are
stored privately and are excluded from this record.

The inspection host’s Python processes cannot connect directly to the LAN
address, while Apple’s `/usr/bin/nc` can. A loopback TCP relay using that
executable allowed TinyTuya to own framing and encryption without changing macOS
privacy settings. Internet access was available throughout these observations.
Several initial reads failed with TinyTuya error 904 after the peer closed the
socket; no control packet was sent on those attempts. Successful persistent
sessions continued to answer status requests and heartbeats. The intermittent
initial closure remains unexplained.

### Data model and observations

The full Tuya data model declares these properties:

| DP  | Code           | Type and range            | Declared access | Local observation                                    |
| --- | -------------- | ------------------------- | --------------- | ---------------------------------------------------- |
| 101 | `position`     | Integer, 0–100%, step one | Read/write      | Last completed position; writable destination        |
| 102 | `mach_operate` | Enum: `ZZ`, `FZ`, `STOP`  | Read/write      | `STOP`, including throughout position-based movement |
| 103 | `opposite`     | Boolean                   | Read/write      | Not reported; never written                          |

The user confirmed a clear path and authorized endpoint and intermediate
movement, followed by restoration of the initial position. Only DP 101 was
written. The user observed raw position 0 as fully open, 100 as fully closed,
and 50 as roughly halfway. Normalized positions therefore use `100 - raw`. The
initial raw position 46 was restored and read back at the end.

| Move, raw positions | Successful control ACK      | First destination report | Physical observation     |
| ------------------- | --------------------------- | ------------------------ | ------------------------ |
| 46 → 0              | Frame metadata not captured | About 16.5 seconds       | Fully open               |
| 0 → 100             | 53 milliseconds             | 32.8 seconds             | Fully closed             |
| 100 → 50            | 45 milliseconds             | 16.0 seconds             | Roughly halfway          |
| 50 → 46             | 43 milliseconds             | 3.5 seconds              | Restored value read back |

The captured acknowledgments have command 7, the same sequence number as the
control packet, return code zero, an empty payload, and a valid CRC. They arrive
before movement completes. This is a protocol acknowledgment, not a report of a
target datapoint. During each movement, status queries continued to return the
previous completed position with DP 102 at `STOP`. Completion produced an
unsolicited command-8 report containing only DP 101, followed by matching full
status responses. DP 102 is therefore not usable as motion telemetry for this
control path. Motion must remain null.

### Approved state policy

The existing curtain contract requires separate current and target positions,
both populated before availability, and fresh evidence of target acceptance
without waiting for arrival. This firmware does not report a separate target.
Mapping each DP 101 response to both fields would erase an acknowledged target
on every status poll during movement; waiting for DP 101 to equal the request
would exceed the eight-second confirmation deadline on longer moves.

The user approved a hardware-specific policy on October 18, 2026, to track the
last target accepted by a correlated successful control acknowledgment, while
current position remains the last device-reported position. Polls returning that
old position must not erase the target. An actual position change can reconcile
the target after arrival or an external control. On a new connection, the last
reported position initializes both fields and any remembered target is
discarded. HomeKit temporarily loses knowledge of movement across reconnects,
and position jumps when the motor reports completion. The `zemismart-bcd300fb`
profile implements this exception to the reported-target baseline. A transport
send result alone must never count as confirmation.

### Live HomeKit and reconnect test

The isolated Tucson daemon and Hoboken at revision `1f24915` ran with a local
Mosquitto 2.0.22 broker, Node.js 26.12.0, and a paired HAP HTTP client. The
client sent a normalized target of 80 from the initial 54. HomeKit acknowledged
the write before arrival and continued reporting current position 54. Tucson
then restarted while the curtain was moving. On reconnection, HomeKit observed
the fresh baseline of current/target 54; the motor continued its existing
movement and later reported 80. HomeKit then requested 54 and observed its
arrival before Tucson shut down and the accessory became unavailable.

The daemon logged exactly two control packets, `{"101":20}` and `{"101":46}`,
and two distinct confirmed requests. No movement was replayed after the restart,
and the initial position was restored. The paired client also verified that
`HoldPosition` was absent. This exercised the source changes recorded with this
acceptance entry (commit `ac201c1`); it was not a production deployment or an
actual Apple Home/Siri test. Internet access was available.

The TCP simulator independently covers immediate acknowledgments with delayed
position reports, reconnects without replay, and missing acknowledgment failure.
Unit tests cover stale or unrelated ACKs, invalid CRC/return codes, late ACKs,
unsupported protocols, and position validation. The scrubbed hardware schema and
trace are in `tests/fixtures/zemismart-bcd300fb.json`.

### Status reply matching

On October 19, 2026 (local date), read-only `inspect` succeeded against the
motor using the merged adapter at `27e9d9a`, Python 3.15.0, TinyTuya 1.20.0,
protocol 3.3, and the loopback relay described above. The working tree contained
other changes, but `tuya_client.py` was unchanged from that revision. Inspection
exited zero and returned `{"101":0,"102":"STOP"}`.

A separate connection, five subsequent status queries, and a reconnection all
returned the same datapoints. Each of those seven captured replies had command
10, echoed its query’s sequence number, had return code zero, and passed CRC
validation. The first connection used sequences one through six; reconnecting
started again at one. Writes were disabled, and no control packet was sent. This
verifies that the motor answers the current adapter’s status queries with the
matching command required by the
[adapter qualification](adapter-qualification.md#status-reply-matching).

The saved October 18 movement and observation captures also contain 95 status
reply frames, all with command 10. Those headers were recorded even though the
adapter used for the earlier live HomeKit run did not require command matching.

### Remaining qualification

The user deferred Internet-blocked testing. No physical controls are installed,
so physical-control updates cannot be exercised on this installation. Actual
Home/Siri use, cold-boot and Wi-Fi recovery, offline operation, and production
deployment remain pending. The profile is registered for explicit configuration
and defaults to disabled writes.

A short paired HomeKit run with the current adapter remains pending. The earlier
live HomeKit run used `ac201c1`, before status-reply matching; the October 19
verification exercised read-only inspection, polling, and reconnect.
