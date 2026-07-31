#!/usr/bin/env bash
# Watchdog for the Colmi ring pipeline. Checks whether bluetoothd itself is
# wedged UNCONDITIONALLY, every run — independent of data staleness/backoff
# state. This is deliberate: STALE_AFTER_SEC and the poller's idle backoff
# now allow data to legitimately go up to ~35min old, but bluetoothd health
# is a completely separate concern and must be checked and fixed on this
# script's normal cadence regardless (e.g. right after the ring is taken off
# for a recharge and put back on — you don't want to wait out a stale
# backoff window before recovery even starts).
#
# The bluetoothctl query itself is a local D-Bus call against the adapter,
# not the ring — cheap, and it never competes with colmi-poller for the
# ring's BLE connection slot.
#
# See /home/pi/.claude/skills/colmi-recover/SKILL.md for the manual version
# of this procedure and background on the failure mode.

set -uo pipefail

LOG_TAG="colmi-watchdog"
COLMI_URL="http://localhost:8090/latest"
CURL_TIMEOUT=10

log() { logger -t "$LOG_TAG" "$*"; echo "$(date -Iseconds) $*"; }

bluetoothd_wedged() {
    local out
    if ! out=$(timeout 5 bluetoothctl show 2>&1); then
        return 0
    fi
    [[ "$out" == *"No default controller"* ]] && return 0
    return 1
}

if bluetoothd_wedged; then
    log "bluetoothd wedged — recovering: stop poller, restart bluetooth, start poller"
    systemctl stop colmi-poller.service
    if ! systemctl restart bluetooth.service; then
        log "first bluetooth.service restart failed (stale D-Bus name) — retrying once"
        sleep 2
        systemctl restart bluetooth.service
    fi
    sleep 3
    systemctl start colmi-poller.service
    log "recovery sequence complete"
    exit 0
fi

# bluetoothd is healthy — nothing to fix. Log data freshness purely for
# visibility; it is never used to gate action here.
response=$(curl --silent --max-time "$CURL_TIMEOUT" "$COLMI_URL" 2>/dev/null)
if [[ -z "$response" ]]; then
    log "bluetoothd OK; colmi-api unreachable at $COLMI_URL — leaving to colmi-api.service's own restart policy"
    exit 0
fi

age=$(echo "$response" | jq -r '.age_seconds // empty')
if [[ -z "$age" ]]; then
    log "bluetoothd OK; no reading yet — poller likely still starting"
else
    log "bluetoothd OK; colmi data age=${age}s"
fi
