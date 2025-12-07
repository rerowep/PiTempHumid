pi_temp_humid
===============

Small utility to read temperature and humidity (AM2302/DHT22 or DHT11)
from a Raspberry Pi and optionally save readings to an SQLite database.

Quick start
-----------

- Install the package editable and the dev extras (provides `poethepoet`):

```bash
pip install -e .[dev]
```

- To run a simulated read (no hardware required):

```bash
uv run pi_temp_humid.cli read --simulate
# or with poethepoet if you prefer (install via `[dev]` extras)
# poethepoet run run
# or directly
# python -m pi_temp_humid.cli read --simulate
```

- To run the GUI (desktop):

```bash
uv run pi_temp_humid.gui
# or with poethepoet
# poethepoet run gui
# or
# python -m pi_temp_humid.gui
```

Persisting readings
-------------------

- Save readings to an SQLite file with `--save-db`:

```bash
uv run pi_temp_humid.cli read --simulate --save-db readings.db
# or
# pi-temp-humid read --simulate --save-db readings.db
# or
# python -m pi_temp_humid.cli read --simulate --save-db readings.db
```

Using `uv` (recommended)
-------------------------

This project recommends using the `uv` project manager for running commands. Install dev extras to get `uv`:

```bash
pip install -e .[dev]
```

- `uv run pi_temp_humid.cli read --simulate` — run a simulated read
- `uv run pi_temp_humid.cli read` — run the CLI read command (see options)
- `uv run pi_temp_humid.gui` — run the Qt GUI
- `uv run pip install -e .` — install the package editable (or use `uv run` to invoke any command)

If you prefer `poethepoet`, the existing poethepoet tasks are still available in `pyproject.toml` and will continue to work.

Hardware notes
--------------

- The CLI supports `AM2302`/`DHT22` and `DHT11` via the `Adafruit_DHT` library.
- Use the `--sensor` and `--pin` options to select the sensor type and BCM
	GPIO pin. Example: `pi-temp-humid read --sensor DHT22 --pin 4`.
- On a Pi you may need to enable gpio access for the user or run with
	appropriate permissions.

Development & testing
---------------------

- Run tests with poethepoet:

```bash
poethepoet run test
```

Notes
-----

- The project provides `pi_temp_humid.storage` for initializing and saving
	readings to SQLite; both the CLI and GUI use that module.
- If you plan to run the GUI on a Pi with an EGLFS/DRM framebuffer, set
	the appropriate Qt environment variables for your platform (e.g.
	`QT_QPA_PLATFORM=eglfs`) before launching.

License
-------

MIT — modify as needed.
# PiTempHumid
