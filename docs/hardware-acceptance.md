# Hardware acceptance

Status: in progress. Read-only observations and physical-control tests from
2026-10-12 are recorded below. The bridge has sent no write to the controller
(only the Smart Life app and the physical controls changed its state), so
production compatibility remains unproven until this record is complete.

## Available evidence

The controller’s label reads style PP21KQ, 120 VAC, 360 W, FCC ID 2ANDL-WBR3, IC
23243-WBR3. The supplied manual is `Heated blanket_mattress pad_21EFM1.pdf`, the
Sunbeam Connected Heated Bedding user manual. Its FCC declaration names the same
FCC ID and IC as the label but does not name style PP21KQ, so the match rests on
that ID.

The manual describes independent A/B zones on queen and king bedding (the left
side of the bed is A and the right side is B), ten heat levels, and a rapid
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
24-hour setting. Writes remain unverified.

## Observations

| Item            | Value                                                                                                                                                                                          |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Date            | 2026-10-12                                                                                                                                                                                     |
| Controller      | Sunbeam style PP21KQ, dual zone                                                                                                                                                                |
| Firmware        | Main module V2.1.6 and MCU module V1.0.10, from the Smart Life Device Update screen; Auto Upgrade is off                                                                                       |
| Local protocol  | 3.3                                                                                                                                                                                            |
| Bridge host     | macOS with Python 3.15.0, TinyTuya 1.20.0, and bridge revision `f89af41`, using the read-only `inspect` command and a scratch watcher built on the same adapter                                |
| Network         | Bridge host and controller on one LAN with Internet access not blocked; the controller is paired with Smart Life, whose app was used during the tests; the DHCP reservation is not yet applied |
| Reported fields | Power, heat level, and preheat for both zones were present in every report; timers and countdowns were present too                                                                             |

### Observed DP map

| DP       | Meaning               | Observed values and notes                                                                                                                                   |
| -------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 14, 15   | Power, side A and B   | Boolean.                                                                                                                                                    |
| 20, 21   | Heat level, A and B   | `level_1` (the idle setting), `level_3`, `level_5`, and `level_10` (during preheat); levels 3 and 5 set on the controller reported `level_3` and `level_5`. |
| 24, 25   | Preheat, A and B      | Boolean.                                                                                                                                                    |
| 26, 27   | Auto-off, A and B     | `10h` by default; side A also reported `30m`, `1h30m`, `5h`, and `Stay_On` after changes in the app.                                                        |
| 28, 29   | Remaining seconds     | Resets to the selected duration when the timer changes, then falls by 60 once a minute while the side is on (36,000, 35,940, …).                            |
| 102, 103 | Preheat remaining     | 1,800 when preheat starts and 0 when it ends.                                                                                                               |
| 1, 4, 8  | Combined controls     | DPs 1 and 8 followed the selected zone’s power and preheat; DP 4 did not change when only the selected zone changed. Not used.                              |
| 13       | Fault bitfield        | 0 throughout.                                                                                                                                               |
| 104      | Presence confirmation | `User_Confirmation`; it became `45_Minutes_Before` when side A was turned on from the app.                                                                  |
| 105      | Selected zone         | `ZONE_A` or `ZONE_B`, following the side most recently operated on the controller or in the app.                                                            |

The profile does not use DPs 1, 4, 8, 13, 104, or 105.

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

## Qualification record

Record the date, physical model, controller firmware, actual protocol version,
zone orientation, bridge revision, Python/TinyTuya versions, and network
conditions. Sanitized fixtures must include this provenance and omit
device/account identifiers and real local keys.

| Requirement                                                               | Result                                                                                                                                                                                  |
| ------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Device identity, firmware, and local protocol                             | Verified: style PP21KQ, main module V2.1.6, MCU module V1.0.10, protocol 3.3                                                                                                            |
| A/B orientation and required reported fields                              | Partial: required fields are reported, and the controller’s A and B map to the A and B DPs. The manual puts A on the left of the bed; that is not yet checked against the physical bed. |
| Power and all ten levels independently on each side                       | Partial: power verified on both sides from the controller; levels 3 and 5 observed on A and level 5 on B, with level 1 as the idle setting. The full sweep needs writes.                |
| Every supported timer enum and `Stay_On` duration                         | Partial: side A reported `30m`, `1h30m`, `5h`, `Stay_On` (86,400 seconds), and `10h` from the app. The full list needs writes.                                                          |
| Level selection without implicit bridge power-on                          | Pending                                                                                                                                                                                 |
| Power-off/on preserves the selected level                                 | Partial: turning a side off at the controller preserved its level. Power-on from the bridge is pending.                                                                                 |
| Verified individual-write ordering or multi-DP behavior                   | Pending                                                                                                                                                                                 |
| Preheat activation, cancellation, completion, and side effects            | Partial: activation and cancellation verified on both sides, with the level reported as 10 during preheat. Completion is pending.                                                       |
| Physical controls reflected in reported state                             | Verified for power, heat level, and preheat on both sides                                                                                                                               |
| Timer expiry and remote presence-confirmation behavior                    | In progress: turning side A on from the app changed DP 104 to `45_Minutes_Before`; timer expiry, the one-hour window, and the cap at level 4 are pending                                |
| Cold boot, Wi-Fi, bridge, and broker interruption/recovery                | Pending                                                                                                                                                                                 |
| Home/Siri controls, inactive speed display, errors, and recovery          | Pending                                                                                                                                                                                 |
| Seven consecutive days with controller and bridge Internet access blocked | Pending                                                                                                                                                                                 |

For the soak, record start/end times, LAN firewall rules, interruptions
exercised, physical setting changes during outages, synchronization after
recovery, and any unexpected writes or degradation. Completion requires seven
consecutive days and the functional matrix above, as specified in the
[implementation plan](implementation-plan.md#hardware-acceptance-and-remaining-decisions).
