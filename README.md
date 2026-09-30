# pi_temp_humid

Read temperature and humidity from an AM2302/DHT22 or DHT11 sensor on a
Raspberry Pi, store readings in SQLite and show them on a fullscreen
touchscreen GUI (chart + idle clock).

## Quick start

```bash
uv sync --extra dev
uv run pi-temp-humid read --simulate          # one simulated reading
uv run pi-temp-humid read --pin 4 --save-db readings.db
uv run poe gui_sim                            # GUI with simulated sensor (desktop)
uv run poe gui                                # GUI with the real sensor
```

## Development

```bash
uv run poe tests
uv run poe lint
uv run poe format
```

## GUI controls

- Drag the chart to pan, mouse wheel to zoom, double-tap to return to "now".
- After `PI_TEMP_CLOCK_IDLE` seconds without input a large clock is shown;
  tap anywhere to return.
- **Quit:** `Esc` or `Ctrl+Q` on an attached keyboard, `Ctrl+C` in the
  terminal, or `sudo systemctl stop pi_temp_humid` / `pkill -f pi_temp_humid.gui`.

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `PI_TEMP_DB` | `readings.db` | SQLite file |
| `PI_TEMP_SENSOR` | `AM2302` | `AM2302`, `DHT22` or `DHT11` |
| `PI_TEMP_PIN` | `11` | BCM GPIO number of the sensor data line (GUI) |
| `PI_TEMP_DHT_DRIVER` | `auto` | `auto`, `adafruit` (CircuitPython) or `legacy` (`Adafruit_DHT`) |
| `PI_TEMP_SIMULATE` | `0` | `1` = random readings, no hardware |
| `PI_TEMP_CLOCK_IDLE` | `60` | Seconds of inactivity before the clock appears |
| `PI_TEMP_PRUNE_ENABLED` | `1` | Delete old readings at start and daily |
| `PI_TEMP_PRUNE_MONTHS` | `3` | Age limit for pruning |
| `PIQT_FORCE_EGLFS` | unset | Use Qt's `eglfs` platform (fullscreen, no desktop) |

## Running on the Pi as a service

```bash
sudo ./scripts/install_service.sh --workdir /opt/pi_temp_humid
sudo systemctl status pi_temp_humid
journalctl -u pi_temp_humid -f
```

The service restarts only after a crash; quitting with `Esc` leaves it stopped.

### Official 7" touchscreen: touch stops responding

If Qt logs two touch devices (`raspberrypi-ts` and `generic ft5x06`), the
firmware and the kernel driver are both polling the same touch controller,
which makes touch fail intermittently. Keep only the kernel driver by adding
this to `config.txt` on the boot partition and rebooting:

```text
disable_touchscreen=1
```

### Locked out of a fullscreen Pi

Put the SD card in another computer and append (to the single line) in
`cmdline.txt` on the boot partition:

```text
systemd.unit=rescue.target systemd.setenv=SYSTEMD_SULOGIN_FORCE=1 systemd.mask=pi_temp_humid.service
```

Boot with a keyboard, fix what is needed (e.g. `passwd <user>`), then remove
those parameters again.

## License

MIT
