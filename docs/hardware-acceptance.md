# Hardware acceptance

Status: pending hardware availability. No physical-controller results have been
recorded. Development and simulated-device testing can proceed; production
compatibility is unproven until this record is complete.

## Available evidence

The supplied manual is `Heated blanket_mattress pad_21EFM1.pdf`. Its filename is
an evidence identifier, not proof of the connected controller’s model or
firmware. The manual describes independent A/B zones on queen/king bedding, ten
heat levels, 30-minute preheat returning to the selected heat setting, and a
24-hour maximum auto-off setting. Remote activation requires physical presence
confirmation within one hour; without confirmation, settings above four are
reduced to four.

The candidate DP map and timer enum table come from
[tuya-local’s Sunbeam definition](https://github.com/make-all/tuya-local/blob/main/custom_components/tuya_local/devices/sunbeam_dual_bedwarmer.yaml).
They still require verification against the physical controller. `Stay_On` is
interpreted as 86,400 seconds, consistent with the manual’s 24-hour setting.

## Qualification record

Record the date, physical model, controller firmware, actual protocol version,
zone orientation, bridge revision, Python/TinyTuya versions, and network
conditions. Sanitized fixtures must include this provenance and omit
device/account identifiers and real local keys.

| Requirement                                                               | Result  |
| ------------------------------------------------------------------------- | ------- |
| Device identity, firmware, and local protocol                             | Pending |
| A/B orientation and required reported fields                              | Pending |
| Power and all ten levels independently on each side                       | Pending |
| Every supported timer enum and `Stay_On` duration                         | Pending |
| Level selection without implicit bridge power-on                          | Pending |
| Power-off/on preserves the selected level                                 | Pending |
| Verified individual-write ordering or multi-DP behavior                   | Pending |
| Preheat activation, cancellation, completion, and side effects            | Pending |
| Physical controls reflected in reported state                             | Pending |
| Timer expiry and remote presence-confirmation behavior                    | Pending |
| Cold boot, Wi-Fi, bridge, and broker interruption/recovery                | Pending |
| Home/Siri controls, inactive speed display, errors, and recovery          | Pending |
| Seven consecutive days with controller and bridge Internet access blocked | Pending |

For the soak, record start/end times, LAN firewall rules, interruptions
exercised, physical setting changes during outages, synchronization after
recovery, and any unexpected writes or degradation. Completion requires seven
consecutive days and the functional matrix above, as specified in the
[implementation plan](implementation-plan.md#hardware-acceptance-and-remaining-decisions).
