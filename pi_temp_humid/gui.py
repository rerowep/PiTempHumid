"""Qt touchscreen GUI for pi_temp_humid.

Shows the latest reading and a temperature/humidity chart with pan and zoom,
and switches to a large clock after a period of inactivity. Built for a small
800x480 display running Qt's EGLFS platform: views are switched inside one
window instead of opening new top-level windows.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QEvent, QLocale, QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from pi_temp_humid.cli import cleanup_dht_device, read_sensor, read_simulated
from pi_temp_humid.storage import clear_readings, get_recent_readings, init_db, prune_old_readings, save_reading

PKG_DIR = os.path.dirname(__file__)

BG_COLOR = "#121212"
TEMP_COLOR = "#ff6b6b"
HUM_COLOR = "#65a6ff"
MUTED_COLOR = "#888888"

# Upper bound for points kept in each chart series (~35 days at 5 min).
MAX_POINTS = 10000
# Smallest time span the chart can be zoomed into.
MIN_SPAN_MS = 60 * 1000
TOUCH_HEIGHT = 56
WINDOW_UNITS = {"Minutes": 60, "Hours": 3600, "Days": 86400, "Weeks": 604800, "Months": 2592000}
ACTIVITY_EVENTS = {QEvent.MouseButtonPress, QEvent.TouchBegin, QEvent.KeyPress, QEvent.Wheel}

SPINBOX_STYLE = (
    "QSpinBox { padding: 6px 8px; }"
    " QSpinBox::up-button, QSpinBox::down-button { width: 48px; }"
    " QSpinBox::up-arrow, QSpinBox::down-arrow { width: 20px; height: 20px; }"
)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def _now_ms() -> int:
    return QDateTime.currentMSecsSinceEpoch()


def _font(point_size: int, bold: bool = False) -> QFont:
    font = QFont()
    font.setPointSize(point_size)
    font.setBold(bold)
    return font


def _values_html(temp: float, hum: float) -> str:
    return (
        f"<span style='color:{TEMP_COLOR}'>{temp} °C</span>&nbsp;&nbsp;"
        f"<span style='color:#999'>&bull;</span>&nbsp;&nbsp;"
        f"<span style='color:{HUM_COLOR}'>{hum} %</span>"
    )


def _make_touch_friendly(widget: QWidget, min_width: int, point_size: int = 14) -> None:
    widget.setFont(_font(point_size, bold=True))
    widget.setFixedHeight(TOUCH_HEIGHT)
    widget.setMinimumWidth(min_width)
    widget.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    if isinstance(widget, QSpinBox):
        widget.setStyleSheet(SPINBOX_STYLE)
    elif isinstance(widget, QComboBox):
        widget.setStyleSheet("QComboBox { padding: 8px 10px; }")
    else:
        widget.setStyleSheet("QPushButton { padding: 8px 14px; }")


def _clock_font_family() -> str | None:
    """Prefer Helvetica, then the first font bundled in ``pi_temp_humid/fonts/``."""
    if "Helvetica" in QFontDatabase.families():
        return "Helvetica"
    fonts_dir = os.path.join(PKG_DIR, "fonts")
    if os.path.isdir(fonts_dir):
        for name in sorted(os.listdir(fonts_dir)):
            if name.lower().endswith((".ttf", ".otf")):
                font_id = QFontDatabase.addApplicationFont(os.path.join(fonts_dir, name))
                if families := QFontDatabase.applicationFontFamilies(font_id):
                    return families[0]
    return None


class SensorReader(QObject):
    """Read the sensor in a background thread so the UI never blocks.

    DHT reads retry internally and can take several seconds; running them on
    the GUI thread would freeze touch input, the clock and the quit shortcuts.
    """

    finished = Signal(float, float)
    failed = Signal(str)

    def __init__(self, sensor: str, pin: int, simulate: bool = False, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.simulate = simulate
        self.sensor = "simulated" if simulate else sensor
        self.pin = None if simulate else pin
        self._busy = False

    def start(self) -> bool:
        """Start a read unless one is already running.

        :returns: Whether a new read was started.
        :rtype: bool
        """
        if self._busy:
            return False
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            temp, hum = read_simulated() if self.simulate else read_sensor(self.sensor, self.pin)
        except (RuntimeError, OSError, ValueError) as exc:
            self._busy = False
            self.failed.emit(str(exc))
        else:
            self._busy = False
            self.finished.emit(temp, hum)


class InteractiveChartView(QChartView):
    """Chart view with drag-to-pan, wheel zoom and double-click reset."""

    def __init__(self, chart: QChart, window: MainWindow) -> None:
        super().__init__(chart)
        self.setRubberBand(QChartView.NoRubberBand)
        self.setRenderHint(QPainter.Antialiasing)
        self._window = window
        self._last_x: float | None = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last_x = event.position().x()
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._last_x is not None:
            x = event.position().x()
            # dragging right reveals earlier data
            self._window.pan_by_pixels(self._last_x - x)
            self._last_x = x
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last_x = None
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._window.reset_view()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        if delta := event.angleDelta().y():
            self._window.zoom_at(1.2 if delta > 0 else 1 / 1.2, event.position().x())
        event.accept()


class MainWindow(QWidget):
    def __init__(self, db_path: str | None = "readings.db", reader: SensorReader | None = None) -> None:
        super().__init__()
        self.setWindowTitle("PiTempHumid")
        self.setStyleSheet(f"background-color: {BG_COLOR}; color: #ffffff;")

        self.db_path = db_path
        if self.db_path:
            try:
                init_db(self.db_path)
            except sqlite3.Error as exc:
                print(f"Database unavailable ({exc}); readings will not be saved", file=sys.stderr)
                self.db_path = None
        self._prune_months = _env_int("PI_TEMP_PRUNE_MONTHS", 3) if _env_flag("PI_TEMP_PRUNE_ENABLED", "1") else 0
        self._idle_ms = _env_int("PI_TEMP_CLOCK_IDLE", 60, minimum=1) * 1000
        self.window_seconds = WINDOW_UNITS["Weeks"]
        # While True, each new reading scrolls the chart to end at "now".
        # Panning or zooming into the past turns it off; double-click resets.
        self._follow_live = True
        self._last_temp: float | None = None
        self._last_hum: float | None = None

        self._reader = reader or SensorReader(
            sensor=os.environ.get("PI_TEMP_SENSOR", "AM2302"),
            pin=_env_int("PI_TEMP_PIN", 11),
            simulate=_env_flag("PI_TEMP_SIMULATE", "0"),
            parent=self,
        )
        self._reader.finished.connect(self._on_reading)
        self._reader.failed.connect(self._on_read_error)

        self._build_ui()
        self._run_prune()
        self._load_history()
        self.reset_view()

        self._read_timer = QTimer(self, timeout=self.read_once)
        self._idle_timer = QTimer(self, singleShot=True, timeout=self.show_clock)
        self._clock_tick = QTimer(self, interval=1000, timeout=self._update_clock_display)
        if self._prune_months:
            QTimer(self, interval=24 * 3600 * 1000, timeout=self._run_prune).start()
        self._idle_timer.start(self._idle_ms)
        # Any tap or key press anywhere counts as activity for the idle clock.
        QApplication.instance().installEventFilter(self)
        # Emits `toggled`, which starts polling and takes a first reading.
        self.auto_button.setChecked(True)

    # -- UI construction ------------------------------------------------------
    def _build_ui(self) -> None:
        self.values_label = QLabel("Temperature: -- °C  •  Humidity: -- %")
        self.values_label.setFont(_font(22, bold=True))
        self.values_label.setAlignment(Qt.AlignCenter)

        self.chart_view = self._build_chart()
        self.clock_widget = self._build_clock()
        self._stack = QStackedLayout()
        self._stack.addWidget(self.chart_view)
        self._stack.addWidget(self.clock_widget)

        self.interval_spin = QSpinBox(minimum=1, maximum=60, value=5, suffix="m")
        self.interval_spin.valueChanged.connect(self._on_interval_change)
        self.auto_button = QPushButton("Start Auto", checkable=True)
        self.auto_button.toggled.connect(self.toggle_auto)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(list(WINDOW_UNITS))
        self.unit_combo.setCurrentText("Weeks")
        self.unit_combo.currentTextChanged.connect(self._on_window_change)
        self.window_spin = QSpinBox(minimum=1, maximum=10000, value=1)
        self.window_spin.valueChanged.connect(self._on_window_change)
        self.clock_button = QPushButton("Show Clock")
        self.clock_button.clicked.connect(self.show_clock)
        self.clear_button = QPushButton("Clear Data")
        self.clear_button.clicked.connect(self.clear_data)

        _make_touch_friendly(self.interval_spin, 100)
        _make_touch_friendly(self.auto_button, 140)
        _make_touch_friendly(self.unit_combo, 140, point_size=16)
        _make_touch_friendly(self.window_spin, 90, point_size=16)
        _make_touch_friendly(self.clock_button, 140)
        _make_touch_friendly(self.clear_button, 140)

        self.controls = QWidget()
        controls = QHBoxLayout(self.controls)
        controls.setSpacing(6)
        controls.setContentsMargins(6, 6, 6, 6)
        for widget in (self.interval_spin, self.auto_button, self.unit_combo, self.window_spin):
            controls.addWidget(widget)
        controls.addStretch()
        controls.addWidget(self.clock_button)
        controls.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.values_label)
        layout.addLayout(self._stack, stretch=1)
        layout.addWidget(self.controls)

    def _build_chart(self) -> InteractiveChartView:
        self.temp_series = QLineSeries()
        self.temp_series.setPen(QPen(QColor(TEMP_COLOR), 3))
        self.hum_series = QLineSeries()
        self.hum_series.setPen(QPen(QColor(HUM_COLOR), 3))

        self.chart = QChart()
        self.chart.addSeries(self.temp_series)
        self.chart.addSeries(self.hum_series)
        self.chart.legend().setVisible(False)
        self.chart.setBackgroundBrush(QColor(BG_COLOR))
        self.chart.setPlotAreaBackgroundBrush(QColor(28, 28, 28))
        self.chart.setPlotAreaBackgroundVisible(True)

        axis_font = _font(16)
        self.x_axis = QDateTimeAxis()
        self.x_axis.setFormat("dd.MM. HH:mm")
        self.x_axis.setLabelsColor(QColor(180, 180, 180))
        self.y_temp = QValueAxis()
        self.y_temp.setRange(0, 30)
        self.y_hum = QValueAxis()
        self.y_hum.setRange(0, 100)
        axes = ((self.x_axis, Qt.AlignBottom), (self.y_temp, Qt.AlignLeft), (self.y_hum, Qt.AlignRight))
        for axis, alignment in axes:
            axis.setLabelsFont(axis_font)
            self.chart.addAxis(axis, alignment)
        self.y_temp.setLabelsColor(Qt.white)
        self.y_hum.setLabelsColor(Qt.white)
        for series, y_axis in ((self.temp_series, self.y_temp), (self.hum_series, self.y_hum)):
            series.attachAxis(self.x_axis)
            series.attachAxis(y_axis)

        view = InteractiveChartView(self.chart, self)
        view.setMinimumHeight(200)
        return view

    def _build_clock(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        time_font = _font(72, bold=True)
        if family := _clock_font_family():
            time_font.setFamily(family)
        self.time_label = QLabel()
        self.time_label.setFont(time_font)
        # Ignored: the font is sized to the widget, not the widget to the font.
        self.time_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.date_label = QLabel()
        self.date_label.setFont(_font(18))
        self.date_label.setStyleSheet("color: #aaaaaa;")
        self.clock_stats_label = QLabel()
        self.clock_stats_label.setFont(_font(20))

        for label, stretch in ((self.time_label, 3), (self.date_label, 1), (self.clock_stats_label, 0)):
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, stretch=stretch)
        return widget

    # -- Readings -------------------------------------------------------------
    def read_once(self) -> None:
        """Start a background sensor read; the result arrives via a signal."""
        self._reader.start()

    def _on_reading(self, temp: float, hum: float) -> None:
        self._last_temp, self._last_hum = temp, hum
        self._show_values(temp, hum, datetime.now())
        if self.db_path:
            try:
                save_reading(self.db_path, temp, hum, self._reader.sensor, self._reader.pin)
            except sqlite3.Error as exc:
                print(f"Failed to save reading: {exc}", file=sys.stderr)
        self._append_points([(_now_ms(), temp, hum)])
        if self._follow_live:
            self.reset_view()

    def _on_read_error(self, message: str) -> None:
        self.values_label.setText(f"Error: {message}")

    def _show_values(self, temp: float, hum: float, when: datetime) -> None:
        self.values_label.setText(
            f"{_values_html(f'Temperature: {temp}', f'Humidity: {hum}')}"
            f"&nbsp;&nbsp;<span style='color:{MUTED_COLOR}'>({when:%H:%M})</span>"
        )

    def _append_points(self, points: list[tuple[int, float, float]]) -> None:
        for series, index in ((self.temp_series, 1), (self.hum_series, 2)):
            series.append([QPointF(p[0], p[index]) for p in points])
            if (excess := series.count() - MAX_POINTS) > 0:
                series.removePoints(0, excess)

    def _load_history(self) -> None:
        if not self.db_path:
            return
        try:
            rows = get_recent_readings(self.db_path, limit=MAX_POINTS)
        except sqlite3.Error as exc:
            print(f"Failed to load history: {exc}", file=sys.stderr)
            return
        points = []
        for ts_iso, temp, hum, _sensor, _pin in rows:
            try:
                ts = datetime.fromisoformat(ts_iso)
            except (TypeError, ValueError):
                continue
            points.append((int(ts.timestamp() * 1000), float(temp), float(hum)))
        if not points:
            return
        self._append_points(points)
        last_ms, self._last_temp, self._last_hum = points[-1]
        # stored timestamps are UTC; fromtimestamp converts to local time
        self._show_values(self._last_temp, self._last_hum, datetime.fromtimestamp(last_ms / 1000))

    def toggle_auto(self, on: bool) -> None:
        if on:
            self._read_timer.start(self.interval_spin.value() * 60 * 1000)
            self.auto_button.setText("Stop Auto")
            self.read_once()
        else:
            self._read_timer.stop()
            self.auto_button.setText("Start Auto")

    def _on_interval_change(self, minutes: int) -> None:
        if self._read_timer.isActive():
            self._read_timer.start(minutes * 60 * 1000)

    def clear_data(self) -> None:
        """Delete all stored readings after confirmation and empty the chart."""
        answer = QMessageBox.question(
            self,
            "Confirm Clear",
            "Are you sure you want to delete all stored readings?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not self.db_path:
            self.values_label.setText("DB not available")
            return
        try:
            clear_readings(self.db_path)
        except sqlite3.Error as exc:
            self.values_label.setText(f"Clear error: {exc}")
            return
        self.temp_series.clear()
        self.hum_series.clear()
        self._last_temp = self._last_hum = None
        self.values_label.setText("<span style='color:green'>Cleared data</span>")

    def _run_prune(self) -> None:
        if not (self.db_path and self._prune_months):
            return
        try:
            deleted = prune_old_readings(self.db_path, months=self._prune_months)
        except sqlite3.Error as exc:
            print(f"Prune failed: {exc}", file=sys.stderr)
            return
        if deleted:
            print(f"Pruned {deleted} readings older than {self._prune_months} months")

    # -- Chart view -----------------------------------------------------------
    def _view_range_ms(self) -> tuple[int, int]:
        return self.x_axis.min().toMSecsSinceEpoch(), self.x_axis.max().toMSecsSinceEpoch()

    def _set_view(self, start_ms: int, end_ms: int) -> None:
        # Shift the range back so it never extends into the future.
        span = end_ms - start_ms
        end_ms = min(end_ms, _now_ms())
        start_ms = max(0, end_ms - span)
        self.x_axis.setRange(QDateTime.fromMSecsSinceEpoch(start_ms), QDateTime.fromMSecsSinceEpoch(end_ms))

    def reset_view(self) -> None:
        """Show the configured time window ending now and follow new readings."""
        self._follow_live = True
        now = _now_ms()
        self._set_view(now - self.window_seconds * 1000, now)

    def pan_by_pixels(self, dx_px: float) -> None:
        """Shift the time axis by ``dx_px`` pixels; positive moves towards now."""
        width = self.chart.plotArea().width()
        if width <= 0:
            return
        start, end = self._view_range_ms()
        delta = int(dx_px * (end - start) / width)
        self._follow_live = end + delta >= _now_ms()
        self._set_view(start + delta, end + delta)

    def zoom_at(self, factor: float, x_px: float) -> None:
        """Zoom by ``factor`` (>1 zooms in), keeping the time under ``x_px`` in place."""
        area = self.chart.plotArea()
        if area.width() <= 0:
            return
        rel = min(1.0, max(0.0, (x_px - area.left()) / area.width()))
        start, end = self._view_range_ms()
        anchor = start + (end - start) * rel
        new_start = int(anchor - (anchor - start) / factor)
        new_end = int(anchor + (end - anchor) / factor)
        if new_end - new_start < MIN_SPAN_MS:
            return
        self._follow_live = new_end >= _now_ms()
        self._set_view(new_start, new_end)

    def _on_window_change(self, *_args) -> None:
        self.window_seconds = self.window_spin.value() * WINDOW_UNITS[self.unit_combo.currentText()]
        self.reset_view()

    # -- Clock ----------------------------------------------------------------
    def is_clock_shown(self) -> bool:
        return self._stack.currentWidget() is self.clock_widget

    def show_clock(self) -> None:
        """Replace the chart and controls with the full-size clock."""
        self._stack.setCurrentWidget(self.clock_widget)
        self.values_label.hide()
        self.controls.hide()
        self._update_clock_display()
        self._clock_tick.start()

    def hide_clock(self) -> None:
        """Return from the clock to the chart and controls."""
        self._clock_tick.stop()
        self._stack.setCurrentWidget(self.chart_view)
        self.values_label.show()
        self.controls.show()

    def on_user_activity(self) -> None:
        """Leave the clock if shown and restart the idle countdown."""
        if self.is_clock_shown():
            self.hide_clock()
        self._idle_timer.start(self._idle_ms)

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype in ACTIVITY_EVENTS:
            self.on_user_activity()
        elif etype == QEvent.Resize and obj is self.clock_widget:
            self._scale_time_font()
        return False

    def _scale_time_font(self) -> None:
        """Use the largest font size at which ``HH:mm`` fits the clock area."""
        padding = 20
        avail_w = self.clock_widget.width() - padding
        avail_h = (
            self.clock_widget.height()
            - self.date_label.sizeHint().height()
            - self.clock_stats_label.sizeHint().height()
            - padding
        )
        if avail_w <= 0 or avail_h <= 0:
            return
        font = QFont(self.time_label.font())
        lo, hi, best = 6, 400, 6
        while lo <= hi:
            mid = (lo + hi) // 2
            font.setPointSize(mid)
            metrics = QFontMetrics(font)
            if metrics.horizontalAdvance("00:00") <= avail_w and metrics.height() <= avail_h:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        font.setPointSize(best)
        self.time_label.setFont(font)

    def _update_clock_display(self) -> None:
        now = QDateTime.currentDateTime()
        # blink the colon by painting it in the background color on odd seconds
        colon_color = "#ffffff" if now.time().second() % 2 == 0 else BG_COLOR
        self.time_label.setText(f"{now.toString('HH')}<span style='color:{colon_color}'>:</span>{now.toString('mm')}")
        self.date_label.setText(QLocale(QLocale.French).toString(now.date(), QLocale.LongFormat))
        if self._last_temp is None or self._last_hum is None:
            self.clock_stats_label.setText(f"<span style='color:{MUTED_COLOR}'>No data</span>")
        else:
            self.clock_stats_label.setText(_values_html(self._last_temp, self._last_hum))

    # -- Shutdown -------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop all timers and release the sensor GPIO line."""
        for timer in self.findChildren(QTimer):
            timer.stop()
        cleanup_dht_device()


