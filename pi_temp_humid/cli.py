"""Small, clean CLI for pi_temp_humid.

This is a minimal, single-copy implementation used as the canonical
command-line entrypoint.
"""

from __future__ import annotations

import contextlib
import os
import random
import sqlite3
import sys
from typing import Optional

import click

# Try to import CircuitPython DHT driver and board mapping once so the
# module can reuse the same import instead of attempting repeated imports
# inside `_read_sensor`. If unavailable, these will be `None` and the
# legacy `Adafruit_DHT` fallback will be used.
try:
    import adafruit_dht  # type: ignore
except Exception:
    adafruit_dht = None  # type: ignore

try:
    import board as _board_module  # type: ignore
except Exception:
    _board_module = None  # type: ignore

try:
    # Normal package import (preferred)
    from .storage import init_db, save_reading
except ImportError:  # pragma: no cover - fallback for different invocation modes
    try:
        # If executed as a module from project root, this will work
        from pi_temp_humid.storage import init_db, save_reading
    except ImportError:
        # If executed as a script (python pi_temp_humid/cli.py), adjust sys.path
        import os
        import sys

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        try:
            from pi_temp_humid.storage import init_db, save_reading
        except ImportError:
            try:
                # final attempt: local module import when package name is not present
                from storage import init_db, save_reading
            except ImportError:
                # Last resort: provide no-op fallbacks so CLI can still run (simulated only)
                def init_db(path: str) -> None:  # pragma: no cover - fallback
                    return None

                def save_reading(
                    path: str,
                    temperature_c: float,
                    humidity: float,
                    sensor: str,
                    pin: int,
                ) -> None:  # pragma: no cover - fallback
                    return None


def _read_simulated() -> tuple[float, float]:
    temp_c = round(20.0 + random.random() * 10.0, 1)
    humid = round(30.0 + random.random() * 50.0, 1)
    return temp_c, humid


def _read_sensor(sensor_name: str = "AM2302", pin: int = 11) -> tuple[float, float]:
    # Prefer the newer CircuitPython `adafruit_dht` (via Blinka) when
    # available on Raspberry Pi. Fall back to the legacy `Adafruit_DHT`
    # package if the modern binding is not present.
    key = sensor_name.strip().upper()
    # allow overriding driver selection via env var: 'auto' (default),
    # 'adafruit' (CircuitPython), or 'legacy' (Adafruit_DHT)
    _driver_pref = os.environ.get("PI_TEMP_DHT_DRIVER", "auto").strip().lower()

    # Attempt CircuitPython / Blinka `adafruit_dht` first (recommended)
    if _driver_pref in ("auto", "adafruit"):
        with contextlib.suppress(Exception):
            import adafruit_dht

            # track any active device instance so callers (e.g. GUI) can
            # request a cleanup on application exit if needed
            globals().setdefault("_DHT_DEVICE", None)

            try:
                # `board` provides pin objects like `board.D4` on Raspberry Pi
                import board
            except Exception:
                board = None

            if key in {"AM2302", "DHT22"}:
                sensor_cls = getattr(adafruit_dht, "DHT22", None)
            elif key == "DHT11":
                sensor_cls = getattr(adafruit_dht, "DHT11", None)
            else:
                sensor_cls = None

            if sensor_cls is not None:
                # Resolve a board pin object from the integer BCM pin where possible.
                board_pin = None
                if board is not None:
                    try:
                        board_pin = getattr(board, f"D{pin}", None)
                    except Exception:
                        board_pin = None
                    if board_pin is None:
                        # Try alternative attribute names used on some platforms
                        try:
                            board_pin = getattr(board, f"GPIO{pin}", None)
                        except Exception:
                            board_pin = None
                # Last-resort: if no board pin available and pin==4, try common D4
                if board_pin is None and board is not None:
                    board_pin = getattr(board, "D4", None)

                # Instantiate the device and attempt a few reads. Keep this
                # whole operation in a try/except so failures fall back to the
                # legacy library implementation.
                with contextlib.suppress(Exception):
                    import time

                    if board_pin is None:
                        # If we don't have a board pin object, constructing the
                        # sensor may fail; try to construct without a pin and
                        # rely on adafruit_dht to raise a clear error.
                        dht_device = sensor_cls()
                    else:
                        dht_device = sensor_cls(board_pin)

                    # expose the device so external cleanup can find it
                    with contextlib.suppress(Exception):
                        globals()["_DHT_DEVICE"] = dht_device
                    # Try a few times to read — the device may need a brief warmup
                    temp_val = None
                    hum_val = None
                    for _ in range(5):
                        try:
                            temp = getattr(dht_device, "temperature", None)
                            hum = getattr(dht_device, "humidity", None)
                            if temp is None or hum is None:
                                time.sleep(1)
                                continue
                            temp_val = round(float(temp), 1)
                            hum_val = round(float(hum), 1)
                            # record driver used (best-effort)
                            globals()["LAST_DHT_DRIVER"] = "adafruit_dht"
                            break
                        except Exception:
                            # Some adafruit_dht builds raise runtime errors on read
                            time.sleep(1)
                    # cleanup the device object carefully so libgpiod releases
                    try:
                        # try common cleanup method names
                        for m in ("exit", "close", "deinit", "shutdown"):
                            fn = getattr(dht_device, m, None)
                            if callable(fn):
                                with contextlib.suppress(Exception):
                                    fn()
                    except Exception:
                        pass
                    finally:
                        with contextlib.suppress(Exception):
                            globals()["_DHT_DEVICE"] = None
                    if temp_val is not None and hum_val is not None:
                        return temp_val, hum_val
    elif _driver_pref == "adafruit":
        # explicit request for adafruit_dht but import failed above
        raise RuntimeError(
            "requested 'adafruit' driver but 'adafruit_dht' is not available"
        )

    # Legacy `Adafruit_DHT` fallback (keeps previous behavior)
    try:
        import Adafruit_DHT
    except ImportError as exc:  # pragma: no cover - hardware dependent
        raise RuntimeError(
            "no DHT driver available: install 'adafruit_dht' or 'Adafruit_DHT'"
        ) from exc

    if key in {"AM2302", "DHT22"}:
        sensor = Adafruit_DHT.DHT22
    elif key == "DHT11":
        sensor = Adafruit_DHT.DHT11
    else:
        raise RuntimeError(f"unsupported sensor type: {sensor_name}")

    humidity, temperature = Adafruit_DHT.read_retry(sensor, pin)
    if humidity is None or temperature is None:
        raise RuntimeError("sensor returned no data")
    # record the driver used for the CLI to display (best-effort)
    try:
        LAST_DHT_DRIVER = "Adafruit_DHT"
        globals()["LAST_DHT_DRIVER"] = LAST_DHT_DRIVER
    except Exception:
        pass
    finally:
        # ensure any temporary device tracker is cleared (none for legacy)
        with contextlib.suppress(Exception):
            globals()["_DHT_DEVICE"] = None
    return round(temperature, 1), round(humidity, 1)


