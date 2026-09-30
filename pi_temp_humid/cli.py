"""Command-line interface and sensor access for pi_temp_humid."""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import threading
import time

import click

from pi_temp_humid.storage import init_db, save_reading

SUPPORTED_SENSORS = ("AM2302", "DHT22", "DHT11")

# Name of the driver that served the most recent successful read.
last_driver: str | None = None

# The CircuitPython device currently reading (if any), so it can be released
# from another thread when the application quits mid-read.
_active_device = None
_device_lock = threading.Lock()


def read_simulated() -> tuple[float, float]:
    """Return a random but plausible (temperature °C, humidity %) pair."""
    temp_c = round(20.0 + random.random() * 10.0, 1)
    humid = round(30.0 + random.random() * 50.0, 1)
    return temp_c, humid


def _release_device(device) -> None:
    exit_fn = getattr(device, "exit", None)
    if callable(exit_fn):
        exit_fn()


def _read_adafruit(key: str, pin: int, attempts: int = 5) -> tuple[float, float] | None:
    global _active_device
    import adafruit_dht
    import board

    board_pin = getattr(board, f"D{pin}", None)
    if board_pin is None:
        raise RuntimeError(f"GPIO {pin} is not available on this board")
    sensor_cls = adafruit_dht.DHT11 if key == "DHT11" else adafruit_dht.DHT22
    device = sensor_cls(board_pin)
    with _device_lock:
        _active_device = device
    try:
        for _ in range(attempts):
            try:
                temp, hum = device.temperature, device.humidity
            except RuntimeError:
                # DHT sensors routinely fail single reads (checksum, timing).
                temp = hum = None
            if temp is not None and hum is not None:
                return round(float(temp), 1), round(float(hum), 1)
            time.sleep(2)
        return None
    finally:
        with _device_lock:
            _active_device = None
        _release_device(device)


def _read_legacy(key: str, pin: int) -> tuple[float, float]:
    try:
        import Adafruit_DHT
    except ImportError as exc:
        raise RuntimeError("no DHT driver available: install 'adafruit-circuitpython-dht' or 'Adafruit_DHT'") from exc
    sensor = Adafruit_DHT.DHT11 if key == "DHT11" else Adafruit_DHT.DHT22
    humidity, temperature = Adafruit_DHT.read_retry(sensor, pin)
    if humidity is None or temperature is None:
        raise RuntimeError("sensor returned no data")
    return round(temperature, 1), round(humidity, 1)


def read_sensor(sensor_name: str = "AM2302", pin: int = 4) -> tuple[float, float]:
    """Read temperature and humidity from a DHT sensor.

    Uses the CircuitPython ``adafruit_dht`` driver when available and falls
    back to the legacy ``Adafruit_DHT`` package. Set ``PI_TEMP_DHT_DRIVER`` to
    ``adafruit`` or ``legacy`` to force one of them (default: ``auto``).

    :param sensor_name: Sensor type: AM2302, DHT22 or DHT11.
    :type sensor_name: str
    :param pin: BCM GPIO number of the data line.
    :type pin: int
    :returns: Temperature in °C and relative humidity in %, rounded to 0.1.
    :rtype: tuple[float, float]
    :raises RuntimeError: When the sensor type is unsupported, no driver is
        installed or the sensor returns no data.
    """
    global last_driver
    key = sensor_name.strip().upper()
    if key not in SUPPORTED_SENSORS:
        raise RuntimeError(f"unsupported sensor type: {sensor_name}")
    driver = os.environ.get("PI_TEMP_DHT_DRIVER", "auto").strip().lower()

    if driver in ("auto", "adafruit"):
        try:
            result = _read_adafruit(key, pin)
        except ImportError as exc:
            if driver == "adafruit":
                raise RuntimeError("requested 'adafruit' driver but 'adafruit_dht' is not available") from exc
            result = None
        if result is not None:
            last_driver = "adafruit_dht"
            return result
        if driver == "adafruit":
            raise RuntimeError("sensor returned no data")

    result = _read_legacy(key, pin)
    last_driver = "Adafruit_DHT"
    return result


def cleanup_dht_device() -> None:
    """Release a CircuitPython DHT device that is still mid-read.

    Called when the GUI quits so libgpiod frees the GPIO line.
    """
    with _device_lock:
        device = _active_device
    if device is not None:
        _release_device(device)


@click.group()
def cli() -> None:
    """pi-temp-humid CLI"""


@cli.command("read")
@click.option("--simulate", is_flag=True, help="simulate sensor readings")
@click.option(
    "--sensor",
    default="AM2302",
    show_default=True,
    type=click.Choice(SUPPORTED_SENSORS, case_sensitive=False),
    help="sensor type",
)
@click.option("--pin", default=4, show_default=True, type=int, help="BCM GPIO pin for data line")
@click.option("--count", default=1, show_default=True, type=click.IntRange(min=1), help="how many readings to produce")
@click.option(
    "--save-db",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="path to SQLite DB file to append readings",
)
@click.option("--fahrenheit", is_flag=True, help="show Fahrenheit instead of Celsius")
def read(simulate: bool, sensor: str, pin: int, count: int, save_db: str | None, fahrenheit: bool) -> None:
    """Read temperature and humidity from sensor or simulator."""
    if save_db:
        init_db(save_db)
    for _ in range(count):
        try:
            temp_c, humid = read_simulated() if simulate else read_sensor(sensor_name=sensor, pin=pin)
        except RuntimeError as exc:
            click.echo(f"Error: {exc}", err=True)
            click.echo("Tip: run with --simulate to produce sample values.", err=True)
            sys.exit(2)

        if save_db:
            try:
                save_reading(save_db, temp_c, humid, sensor, pin)
            except (sqlite3.Error, OSError) as exc:
                click.echo(f"Failed to save reading to {save_db}: {exc}", err=True)

        if fahrenheit:
            temp, unit = round(temp_c * 9.0 / 5.0 + 32.0, 1), "°F"
        else:
            temp, unit = temp_c, "°C"
        click.echo(f"Temperature: {temp}{unit}, Humidity: {humid}%")
        if not simulate and last_driver:
            click.echo(f"(DHT driver: {last_driver})")


def main(argv: list[str] | None = None) -> None:
    cli.main(args=argv)


if __name__ == "__main__":
    main()
