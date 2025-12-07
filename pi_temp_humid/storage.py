"""Storage helpers for PiTempHumid.

Provides a small, well-scoped API for initializing and appending readings to
an SQLite database. Other modules should import these helpers instead of
duplicating the logic.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional


def init_db(path: str) -> None:
    """Ensure the SQLite database and `readings` table exist."""
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                temperature_c REAL NOT NULL,
                humidity REAL NOT NULL,
                sensor TEXT,
                pin INTEGER
            )
            """
        )
        con.commit()
    finally:
        con.close()


def save_reading(
    path: str,
    temperature_c: float,
    humidity: float,
    sensor: Optional[str],
    pin: Optional[int],
) -> None:
    """Append a reading to the SQLite database.

    `path` is the SQLite file path. `sensor` and `pin` can be None.
    """
    ts = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO readings (ts, temperature_c, humidity, sensor, pin) VALUES (?, ?, ?, ?, ?)",
            (ts, temperature_c, humidity, sensor, pin),
        )
        con.commit()
    finally:
        con.close()


def get_recent_readings(
    path: str, limit: int = 1000
) -> list[tuple[str, float, float, Optional[str], Optional[int]]]:
    """Return up to `limit` most recent readings from the DB.

    Returns a list of tuples `(ts_iso, temperature_c, humidity, sensor, pin)`
    ordered from oldest to newest.
    """
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT ts, temperature_c, humidity, sensor, pin FROM readings ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        # rows are newest-first; return oldest-first for plotting
        rows.reverse()
        return rows
    finally:
        con.close()


def prune_old_readings(path: str, months: int = 3) -> int:
    """Delete readings older than `months` months (approx. 30 days/month).

    Returns the number of rows deleted. This uses a conservative estimate of
    a month as 30 days to avoid adding extra dependencies.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    cutoff_iso = cutoff.isoformat()
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM readings WHERE ts < ?", (cutoff_iso,))
        deleted = cur.rowcount if cur.rowcount is not None else 0
        con.commit()
        return deleted
    finally:
        con.close()
