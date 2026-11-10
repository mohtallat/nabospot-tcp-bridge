#!/bin/sh

HOST="91.99.18.38"
PORT="9667"
ENDPOINT="$HOST:$PORT"

STATE_DIR="/tmp/nabospot-zento-watchdog"
MISSED_FILE="$STATE_DIR/missed"
LAST_RESTART_FILE="$STATE_DIR/last_restart"
LOCK_FILE="$STATE_DIR/lock"

MAX_MISSED=3
COOLDOWN=180

mkdir -p "$STATE_DIR" || exit 1

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

NOW=$(date +%s)

log_msg() {
    logger -t nabospot-zento-watchdog "$1"
    echo "$1"
}

CONNECTED=$(netstat -tan 2>/dev/null | awk \
    -v endpoint="$ENDPOINT" \
    '$5 == endpoint && $6 == "ESTABLISHED" {print 1; exit}')

if [ "$CONNECTED" = "1" ]; then
    echo 0 > "$MISSED_FILE"
    [ "$1" = "--dry-run" ] && echo "PASS: TCP connection established"
    exit 0
fi

TCP_STATE=$(netstat -tan 2>/dev/null | awk \
    -v endpoint="$ENDPOINT" \
    '$5 == endpoint {print $6; exit}')

[ -z "$TCP_STATE" ] && TCP_STATE="ABSENT"

MISSED=$(cat "$MISSED_FILE" 2>/dev/null)
case "$MISSED" in
    ''|*[!0-9]*) MISSED=0 ;;
esac

MISSED=$((MISSED + 1))

if [ "$1" = "--dry-run" ]; then
    echo "DRY RUN: TCP state=$TCP_STATE"
    echo "DRY RUN: consecutive missing checks would become $MISSED"
    echo "DRY RUN: no state changed and no service restarted"
    exit 0
fi

echo "$MISSED" > "$MISSED_FILE"

if [ "$MISSED" -lt "$MAX_MISSED" ]; then
    log_msg "TCP state=$TCP_STATE; missing=$MISSED/$MAX_MISSED"
    exit 0
fi

LAST_RESTART=$(cat "$LAST_RESTART_FILE" 2>/dev/null)
case "$LAST_RESTART" in
    ''|*[!0-9]*) LAST_RESTART=0 ;;
esac

ELAPSED=$((NOW - LAST_RESTART))

if [ "$ELAPSED" -ge 0 ] && [ "$ELAPSED" -lt "$COOLDOWN" ]; then
    exit 0
fi

CELLULAR_UP=$(ubus call network.interface.mob1s1a1 status \
    2>/dev/null | grep '"up": true')

if [ -z "$CELLULAR_UP" ]; then
    log_msg "Cellular interface unavailable; restart deferred"
    exit 0
fi

if [ "$(uci -q get rs_overip.1.always_reconnect)" != "0" ]; then
    log_msg "Unexpected reconnect configuration; restart deferred"
    exit 0
fi

log_msg "TCP state=$TCP_STATE; restarting rs_overip"

echo "$NOW" > "$LAST_RESTART_FILE"
echo 0 > "$MISSED_FILE"

/etc/init.d/rs_overip restart
RESULT=$?

log_msg "rs_overip restart exit_code=$RESULT"
exit "$RESULT"
