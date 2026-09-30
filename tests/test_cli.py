import sys
import types

import pytest
from click.testing import CliRunner

from pi_temp_humid import cli
from pi_temp_humid.storage import get_recent_readings


def test_read_simulated_saves_to_db(tmp_path):
    db = str(tmp_path / "cli.db")
    result = CliRunner().invoke(cli.cli, ["read", "--simulate", "--count", "2", "--save-db", db])
    assert result.exit_code == 0, result.output
    assert result.output.count("Temperature:") == 2
    assert len(get_recent_readings(db)) == 2


def test_read_fahrenheit():
    result = CliRunner().invoke(cli.cli, ["read", "--simulate", "--fahrenheit"])
    assert "°F" in result.output


def test_unsupported_sensor_rejected():
    result = CliRunner().invoke(cli.cli, ["read", "--sensor", "BME280"])
    assert result.exit_code != 0
    with pytest.raises(RuntimeError, match="unsupported"):
        cli.read_sensor("BME280")


def test_forced_adafruit_driver_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("PI_TEMP_DHT_DRIVER", "adafruit")
    monkeypatch.setitem(sys.modules, "adafruit_dht", None)
    with pytest.raises(RuntimeError, match="adafruit_dht"):
        cli.read_sensor("DHT22", 4)


def test_no_driver_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "adafruit_dht", None)
    monkeypatch.setitem(sys.modules, "Adafruit_DHT", None)
    with pytest.raises(RuntimeError, match="no DHT driver"):
        cli.read_sensor("DHT22", 4)


def _fake_legacy(monkeypatch, reading=(50.0, 21.0)):
    module = types.SimpleNamespace(DHT11=11, DHT22=22, read_retry=lambda sensor, pin: reading)
    monkeypatch.setitem(sys.modules, "Adafruit_DHT", module)


def test_auto_falls_back_when_adafruit_setup_fails(monkeypatch):
    def unsupported_board(key, pin):
        raise NotImplementedError("board not supported")

    monkeypatch.setattr(cli, "_read_adafruit", unsupported_board)
    _fake_legacy(monkeypatch)
    assert cli.read_sensor("DHT22", 4) == (21.0, 50.0)
    assert cli.last_driver == "Adafruit_DHT"


def test_auto_reports_adafruit_failure_without_legacy(monkeypatch):
    monkeypatch.setattr(cli, "_read_adafruit", lambda key, pin: None)
    monkeypatch.setitem(sys.modules, "Adafruit_DHT", None)
    with pytest.raises(RuntimeError, match="sensor returned no data"):
        cli.read_sensor("DHT22", 4)


def test_unusable_db_path_still_reads(tmp_path):
    db = str(tmp_path / "missing" / "cli.db")
    result = CliRunner().invoke(cli.cli, ["read", "--simulate", "--save-db", db])
    assert result.exit_code == 0, result.output
    assert "readings will not be saved" in result.output
    assert "Temperature:" in result.output
