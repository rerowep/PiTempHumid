"""SQLite storage for readings.

Timestamps are stored as UTC ISO-8601 strings so they sort and compare
correctly as text.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

Reading = tuple[str, float, float, str | None, int | None]


def _connect(path: str) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(path))


def init_db(path: str) -> None:
    """Ensure the SQLite database and ``readings`` table exist."""
    with _connect(path) as con, con:
        con.execute(
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
        con.execute("CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts)")


def save_reading(
    path: str,
    temperature_c: float,
    humidity: float,
    sensor: str | None,
    pin: int | None,
) -> None:
    """Append a reading stamped with the current UTC time."""
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(path) as con, con:
        con.execute(
            "INSERT INTO readings (ts, temperature_c, humidity, sensor, pin) VALUES (?, ?, ?, ?, ?)",
            (ts, temperature_c, humidity, sensor, pin),
        )


def get_recent_readings(path: str, limit: int = 1000) -> list[Reading]:
    """Return up to ``limit`` most recent readings, oldest first.

    :param path: SQLite file path.
    :type path: str
    :param limit: Maximum number of rows to return.
    :type limit: int
    :returns: Tuples of ``(ts_iso, temperature_c, humidity, sensor, pin)``.
    :rtype: list[tuple]
    """
    with _connect(path) as con:
        rows = con.execute(
            "SELECT ts, temperature_c, humidity, sensor, pin FROM readings ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    rows.reverse()
    return rows


def clear_readings(path: str) -> None:
    """Delete all stored readings."""
    with _connect(path) as con, con:
        con.execute("DELETE FROM readings")


def prune_old_readings(path: str, months: int = 3) -> int:
    """Delete readings older than ``months`` months (a month counts as 30 days).

    :returns: Number of deleted rows.
    :rtype: int
    """
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=months * 30)).isoformat()
    with _connect(path) as con, con:
        return con.execute("DELETE FROM readings WHERE ts < ?", (cutoff_iso,)).rowcount
