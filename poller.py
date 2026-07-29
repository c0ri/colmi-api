"""Colmi ring background poll loop — the sole owner of the BLE connection.

Runs forever on a fixed interval, reads whatever sensors it can in one cycle,
and writes the result to SQLite. Nothing else in this project touches BLE;
the REST API (app.py) only ever reads from the database.

Liveness is tracked via db.record_cycle() and surfaced through the API's
/health endpoint rather than systemd's watchdog — Type=notify + WatchdogSec
was tried and dropped: colmi_r02_client's dependencies (anyio/asyncclick)
send their own sd_notify lifecycle messages if NOTIFY_SOCKET is present in
their environment, which systemd took as this service stopping and killed
the poll loop every cycle. See git history for that dead end.
"""

import os
import re
import signal
import subprocess
import sys
import time

from dotenv import load_dotenv

import db

load_dotenv()

COLMI_ADDRESS = os.getenv("COLMI_ADDRESS", "")
COLMI_BIN = os.getenv("COLMI_BIN", "/home/pi/.local/bin/colmi_r02_client")
COLMI_TIMEOUT = int(os.getenv("COLMI_TIMEOUT", "45"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _ble_disconnect() -> None:
    """Disconnect from BlueZ so the ring resumes advertising for bleak."""
    try:
        subprocess.run(
            ["bluetoothctl", "disconnect", COLMI_ADDRESS],
            timeout=5,
            capture_output=True,
        )
        time.sleep(3)
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


def run_colmi_command(subcommand: str) -> str | None:
    """Run one colmi_r02_client subcommand, retrying once if the ring was asleep."""
    cmd = [COLMI_BIN, f"--address={COLMI_ADDRESS}", *subcommand.split()]
    _ble_disconnect()

    for attempt in range(2):
        if _shutdown:
            return None
        proc = None
        try:
            print(f"  ▶ Running: {' '.join(cmd)} (attempt {attempt + 1})", flush=True)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=COLMI_TIMEOUT)
            if proc.returncode == 0 and stdout.strip():
                print(f"  ✅ Output: {stdout.strip()[:120]}", flush=True)
                return stdout.strip()
            if stderr.strip():
                err = stderr.strip()
                print(f"  ⚠️ stderr: {err[-300:]}", flush=True)
                if "BleakDeviceNotFoundError" in err and attempt == 0:
                    _ble_wake_and_advertise()
                    continue
        except subprocess.TimeoutExpired:
            print(f"  ⏰ Timeout after {COLMI_TIMEOUT}s", flush=True)
            if proc is not None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        except Exception as e:
            print(f"  ❌ Error: {e}", flush=True)
            break

    return None


def parse_values(output: str) -> list[int]:
    list_match = re.search(r"\[([0-9,\s]+)\]", output)
    if list_match:
        return [int(v.strip()) for v in list_match.group(1).split(",") if v.strip()]
    matches = re.findall(r"\[(\d+)\]", output)
    return [int(m) for m in matches]


def parse_last_value(output: str) -> int | None:
    values = parse_values(output)
    return values[-1] if values else None


def read_metric(subcommand: str) -> int | None:
    output = run_colmi_command(subcommand)
    if not output:
        return None
    if "no results" in output.lower():
        return 0
    return parse_last_value(output)


def read_battery() -> int | None:
    output = run_colmi_command("info")
    if not output:
        return None
    match = re.search(r"battery.*?(\d+)%?", output, re.IGNORECASE)
    return int(match.group(1)) if match else None


def run_cycle() -> None:
    heart_rate = spo2 = stress = hrv = steps = battery = None

    if not _shutdown:
        heart_rate = read_metric("get-real-time heart-rate")
    if not _shutdown:
        spo2 = read_metric("get-real-time spo2")
    if not _shutdown:
        stress = read_metric("get-real-time pressure")
    if not _shutdown:
        hrv = read_metric("get-real-time hrv")
    if not _shutdown:
        steps = read_metric("get-steps")
    if not _shutdown:
        battery = read_battery()

    got_anything = any(
        v is not None for v in (heart_rate, spo2, stress, hrv, steps, battery)
    )
    if got_anything:
        db.insert_reading(heart_rate, spo2, stress, hrv, steps, battery)
        db.record_cycle(ok=True)
        print(
            f"  💾 Stored reading: hr={heart_rate} spo2={spo2} stress={stress} "
            f"hrv={hrv} steps={steps} battery={battery}",
            flush=True,
        )
    else:
        db.record_cycle(ok=False, error="ring unreachable — no sensors returned data")
        print("  ⚠️ Cycle produced no data — ring unreachable", flush=True)


def main() -> None:
    if not COLMI_ADDRESS:
        print("ERROR: COLMI_ADDRESS not set in .env", file=sys.stderr)
        sys.exit(1)

    db.init_db()
    print(f"Colmi poller starting. address={COLMI_ADDRESS} interval={POLL_INTERVAL_SEC}s", flush=True)

    while not _shutdown:
        cycle_start = time.time()
        try:
            run_cycle()
        except Exception as e:
            print(f"  ❌ Unhandled cycle error: {e}", flush=True)
            db.record_cycle(ok=False, error=str(e))

        elapsed = time.time() - cycle_start
        remaining = max(0.0, POLL_INTERVAL_SEC - elapsed)
        deadline = time.time() + remaining
        while not _shutdown and time.time() < deadline:
            time.sleep(min(5.0, deadline - time.time()))

    print("Colmi poller shutting down.", flush=True)


if __name__ == "__main__":
    main()
