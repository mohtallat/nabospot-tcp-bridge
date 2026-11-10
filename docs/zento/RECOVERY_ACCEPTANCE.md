# NaboSpot Zento D8 — Recovery Acceptance Record

Date: 2026-11-10

Scope: ZB-1B recovery validation

## R1 — VPS listener restart

Result: PASS

The VPS recovery listener was restarted.

The TRB145 watchdog detected the missing TCP connection
and automatically restarted Serial over IP.

Authenticated TLS communication and valid RS485 STATUS
responses resumed.

Observed recovery: approximately 124 seconds.

## R2 — TRB145 reboot

Result: PASS

The TRB145 was rebooted through its administrative shell.

Serial over IP restarted after boot.

The VPS authenticated the returning gateway and resumed
valid STATUS polling.

Observed interruption: approximately 66 seconds.

## R3A — Cellular connection restart

Result: PASS

The cellular connection was restarted through the TRB145
administration interface.

The VPS detected disconnection at 13:38:32 UTC.

The watchdog recorded missing connections at:

- 13:39:00 UTC: 1/3
- 13:40:00 UTC: 2/3

At 13:41:00 UTC the watchdog restarted rs_overip.

The VPS authenticated the new session at:

13:41:00.688 UTC

The first valid STATUS response arrived at:

13:41:00.863 UTC

Observed recovery: approximately 149 seconds.

Subsequent STATUS responses remained available.

## Test coverage

Protocol tests: 11 passed.

Recovery listener tests: 9 passed.

Total: 20 passed.

## Source-control checkpoints

Protocol core:

d5d0bfb

Authenticated recovery listener:

4e0d0fe

## Safety observations

Recovery tests used STATUS polling only.

No physical OPEN, CLOSE, or STOP commands were intentionally
issued during these tests.

The existing Omni service remained operational.

## Remaining acceptance

- Preserve watchdog and deployment documentation in Git.
- Verify final deployment/source consistency.
- Perform final service-health validation.
- Retain prolonged-outage and stale-TCP limitations as
  explicitly unaccepted resilience scenarios.
