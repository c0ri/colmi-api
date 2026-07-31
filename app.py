"""Colmi Ring HTTP API — read-only view over the poller's SQLite database.

This process never touches BLE. The poller (poller.py) is the sole writer;
this just serves the latest stored reading. Responses are single-digit ms.

Endpoints:
    GET /latest    — latest reading with a staleness signal
    GET /heartrate — heart rate only, same freshness contract
    GET /metrics   — full sensor suite, same freshness contract
    GET /health    — poller liveness (not ring connectivity)
"""

import os
import time

from dotenv import load_dotenv
from flask import Flask, jsonify, request

import db

load_dotenv()

PORT = int(os.getenv("PORT", "8080"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))
STALE_AFTER_SEC = int(os.getenv("STALE_AFTER_SEC", str(POLL_INTERVAL_SEC * 3)))

app = Flask(__name__)


def _maybe_record_query() -> None:
    """Track real callers (Evelyn/BOT) for the poller's idle-backoff decision.

    Excludes localhost so colmi-watchdog's own /latest checks don't look like
    activity and keep the poller pinned to its active interval forever.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        db.record_query()


def _reading_response() -> dict:
    _maybe_record_query()
    reading = db.get_latest_reading()
    if not reading:
        return {"error": "no readings yet", "stale": True}

    recorded_at = reading["recorded_at"]  # SQLite datetime('now'), UTC, "YYYY-MM-DD HH:MM:SS"
    recorded_epoch = time.mktime(time.strptime(recorded_at, "%Y-%m-%d %H:%M:%S")) - time.timezone
    age_seconds = max(0, int(time.time() - recorded_epoch))

    return {
        "heart_rate": reading["heart_rate"],
        "spo2": reading["spo2"],
        "stress": reading["stress"],
        "hrv": reading["hrv"],
        "steps": reading["steps"],
        "battery": reading["battery"],
        "recorded_at": recorded_at + "Z",
        "age_seconds": age_seconds,
        "stale": age_seconds > STALE_AFTER_SEC,
    }


@app.route("/latest")
def latest():
    return jsonify(_reading_response())


@app.route("/heartrate")
def heartrate():
    data = _reading_response()
    return jsonify({
        "heart_rate": data.get("heart_rate"),
        "recorded_at": data.get("recorded_at"),
        "age_seconds": data.get("age_seconds"),
        "stale": data.get("stale", True),
    })


@app.route("/metrics")
def metrics():
    return jsonify(_reading_response())


@app.route("/health")
def health():
    """Poller liveness — distinct from ring data freshness."""
    state = db.get_poller_state()
    if not state or state["last_cycle_at"] is None:
        return jsonify({"status": "unknown", "poller_alive": False}), 503

    since_last_cycle = time.time() - state["last_cycle_at"]
    poller_alive = since_last_cycle < STALE_AFTER_SEC
    last_queried_at = state.get("last_queried_at")
    return jsonify({
        "status": "ok" if poller_alive else "poller_stalled",
        "poller_alive": poller_alive,
        "seconds_since_last_cycle": int(since_last_cycle),
        "last_cycle_ok": bool(state["last_cycle_ok"]) if state["last_cycle_ok"] is not None else None,
        "last_error": state["last_error"],
        "poll_interval_sec": state.get("poll_interval_sec"),
        "seconds_since_last_query": int(time.time() - last_queried_at) if last_queried_at else None,
    }), 200 if poller_alive else 503


if __name__ == "__main__":
    db.init_db()
    print(f"Starting Colmi API (read-only) on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
