# NaboSpot Zento D8 — Recovery Operations Runbook

## 1. Architecture

The Zento D8 barrier communicates over RS485 through a
Teltonika TRB145 4G gateway.

The gateway connects to the NaboSpot VPS using TCP port 9667
with mutual TLS authentication.

Components:

- Zento D8 RS485 controller
- Teltonika TRB145 Serial over IP
- TRB145 recovery watchdog
- VPS authenticated TLS recovery listener
- Zento protocol parser and STATUS polling

The Zento recovery listener is independent of the existing
Omni TCP bridge.

## 2. Safety boundary

The recovery implementation operates in STATUS-only mode.

Recovery operations must not issue physical OPEN, CLOSE,
or STOP commands to the barrier.

Do not use movement commands during connectivity diagnosis,
service recovery, or automated recovery testing.

## 3. VPS configuration

Repository:

/opt/nabospot-tcp

Service:

nabospot-zento.service

Listener:

zento_recovery_listener.py

TCP port:

9667

TLS directory:

/etc/nabospot/zento-tls

Expected client identity:

nabospot-trb145-pilot-01

The TLS directory contains certificate material and must
not be committed to Git.

## 4. VPS recovery behavior

The listener provides:

- Mutual TLS client authentication
- Expected client identity validation
- Single active session per gateway identity
- Session generation protection
- RS485 STATUS polling approximately every 10 seconds
- STATUS response timeout and missed-response handling
- TCP keepalive
- Automatic process restart through systemd

The listener does not automatically authorize physical
barrier movement.

## 5. TRB145 configuration

Serial-over-IP service:

rs_overip

Connection destination:

91.99.18.38:9667

Important configuration:

- Client mode
- TLS enabled
- Client certificate authentication
- Server certificate validation
- always_reconnect=0
- recon_interval=5
- TCP keepalive enabled

Do not change always_reconnect to 1 without a separate
diagnosis and acceptance test. That setting previously
caused rapid connection cycling.

## 6. TRB145 watchdog

Installed script:

/etc/nabospot/zento-watchdog.sh

Repository source:

deployment/trb145/zento-watchdog.sh

Cron schedule:

* * * * * /etc/nabospot/zento-watchdog.sh

Behavior:

1. Acquire a nonblocking execution lock.
2. Check whether TCP port 9667 is ESTABLISHED.
3. Reset the missing counter when connected.
4. Count consecutive missing connections.
5. After three missing checks, verify cellular availability.
6. Verify always_reconnect remains disabled.
7. Respect a 180-second restart cooldown.
8. Restart only the rs_overip service.
9. Record the result in the device system log.

Watchdog state is stored under:

/tmp/nabospot-zento-watchdog

This state is temporary and resets on device reboot.

## 7. Monitoring

VPS:

systemctl is-active nabospot-zento.service
systemctl is-active nabospot-tcp.service
journalctl -u nabospot-zento.service -n 30 --no-pager
ss -tanp | grep ':9667'

TRB145:

logread | grep 'nabospot-zento-watchdog'
ps w | grep '[s]odog'
netstat -tan | grep '91.99.18.38:9667'

A healthy TCP connection alone does not prove successful
RS485 communication. Confirm fresh STATUS responses in
the VPS journal.

## 8. Recovery troubleshooting

If STATUS responses stop:

1. Check the VPS service state.
2. Check whether a TLS client is authenticated.
3. Check the TRB145 mobile interface.
4. Check the TRB145 TCP connection.
5. Inspect watchdog logs and cooldown state.
6. Confirm whether automatic recovery occurs.
7. Diagnose failures before changing configuration.

Do not issue movement commands as a diagnostic technique.

## 9. Rollback

For a watchdog-specific problem, disable its cron entry
before investigating or restoring a previous watchdog
version.

Preserve the existing TRB145 configuration and backups.

For a VPS listener problem, inspect the service logs,
configuration, and previous known-good Git revision.

Rollback must be separately validated before restarting
production services.

Do not stop or modify the Omni bridge as part of Zento
recovery.

## 10. Known limitations

- The TRB145 watchdog uses local TCP ESTABLISHED state.
- A stale TCP socket may temporarily appear healthy.
- The VPS independently monitors STATUS freshness.
- A prolonged cellular outage has not been independently
  accepted as a separate recovery test.
- Recovery tests do not establish physical movement safety.
- Production movement authorization requires separate
  design and acceptance.

## 11. Deployment discipline

Changes must follow:

Diagnosis -> implementation -> validation -> staged review
-> controlled commit -> deployment acceptance.

Do not commit private keys, TLS credentials, runtime state,
temporary files, or legacy listener backups.
