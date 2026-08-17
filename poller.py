"""Colmi ring background poll loop — the sole owner of the BLE connection.

Runs forever on a fixed interval, reads whatever sensors it can in one cycle,
and writes the result to SQLite. Nothing else in this project touches BLE;
the REST API (app.py) only ever reads from the database.

Each cycle opens exactly ONE BLE connection (via colmi_r02_client's async
Client) and reads every metric through it before disconnecting. The
previous design shelled out to the colmi_r02_client CLI once per metric
(6 subprocesses per cycle, each its own bluetoothctl-disconnect + fresh
BLE connect/disconnect) — ~6x the connection churn this version makes.
That churn is the leading suspect for a multi-GB system dbus-daemon
memory leak observed after weeks of uptime (see
~/debian-dbus-leak.md and ~/dbus-leak-repro/); collapsing to one
connection per cycle removes the churn regardless of whether dbus-daemon
itself turns out to be at fault.

Liveness is tracked via db.record_cycle() and surfaced through the API's
/health endpoint rather than systemd's watchdog — Type=notify + WatchdogSec
was tried and dropped: colmi_r02_client's dependencies (anyio/asyncclick)
send their own sd_notify lifecycle messages if NOTIFY_SOCKET is present in
their environment, which systemd took as this service stopping and killed
the poll loop every cycle. See git history for that dead end.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from colmi_r02_client.client import Client
from colmi_r02_client.real_time import RealTimeReading

import db

load_dotenv()

COLMI_ADDRESS = os.getenv("COLMI_ADDRESS", "")
COLMI_TIMEOUT = int(os.getenv("COLMI_TIMEOUT", "45"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))

# Idle backoff: nobody's actually looking at the data (Evelyn's not in
# dominant mode), so poll less often to save the ring's battery. Any real
# caller (see app.py's _maybe_record_query — excludes the watchdog) clears
# this immediately; the poller notices within one 5s sleep tick.
IDLE_BACKOFF_SEC = int(os.getenv("IDLE_BACKOFF_SEC", str(90 * 60)))
POLL_INTERVAL_BACKOFF_SEC = int(os.getenv("POLL_INTERVAL_BACKOFF_SEC", str(30 * 60)))

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _ble_disconnect() -> None:
    """Defensive cleanup: release any stale BlueZ connection left over from
    a previous cycle that crashed before reaching its own disconnect."""
    try:
        subprocess.run(
            ["bluetoothctl", "disconnect", COLMI_ADDRESS],
            timeout=5,
            capture_output=True,
        )
    except Exception as e:
        print(f"  ⚠️ bluetoothctl disconnect error: {e}", flush=True)


def _ble_wake_and_advertise() -> None:
    """Wake a sleeping ring via a bonded connect, then disconnect so it advertises."""
    if not COLMI_ADDRESS:
        return
    print("  🔔 Waking ring via bluetoothctl connect...", flush=True)
    try:
        subprocess.run(
            ["bluetoothctl", "connect", COLMI_ADDRESS],
            timeout=15,
            capture_output=True,
        )
    except Exception as e:
        print(f"  ⚠️ bluetoothctl connect error: {e}", flush=True)
    _ble_disconnect()
    time.sleep(3)


async def _read(client: Client, label: str, make_awaitable) -> Any | None:
    """Await one metric read within the shared connection.

    This ring's BLE link is occasionally flaky mid-cycle (observed:
    everything fine for several reads, then a sudden drop with no clear
    trigger). If the read fails AND the connection actually dropped,
    reconnect once and retry — recovers the same way the old
    reconnect-every-metric design did, but only pays the reconnect cost
    when a drop actually happens, not on every single metric every cycle.
    If we're still connected and the read just failed/timed out (e.g. the
    ring didn't produce a valid HRV sample in time), don't reconnect —
    that's not a dropped connection, retrying the same way wouldn't help,
    and one bad metric shouldn't cost the rest of the cycle.
    """
    try:
        return await asyncio.wait_for(make_awaitable(), timeout=COLMI_TIMEOUT)
    except Exception as e:
        print(f"  ⚠️ {label} read failed: {e}", flush=True)
        if client.bleak_client.is_connected:
            return None
        print(f"  🔄 Connection dropped — reconnecting for {label} retry...", flush=True)
        try:
            await asyncio.wait_for(client.connect(), timeout=COLMI_TIMEOUT)
        except Exception as e2:
            print(f"  ❌ Reconnect failed: {e2}", flush=True)
            return None
        try:
            return await asyncio.wait_for(make_awaitable(), timeout=COLMI_TIMEOUT)
        except Exception as e3:
            print(f"  ⚠️ {label} retry after reconnect failed: {e3}", flush=True)
            return None


async def _run_cycle_async() -> tuple[int | None, ...]:
    heart_rate = spo2 = stress = hrv = steps = battery = None

    client = Client(COLMI_ADDRESS)
    try:
        await asyncio.wait_for(client.connect(), timeout=COLMI_TIMEOUT)
    except Exception as e:
        print(f"  ⚠️ Connect failed ({e}), attempting wake...", flush=True)
        _ble_wake_and_advertise()
        try:
            await asyncio.wait_for(client.connect(), timeout=COLMI_TIMEOUT)
        except Exception as e2:
            print(f"  ❌ Connect failed after wake: {e2}", flush=True)
            return (None, None, None, None, None, None)

    try:
        if not _shutdown:
            r = await _read(client, "heart-rate", lambda: client.get_realtime_reading(RealTimeReading.HEART_RATE))
            heart_rate = r[-1] if r else None
        if not _shutdown:
            r = await _read(client, "spo2", lambda: client.get_realtime_reading(RealTimeReading.SPO2))
            spo2 = r[-1] if r else None
        if not _shutdown:
            r = await _read(client, "pressure", lambda: client.get_realtime_reading(RealTimeReading.PRESSURE))
            stress = r[-1] if r else None
        if not _shutdown:
            r = await _read(client, "hrv", lambda: client.get_realtime_reading(RealTimeReading.HRV))
            hrv = r[-1] if r else None
        if not _shutdown:
            result = await _read(client, "steps", lambda: client.get_steps(datetime.now(timezone.utc)))
            if isinstance(result, list) and result:
                steps = sum(entry.steps for entry in result)
        if not _shutdown:
            info = await _read(client, "battery", lambda: client.get_battery())
            battery = info.battery_level if info else None
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception as e:
            print(f"  ⚠️ Disconnect error: {e}", flush=True)

    return (heart_rate, spo2, stress, hrv, steps, battery)


def current_poll_interval() -> int:
    last_queried_at = db.get_last_queried_at()
    if last_queried_at is None:
        return POLL_INTERVAL_SEC
    if time.time() - last_queried_at > IDLE_BACKOFF_SEC:
        return POLL_INTERVAL_BACKOFF_SEC
    return POLL_INTERVAL_SEC


def run_cycle() -> None:
    heart_rate, spo2, stress, hrv, steps, battery = asyncio.run(_run_cycle_async())

    got_anything = any(
        v is not None for v in (heart_rate, spo2, stress, hrv, steps, battery)
    )
    next_interval = current_poll_interval()
    if got_anything:
        db.insert_reading(heart_rate, spo2, stress, hrv, steps, battery)
        db.record_cycle(ok=True, poll_interval_sec=next_interval)
        print(
            f"  💾 Stored reading: hr={heart_rate} spo2={spo2} stress={stress} "
            f"hrv={hrv} steps={steps} battery={battery}",
            flush=True,
        )
    else:
        db.record_cycle(
            ok=False,
            error="ring unreachable — no sensors returned data",
            poll_interval_sec=next_interval,
        )
        print("  ⚠️ Cycle produced no data — ring unreachable", flush=True)


def main() -> None:
    if not COLMI_ADDRESS:
        print("ERROR: COLMI_ADDRESS not set in .env", file=sys.stderr)
        sys.exit(1)

    db.init_db()
    print(
        f"Colmi poller starting. address={COLMI_ADDRESS} interval={POLL_INTERVAL_SEC}s "
        f"backoff_interval={POLL_INTERVAL_BACKOFF_SEC}s after {IDLE_BACKOFF_SEC}s idle",
        flush=True,
    )

    last_logged_interval = None
    while not _shutdown:
        cycle_start = time.time()
        try:
            run_cycle()
        except Exception as e:
            print(f"  ❌ Unhandled cycle error: {e}", flush=True)
            db.record_cycle(ok=False, error=str(e), poll_interval_sec=current_poll_interval())

        # Re-checked every tick (not fixed at cycle start) so a query arriving
        # mid-sleep during backoff shortens the wait immediately, and idle
        # time crossing the threshold mid-sleep extends it.
        while not _shutdown:
            target = current_poll_interval()
            if target != last_logged_interval:
                print(f"  ⏱ Poll interval: {target}s "
                      f"({'backoff' if target == POLL_INTERVAL_BACKOFF_SEC else 'active'})", flush=True)
                last_logged_interval = target
            elapsed = time.time() - cycle_start
            if elapsed >= target:
                break
            time.sleep(min(5.0, target - elapsed))

    print("Colmi poller shutting down.", flush=True)


if __name__ == "__main__":
    main()
