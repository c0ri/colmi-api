"""SQLite storage for ring readings and poller liveness.

The poll loop is the only writer; the REST API only reads.
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "colmi.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    heart_rate INTEGER,
    spo2 INTEGER,
    stress INTEGER,
    hrv INTEGER,
    steps INTEGER,
    battery INTEGER,
    reading_type TEXT NOT NULL DEFAULT 'full'
);
CREATE INDEX IF NOT EXISTS idx_readings_recorded_at ON readings(recorded_at DESC);

CREATE TABLE IF NOT EXISTS poller_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_cycle_at REAL,
    last_cycle_ok INTEGER,
    last_error TEXT
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO poller_state (id, last_cycle_at, last_cycle_ok, last_error) "
            "VALUES (1, NULL, NULL, NULL)"
        )


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_reading(
    heart_rate: int | None,
    spo2: int | None,
    stress: int | None,
    hrv: int | None,
    steps: int | None,
    battery: int | None,
    reading_type: str = "full",
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO readings
               (recorded_at, heart_rate, spo2, stress, hrv, steps, battery, reading_type)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?)""",
            (heart_rate, spo2, stress, hrv, steps, battery, reading_type),
        )


def get_latest_reading() -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM readings ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def record_cycle(ok: bool, error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE poller_state SET last_cycle_at = ?, last_cycle_ok = ?, last_error = ? WHERE id = 1",
            (time.time(), 1 if ok else 0, error),
        )


def get_poller_state() -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM poller_state WHERE id = 1").fetchone()
        return dict(row) if row else None
