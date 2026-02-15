"""Colmi Ring HTTP API — wraps colmi_r02_client CLI for bot biometrics.

Endpoints:
    GET /heartrate — heart rate only (~15s)
    GET /metrics   — full sensor suite (~1-2min)
    GET /health    — ring connectivity check
"""

import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

app = Flask(__name__)

COLMI_ADDRESS = os.getenv("COLMI_ADDRESS", "")
COLMI_BIN = os.getenv("COLMI_BIN", "/home/pi/.local/bin/colmi_r02_client")
COLMI_TIMEOUT = int(os.getenv("COLMI_TIMEOUT", "45"))
PORT = int(os.getenv("PORT", "8080"))

# Simple in-memory cache to prevent concurrent BLE collisions
_cache = {
    "heartrate": {"data": None, "at": 0.0},
    "metrics": {"data": None, "at": 0.0},
}
_cache_lock = threading.Lock()
CACHE_TTL = 10  # seconds


def _get_cached(key: str) -> dict | None:
    """Return cached data if fresh enough."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry["data"] and (time.time() - entry["at"]) < CACHE_TTL:
            return entry["data"]
    return None


def _set_cached(key: str, data: dict) -> None:
    """Store data in cache."""
    with _cache_lock:
        _cache[key] = {"data": data, "at": time.time()}


def run_colmi_command(subcommand: str, timeout: int | None = None) -> str | None:
    """Run a colmi_r02_client CLI command and return stdout.

    Args:
        subcommand: The subcommand to run (e.g. "get-real-time heart-rate")
        timeout: Timeout in seconds (default COLMI_TIMEOUT)

    Returns:
        stdout string or None on failure
    """
    if not COLMI_ADDRESS:
        return None

    cmd = [COLMI_BIN, f"--address={COLMI_ADDRESS}", *subcommand.split()]
    timeout = timeout or COLMI_TIMEOUT

    for attempt in range(2):
        try:
            print(f"  ▶ Running: {' '.join(cmd)} (attempt {attempt + 1})")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                print(f"  ✅ Output: {result.stdout.strip()[:120]}")
                return result.stdout.strip()
            if result.stderr.strip():
                print(f"  ⚠️ stderr: {result.stderr.strip()[:120]}")
            if attempt == 0:
                time.sleep(2)
        except subprocess.TimeoutExpired:
            print(f"  ⏰ Timeout after {timeout}s")
            if attempt == 0:
                time.sleep(2)
        except Exception as e:
            print(f"  ❌ Error: {e}")
            break

    return None


def parse_values(output: str) -> list[int]:
    """Extract integer values from colmi_r02_client output.

    The CLI outputs values like:
        Starting reading, please wait.
        [84, 83, 83, 83, 83, 80]
    """
    # Match the list format [84, 83, 83, 83, 83, 80]
    list_match = re.search(r"\[([0-9,\s]+)\]", output)
    if list_match:
        return [int(v.strip()) for v in list_match.group(1).split(",") if v.strip()]

    # Fallback: individual [value] matches
    matches = re.findall(r"\[(\d+)\]", output)
    return [int(m) for m in matches]


def parse_last_value(output: str) -> int | None:
    """Extract the last (most recent) value from CLI output."""
    values = parse_values(output)
    return values[-1] if values else None


def get_heart_rate() -> int | None:
    """Get current heart rate from the ring."""
    output = run_colmi_command("get-real-time heart-rate")
    if not output:
        return None
    return parse_last_value(output)


def get_spo2() -> int | None:
    """Get current SpO2 from the ring."""
    output = run_colmi_command("get-real-time spo2")
    if not output:
        return None
    return parse_last_value(output)


def get_stress() -> int | None:
    """Get current stress level from the ring."""
    output = run_colmi_command("get-real-time pressure")
    if not output:
        return None
    return parse_last_value(output)


def get_hrv() -> int | None:
    """Get current HRV from the ring."""
    output = run_colmi_command("get-real-time hrv")
    if not output:
        return None
    return parse_last_value(output)


def get_steps() -> int | None:
    """Get current step count."""
    output = run_colmi_command("get-steps")
    if not output:
        return None
    # get-steps may return "No results for day"
    if "no results" in output.lower():
        return 0
    return parse_last_value(output)


def get_battery() -> int | None:
    """Get battery level from the ring."""
    output = run_colmi_command("info")
    if not output:
        return None
    match = re.search(r"battery.*?(\d+)%?", output, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


@app.route("/heartrate")
def heartrate():
    """Fast heart rate endpoint (~15s)."""
    cached = _get_cached("heartrate")
    if cached:
        return jsonify(cached)

    hr = get_heart_rate()
    if hr is None:
        return jsonify({"error": "Failed to read heart rate", "heart_rate": None}), 503

    data = {
        "heart_rate": hr,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _set_cached("heartrate", data)
    return jsonify(data)


@app.route("/metrics")
def metrics():
    """Full sensor suite (~1-2min due to sequential BLE calls)."""
    cached = _get_cached("metrics")
    if cached:
        return jsonify(cached)

    hr = get_heart_rate()
    spo2 = get_spo2()
    stress = get_stress()
    hrv = get_hrv()
    steps = get_steps()
    battery = get_battery()

    data = {
        "heart_rate": hr,
        "spo2": spo2,
        "stress": stress,
        "hrv": hrv,
        "steps": steps,
        "battery": battery,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _set_cached("metrics", data)

    # Also update the heartrate cache since we just read it
    if hr is not None:
        _set_cached("heartrate", {
            "heart_rate": hr,
            "timestamp": data["timestamp"],
        })

    return jsonify(data)


@app.route("/health")
def health():
    """Check ring connectivity."""
    output = run_colmi_command("info", timeout=15)
    connected = output is not None and len(output) > 0
    return jsonify({
        "status": "ok" if connected else "ring_unreachable",
        "ring_connected": connected,
        "address": COLMI_ADDRESS,
    })


if __name__ == "__main__":
    if not COLMI_ADDRESS:
        print("ERROR: COLMI_ADDRESS not set in .env")
        exit(1)
    print(f"Starting Colmi API on port {PORT}")
    print(f"Ring address: {COLMI_ADDRESS}")
    print(f"CLI binary: {COLMI_BIN}")
    app.run(host="0.0.0.0", port=PORT)
