#!/usr/bin/env bash
# Watchdog for the Colmi ring pipeline. Checks actual data freshness (not
# just poll-loop liveness, which stays "ok" even when every read fails) and,
# if data is stale AND bluetoothd itself is wedged, runs the recovery
# sequence: stop poller -> restart bluetooth -> start poller.
#
# Does not touch BLE directly (no bluetoothctl connect/scan) so it never
# competes with colmi-poller for the ring's connection slot.
#
# See /home/pi/.claude/skills/colmi-recover/SKILL.md for the manual version
# of this procedure and background on the failure mode.

set -uo pipefail

LOG_TAG="colmi-watchdog"
COLMI_URL="http://localhost:8090/latest"
CURL_TIMEOUT=10
STALE_THRESHOLD_SEC=1200  # above STALE_AFTER_SEC (900s) to avoid false positives

log() { logger -t "$LOG_TAG" "$*"; echo "$(date -Iseconds) $*"; }

bluetoothd_wedged() {
    local out
    if ! out=$(timeout 5 bluetoothctl show 2>&1); then
        return 0
    fi
    [[ "$out" == *"No default controller"* ]] && return 0
    return 1
}

response=$(curl --silent --max-time "$CURL_TIMEOUT" "$COLMI_URL" 2>/dev/null)
if [[ -z "$response" ]]; then
    log "colmi-api unreachable at $COLMI_URL — leaving to colmi-api.service's own restart policy"
    exit 0
fi

age=$(echo "$response" | jq -r '.age_seconds // empty')
if [[ -z "$age" ]]; then
    log "no reading yet (response: $response) — poller likely still starting, nothing to do"
    exit 0
fi

if (( age <= STALE_THRESHOLD_SEC )); then
    log "colmi OK (age=${age}s)"
    exit 0
fi

log "colmi data stale (age=${age}s) — checking bluetoothd"

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
else
    log "colmi data stale but bluetoothd healthy — likely ring out of range/off-wrist; poller will retry on its own, no action taken"
fi