@click.group()
def cli() -> None:
    """pi-temp-humid CLI"""


@cli.command("read")
@click.option("--simulate", is_flag=True, help="simulate sensor readings")
@click.option(
    "--sensor",
    default="AM2302",
    show_default=True,
    help="sensor type (AM2302/DHT22/DHT11)",
)
@click.option(
    "--pin", default=4, show_default=True, type=int, help="BCM GPIO pin for data line"
)
@click.option(
    "--count",
    default=1,
    show_default=True,
    type=int,
    help="how many readings to produce",
)
@click.option(
    "--save-db",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="path to SQLite DB file to append readings",
)
@click.option("--fahrenheit", is_flag=True, help="show Fahrenheit instead of Celsius")
def read(
    simulate: bool,
    sensor: str,
    pin: int,
    count: int,
    save_db: Optional[str],
    fahrenheit: bool,
) -> None:
    """Read temperature and humidity from sensor or simulator."""
    for _ in range(count):
        try:
            if simulate:
                temp_c, humid = _read_simulated()
            else:
                temp_c, humid = _read_sensor(sensor_name=sensor, pin=pin)
        except RuntimeError as exc:
            click.echo(f"Error: {exc}", err=True)
            click.echo("Tip: run with --simulate to produce sample values.", err=True)
            sys.exit(2)

        if save_db:
            try:
                init_db(save_db)
                save_reading(save_db, temp_c, humid, sensor, pin)
            except (sqlite3.Error, OSError) as exc:
                click.echo(f"Failed to save reading to {save_db}: {exc}", err=True)

        if fahrenheit:
            temp = round(temp_c * 9.0 / 5.0 + 32.0, 1)
            unit = "°F"
        else:
            temp = temp_c
            unit = "°C"

        click.echo(f"Temperature: {temp}{unit}, Humidity: {humid}%")

        # Print which driver was used (best-effort). This is helpful when
        # running the CLI to confirm whether CircuitPython or legacy driver
        # handled the read.
        with contextlib.suppress(Exception):
            if drv := globals().get("LAST_DHT_DRIVER"):
                click.echo(f"(DHT driver: {drv})")


def main(argv: Optional[list[str]] = None) -> None:
    if argv is None:
        cli()
    else:
        cli.main(args=argv)


if __name__ == "__main__":
    main()


def cleanup_dht_device() -> None:
    """Best-effort cleanup for any active adafruit_dht device.

    This will be called from `aboutToQuit` so the application can
    attempt to release GPIO lines held by the CircuitPython driver.
    """
    with contextlib.suppress(Exception):
        dev = globals().get("_DHT_DEVICE")
        if dev is None:
            return
        for m in ("exit", "close", "deinit", "shutdown"):
            fn = getattr(dev, m, None)
            if callable(fn):
                with contextlib.suppress(Exception):
                    fn()
        with contextlib.suppress(Exception):
            globals()["_DHT_DEVICE"] = None