def _apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.Window, QColor(18, 18, 18)),
        (QPalette.WindowText, Qt.white),
        (QPalette.Base, QColor(28, 28, 28)),
        (QPalette.AlternateBase, QColor(38, 38, 38)),
        (QPalette.ToolTipBase, Qt.white),
        (QPalette.ToolTipText, Qt.white),
        (QPalette.Text, Qt.white),
        (QPalette.Button, QColor(35, 35, 35)),
        (QPalette.ButtonText, Qt.white),
        (QPalette.BrightText, Qt.red),
        (QPalette.Link, QColor(42, 130, 218)),
        (QPalette.Highlight, QColor(42, 130, 218)),
        (QPalette.HighlightedText, Qt.black),
    ):
        palette.setColor(role, color)
    app.setPalette(palette)
    app.setStyleSheet("QToolTip { color: #ffffff; background-color: #2a82da; border: 1px solid white; }")


def _install_quit_handlers(app: QApplication, window: QWidget) -> None:
    # Keyboard: Esc or Ctrl+Q quits. Window-scoped so Esc still dismisses
    # dialogs like the clear confirmation.
    for seq in (QKeySequence(Qt.Key_Escape), QKeySequence("Ctrl+Q")):
        QShortcut(seq, window, activated=app.quit)
    # Signals: Ctrl+C in the terminal or `pkill` quit cleanly so the
    # aboutToQuit cleanup runs and GPIO lines are released.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    # Python only runs signal handlers when the interpreter gets control;
    # a periodic no-op timer wakes it up from inside Qt's event loop.
    QTimer(app, interval=250, timeout=lambda: None).start()


def main(argv: list[str] | None = None) -> None:
    """Start the Qt application.

    If ``QT_QPA_PLATFORM`` is not set and ``PIQT_FORCE_EGLFS`` is, the
    ``eglfs`` platform is used to run fullscreen on embedded devices.
    """
    if os.environ.get("PIQT_FORCE_EGLFS") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "eglfs"

    app = QApplication(argv if argv is not None else sys.argv)
    app.setWindowIcon(QIcon(os.path.join(PKG_DIR, "icon.svg")))
    _apply_dark_theme(app)

    window = MainWindow(db_path=os.environ.get("PI_TEMP_DB", "readings.db"))
    app.aboutToQuit.connect(window.shutdown)
    _install_quit_handlers(app, window)
    window.resize(800, 480)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
