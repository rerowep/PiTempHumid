import sqlite3
from datetime import datetime, timedelta, timezone

from pi_temp_humid.storage import clear_readings, get_recent_readings, prune_old_readings, save_reading


def _insert(db_path, ts, temp=20.0, hum=50.0):
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO readings (ts, temperature_c, humidity) VALUES (?, ?, ?)",
            (ts.isoformat(), temp, hum),
        )


def test_save_and_get_returns_oldest_first(db_path):
    now = datetime.now(timezone.utc)
    _insert(db_path, now - timedelta(minutes=10), temp=1.0)
    _insert(db_path, now - timedelta(minutes=5), temp=2.0)
    save_reading(db_path, 3.0, 40.0, "AM2302", 11)

    rows = get_recent_readings(db_path)
    assert [r[1] for r in rows] == [1.0, 2.0, 3.0]
    assert rows[-1][3:] == ("AM2302", 11)
    assert [r[1] for r in get_recent_readings(db_path, limit=2)] == [2.0, 3.0]


def test_prune_deletes_only_old_rows(db_path):
    now = datetime.now(timezone.utc)
    _insert(db_path, now - timedelta(days=100))
    _insert(db_path, now - timedelta(days=10))

    assert prune_old_readings(db_path, months=3) == 1
    assert len(get_recent_readings(db_path)) == 1


def test_clear_readings(db_path):
    save_reading(db_path, 20.0, 50.0, None, None)
    clear_readings(db_path)
    assert get_recent_readings(db_path) == []
