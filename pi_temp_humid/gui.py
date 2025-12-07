"""Qt EGLFS GUI for PiTempHumid.
# hide clock if visible (ignore attribute errors)
try:
    if getattr(self, "clock_label", None) is not None and self.clock_label.isVisible():
        self._hide_clock()
except AttributeError:
    pass
# restart single-shot timer (ignore TypeError/AttributeError on some Qt bindings)
try:
    if getattr(self, "_clock_timer", None) is not None:
        self._clock_timer.start(self._idle_seconds * 1000)
except (AttributeError, TypeError):
    pass
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import suppress
from datetime import datetime
from typing import Optional

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QLocale, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
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

from pi_temp_humid.cli import _read_sensor, cleanup_dht_device
from pi_temp_humid.storage import init_db, prune_old_readings, save_reading


class ClickableLabel(QLabel):
    """Simple QLabel that emits a click by overriding mousePressEvent."""

    def __init__(self, *args, on_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_click = on_click

    def mousePressEvent(self, event):
        if callable(self._on_click):
            with suppress(Exception):
                self._on_click()
        super().mousePressEvent(event)


class InteractiveChartView(QChartView):
    """QChartView subclass that supports mouse panning and wheel zoom.

    Panning: left-mouse-drag
    Zoom: mouse wheel (zoom towards pointer)
    Double-click: reset zoom to configured window
    """

    def __init__(self, chart: QChart, parent_window: "MainWindow") -> None:
        super().__init__(chart)
        self.setRubberBand(QChartView.NoRubberBand)
        self._panning = False
        self._last_pos = None
        self._parent_window = parent_window

    def _get_event_pos(self, event):
        """Return an object with `.x()` and `.y()` for the event position.

        Prefer `position()` (Qt6) and fall back to `pos()` for older bindings.
        """
        return event.position() if hasattr(event, "position") else event.pos()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._panning = True
            self._last_pos = self._get_event_pos(event)
            self.setCursor(Qt.ClosedHandCursor)
            # user interacted with the chart; reset clock switch timer
            with suppress(Exception):
                self._parent_window.reset_clock_timer()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning and self._last_pos is not None:
            pos = self._get_event_pos(event)
            dx = pos.x() - self._last_pos.x()
            # positive dx means mouse moved right -> pan left (earlier)
            self._parent_window.pan_by_pixels(-dx)
            self._last_pos = pos
            with suppress(Exception):
                self._parent_window.reset_clock_timer()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._panning = False
            self._last_pos = None
            self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        # reset zoom on double-click
        self._parent_window.reset_zoom()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.2 if delta > 0 else (1.0 / 1.2)
        # use mouse x position to zoom towards that timestamp
        # QWheelEvent may provide `position()` (Qt6) or `pos()` depending on
        # the PySide6/Qt version. Handle both.
        try:
            if hasattr(event, "position"):
                mouse_x = int(event.position().x())
            else:
                mouse_x = int(event.pos().x())
        except Exception:
            # fallback to widget midpoint if position unavailable
            mouse_x = int(self.width() / 2)
        self._parent_window.zoom_at(factor, mouse_x, self.width())
        with suppress(Exception):
            self._parent_window.reset_clock_timer()
        super().wheelEvent(event)


class MainWindow(QWidget):
    def __init__(self, db_path: Optional[str] = "readings.db") -> None:
        super().__init__()
        self.db_path = db_path
        if self.db_path:
            try:
                init_db(self.db_path)
            except sqlite3.Error:
                # DB init failures should not stop the UI; show later on save
                self.db_path = None

        self.setWindowTitle("PiTempHumid")
        # Ensure the main window background is dark to match the dark theme
        with suppress(AttributeError, TypeError):
            self.setStyleSheet("background-color: #121212; color: #ffffff;")
            self.setAutoFillBackground(True)
        # Set application/window icon from package asset if present
        pkg_dir = os.path.dirname(__file__)
        icon_path = os.path.join(pkg_dir, "icon.svg")
        with suppress(OSError, RuntimeError, AttributeError):
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        # Track bundled font families (best-effort): prefer the bundled
        # `FLIPclockblack` font when available and fall back to system
        # defaults otherwise. Users should place the TTF/OTF into
        # `pi_temp_humid/fonts/` (common candidate names are tried).
        self._clock_family = None
        try:
            clock_candidates = ("fonts/FLIPclockwhite.ttf", "FLIPclockwhite.ttf")
            for fname in clock_candidates:
                fpath = os.path.join(pkg_dir, fname)
                if os.path.exists(fpath):
                    with suppress(Exception):
                        fid = QFontDatabase.addApplicationFont(fpath)
                        if fid != -1:
                            families = QFontDatabase.applicationFontFamilies(fid)
                            if families:
                                self._clock_family = families[0]
                                break
        except Exception:
            # ignore font loading errors
            self._clock_family = None

        self._preferred_clock_family = None
        try:
            # Use the static API to avoid constructing a QFontDatabase
            # instance (constructor is deprecated in some Qt versions).
            fams = set(QFontDatabase.families())
            for candidate in ("Helvetica", "Helvetica Bold"):
                if candidate in fams:
                    self._preferred_clock_family = candidate
                    break
            if self._preferred_clock_family is None and getattr(
                self, "_clock_family", None
            ):
                self._preferred_clock_family = self._clock_family
        except Exception:
            self._preferred_clock_family = getattr(self, "_clock_family", None)
        # Single-line, large display for temperature and humidity
        self.values_label = QLabel("Temp: -- °C Humid: -- %")
        font = QFont()
        font.setPointSize(22)
        font.setBold(True)
        self.values_label.setFont(font)
        self.values_label.setAlignment(Qt.AlignCenter)
        # ensure text is visible on dark backgrounds
        with suppress(AttributeError, TypeError):
            self.values_label.setStyleSheet("color: #ffffff;")

        # Buttons removed: automatic polling / saving handled via Auto and DB writes

        # Simulation checkbox removed — always read real sensor by default

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 60)
        # Default interval set to 5 minutes for auto-start mode
        self.interval_spin.setValue(5)
        # Display minutes in the spinbox suffix
        self.interval_spin.setSuffix("m")
        # make input widgets compact (use minimum width so layout can
        # preserve touch-friendly heights when widgets are hidden/shown)
        with suppress(AttributeError, TypeError):
            try:
                self.interval_spin.setFixedHeight(56)
                self.interval_spin.setMinimumWidth(100)
                self.interval_spin.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                # Make the spinbox arrow buttons wider for touch targets
                self.interval_spin.setStyleSheet(
                    "QSpinBox { padding: 6px 8px; }"
                    " QSpinBox::up-button, QSpinBox::down-button { width: 48px; }"
                    " QSpinBox::up-arrow, QSpinBox::down-arrow { width: 20px; height: 20px; }"
                )
            except Exception:
                self.interval_spin.setMinimumWidth(80)

        self.auto_button = QPushButton("Start Auto")
        self.auto_button.setCheckable(True)
        self.auto_button.toggled.connect(self.toggle_auto)

        # Manual clock toggle: show/hide the large clock display
        self.clock_button = QPushButton("Show Clock")
        self.clock_button.setCheckable(True)
        self.clock_button.setChecked(False)
        self.clock_button.toggled.connect(self._on_clock_button_toggled)
        with suppress(AttributeError, TypeError):
            # prefer minimum width instead of fixed to avoid shrink/expand
            # behavior after show/hide cycles
            self.clock_button.setMinimumWidth(100)

        # Display mode removed: use a single QDateTimeAxis format driven
        # automatically by the window size. (Previously had Auto/Time/Date.)

        self.clear_button = QPushButton("Clear Data")
        self.clear_button.clicked.connect(self.clear_data)

        # Make control buttons larger and touch-friendly: bigger font, padding and
        # a sensible minimum size so buttons are easy to tap on embedded screens.
        with suppress(AttributeError, TypeError):
            with suppress(Exception):
                btn_font = QFont()
                # Slightly larger than default but not oversized for labels
                btn_font.setPointSize(14)
                btn_font.setBold(True)
                for b in (self.auto_button, self.clock_button, self.clear_button):
                    if b is None:
                        continue
                    with suppress(Exception):
                        b.setFont(btn_font)
                        # Make buttons taller for touch targets while keeping a
                        # reasonable minimum width so labels wrap less on small
                        # screens. Fix the height so the buttons keep a stable
                        # touch-friendly size even after layout changes.
                        b.setFixedHeight(56)
                        b.setMinimumWidth(140)
                        # Use a size policy that fixes the vertical dimension
                        # while allowing the horizontal dimension to shrink/grow
                        # with available space.
                        b.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                        # Use per-widget stylesheet to avoid affecting other widgets
                        b.setStyleSheet("QPushButton { padding: 8px 14px; }")

        # Also increase the height / padding of select widgets so they match
        # the buttons' touch-friendly targets (combo boxes, spin boxes,
        # interval input and checkbox). Use guarded access so static
        # analyzers do not flag attribute-access-before-definition.
        uc = getattr(self, "unit_combo", None)
        if uc is not None:
            try:
                uc.setMinimumHeight(48)
                uc.setStyleSheet("QComboBox { padding: 8px 10px; }")
            except Exception:
                pass

        ws = getattr(self, "window_spin", None)
        if ws is not None:
            try:
                ws.setMinimumHeight(48)
                # Make the spinbox arrow buttons wider for touch targets
                ws.setStyleSheet(
                    "QSpinBox { padding: 6px 8px; }"
                    " QSpinBox::up-button, QSpinBox::down-button { width: 48px; }"
                    " QSpinBox::up-arrow, QSpinBox::down-arrow { width: 20px; height: 20px; }"
                )
            except Exception:
                pass

        isb = getattr(self, "interval_spin", None)
        if isb is not None:
            try:
                isb.setMinimumHeight(48)
                # Make the spinbox arrow buttons wider for touch targets
                isb.setStyleSheet(
                    "QSpinBox { padding: 6px 8px; }"
                    " QSpinBox::up-button, QSpinBox::down-button { width: 48px; }"
                    " QSpinBox::up-arrow, QSpinBox::down-arrow { width: 20px; height: 20px; }"
                )
            except Exception:
                pass

        controls = QHBoxLayout()
        # More breathing room for touch targets on small embedded screens
        controls.setSpacing(6)
        controls.setContentsMargins(6, 6, 6, 6)
        # read/save buttons intentionally not shown
        # simulation checkbox removed; do not add to controls
        controls.addWidget(self.interval_spin)
        controls.addWidget(self.auto_button)
        # display selector removed

        layout = QVBoxLayout()
        # Slightly larger spacing/margins for touch readability on 800x480
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.values_label)
        # Chart: temperature (red) and humidity (blue)
        # Time-window in seconds for the X axis (scalable). Default: 1 week
        self.window_seconds = 7 * 24 * 3600  # 604800 seconds

        self._max_points = 10000
        # (no overlay labels) we rely exclusively on QDateTimeAxis
        # last shown axis range in ms (initialized None)
        self._last_start_ms: Optional[int] = None
        self._last_end_ms: Optional[int] = None
        # display mode removed; formatting is automatic based on window_seconds

        self.temp_series = QLineSeries()
        self.hum_series = QLineSeries()
        # hide per-series names to keep the chart minimal
        self.temp_series.setName("")
        self.hum_series.setName("")
        # Use slightly brighter/contrasting colors on dark backgrounds
        self.temp_series.setPen(QPen(QColor("#ff6b6b"), 3))
        self.hum_series.setPen(QPen(QColor("#65a6ff"), 3))

        self.chart = QChart()
        self.chart.addSeries(self.temp_series)
        self.chart.addSeries(self.hum_series)
        # hide legend/labels for a minimal display
        self.chart.legend().setVisible(False)

        # Apply a darker chart background and plot area for dark theme
        with suppress(AttributeError, TypeError):
            self.chart.setBackgroundBrush(QColor(18, 18, 18))
        # Some Qt versions expose plot-area background helpers
        with suppress(AttributeError, TypeError):
            self.chart.setPlotAreaBackgroundBrush(QColor(28, 28, 28))
            self.chart.setPlotAreaBackgroundVisible(True)

        # X axes: time axis (bottom) and optional date axis (top)
        self.x_axis_time = QDateTimeAxis()
        self.x_axis_time.setFormat("dd.MM. HH:mm")

        # Font for axis labels and overlay text
        self._axis_label_font = QFont()
        # Slightly larger for readability on 800x480 embedded displays
        self._axis_label_font.setPointSize(16)

        now = QDateTime.currentDateTime()
        start = QDateTime.fromMSecsSinceEpoch(
            max(0, now.toMSecsSinceEpoch() - self.window_seconds * 1000)
        )
        # set initial range on time axis and add to chart
        self.x_axis_time.setRange(start, now)
        self.chart.addAxis(self.x_axis_time, Qt.AlignBottom)
        self.temp_series.attachAxis(self.x_axis_time)
        self.hum_series.attachAxis(self.x_axis_time)

        # Axis label colors to be readable on dark backgrounds
        with suppress(AttributeError, TypeError):
            self.x_axis_time.setLabelsColor(QColor(180, 180, 180))
            # apply larger label font
            self.x_axis_time.setLabelsFont(self._axis_label_font)
        # no separate date axis; all formatting done on `x_axis_time`

        # Load recent history from DB (if available) and populate series
        self._load_history_from_db()

        # Y axes: left for temp, right for humidity
        self.y_temp = QValueAxis()
        # Fixed temperature axis range: 0°C — 30°C
        self.y_temp.setRange(0, 30)
        self.chart.addAxis(self.y_temp, Qt.AlignLeft)
        self.temp_series.attachAxis(self.y_temp)

        # Ensure Y-axis labels are readable on dark backgrounds
        with suppress(AttributeError, TypeError):
            self.y_temp.setLabelsColor(Qt.white)
            self.y_temp.setLabelsFont(self._axis_label_font)

        self.y_hum = QValueAxis()
        self.y_hum.setRange(0, 100)
        self.chart.addAxis(self.y_hum, Qt.AlignRight)
        self.hum_series.attachAxis(self.y_hum)

        with suppress(AttributeError, TypeError):
            self.y_hum.setLabelsColor(Qt.white)
            self.y_hum.setLabelsFont(self._axis_label_font)

        self.chart_view = InteractiveChartView(self.chart, parent_window=self)
        self.chart_view.setContentsMargins(0, 0, 0, 0)
        # Larger chart area by default on 800x480 displays
        self.chart_view.setMinimumHeight(200)
        # Enable antialiasing for smoother lines
        with suppress(AttributeError, TypeError):
            # Enable antialiasing for smoother lines where available
            self.chart_view.setRenderHint(QPainter.Antialiasing)
        # chart will be placed into a stacked layout together with the clock

        # Large clock widget (hidden by default). Tapping it returns to chart.
        self.clock_widget = QWidget()
        cw_layout = QVBoxLayout()
        cw_layout.setContentsMargins(0, 0, 0, 0)
        cw_layout.setSpacing(4)
        # Time label (clickable) — show HH:mm (no seconds) large
        self.time_label = ClickableLabel(on_click=self.reset_clock_timer)
        time_font = QFont()
        # Prefer the system-preferred clock family when available,
        # otherwise fall back to the bundled clock font.
        try:
            if getattr(self, "_preferred_clock_family", None):
                with suppress(Exception):
                    time_font.setFamily(self._preferred_clock_family)
        except Exception:
            # Best-effort fallback to the bundled family if present
            if getattr(self, "_clock_family", None):
                with suppress(Exception):
                    time_font.setFamily(self._clock_family)
        time_font.setPointSize(72)
        time_font.setBold(True)
        self.time_label.setFont(time_font)
        self.time_label.setAlignment(Qt.AlignCenter)
        with suppress(AttributeError, TypeError):
            self.time_label.setStyleSheet("color: #ffffff;")
        # Date label below time in gray
        self.date_label = QLabel()
        date_font = QFont()
        date_font.setPointSize(18)
        self.date_label.setFont(date_font)
        self.date_label.setAlignment(Qt.AlignCenter)
        with suppress(AttributeError, TypeError):
            self.date_label.setStyleSheet("color: #aaaaaa;")
        cw_layout.addWidget(self.time_label, stretch=3)
        cw_layout.addWidget(self.date_label, stretch=1)
        # Small stats line: show latest temperature and humidity on clock
        self.clock_stats_label = QLabel()
        stats_font = QFont()
        stats_font.setPointSize(20)
        self.clock_stats_label.setFont(stats_font)
        self.clock_stats_label.setAlignment(Qt.AlignCenter)
        with suppress(AttributeError, TypeError):
            # color via HTML when setting text; keep label color neutral
            self.clock_stats_label.setStyleSheet("color: #cccccc;")
        cw_layout.addWidget(self.clock_stats_label, stretch=0)
        self.clock_widget.setLayout(cw_layout)
        # Flip-bar removed: no overlay widget is created here
        self._flip_bar = None
        with suppress(AttributeError, TypeError):
            self.clock_widget.setSizePolicy(
                QSizePolicy.Expanding, QSizePolicy.Expanding
            )
            self.time_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.date_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Keep the in-layout clock widget for non-fullscreen fallback,
        # but do not show it by default. We'll present a fullscreen
        # `QDialog` overlay when switching to clock mode so the main
        # layout and widget sizes do not change (avoids input remapping).
        self.clock_widget.hide()
        # create a stacked layout so we can switch between the chart and
        # the clock without creating new top-level windows (safe for EGLFS)
        self._stack = QStackedLayout()
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack_widget = QWidget()
        self._stack_widget.setLayout(self._stack)
        self._stack.addWidget(self.chart_view)
        self._stack.addWidget(self.clock_widget)
        layout.addWidget(self._stack_widget)
        # default to showing the chart page
        with suppress(Exception):
            self._stack.setCurrentWidget(self.chart_view)
        # Fullscreen clock overlay (child widget created on demand).
        # Using a child overlay avoids creating a new top-level window
        # which can cause some QPA backends to reinitialize input devices.
        self._clock_dialog: Optional[QDialog] = None
        self._clock_overlay: Optional[QWidget] = None

        # Window control for time-axis (value + unit)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["Minutes", "Hours", "Days", "Weeks", "Months"])
        # default unit: Weeks
        self.unit_combo.setCurrentText("Weeks")
        with suppress(AttributeError, TypeError):
            # Make the unit selector larger so items like 'Weeks' are easily readable
            with suppress(Exception):
                uc_font = QFont()
                uc_font.setPointSize(16)
                uc_font.setBold(True)
                self.unit_combo.setFont(uc_font)
            # Match the buttons' touch-friendly height and sizing
            try:
                self.unit_combo.setFixedHeight(56)
                self.unit_combo.setMinimumWidth(140)
                self.unit_combo.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                self.unit_combo.setStyleSheet("QComboBox { padding: 8px 10px; }")
            except Exception:
                # Fall back to minimum width only
                with suppress(Exception):
                    self.unit_combo.setMinimumWidth(140)
        controls.addWidget(self.unit_combo)

        self.window_spin = QSpinBox()
        self.window_spin.setRange(1, 10000)
        # show value in chosen unit (default 1 week)
        self.window_spin.setValue(1)
        self.window_spin.setSuffix(" ")
        with suppress(AttributeError, TypeError):
            with suppress(Exception):
                ws_font = QFont()
                ws_font.setPointSize(16)
                ws_font.setBold(True)
                self.window_spin.setFont(ws_font)
            try:
                self.window_spin.setFixedHeight(56)
                self.window_spin.setMinimumWidth(90)
                self.window_spin.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                # Make the spinbox arrow buttons wider for touch targets
                self.window_spin.setStyleSheet(
                    "QSpinBox { padding: 6px 8px; }"
                    " QSpinBox::up-button, QSpinBox::down-button { width: 48px; }"
                    " QSpinBox::up-arrow, QSpinBox::down-arrow { width: 20px; height: 20px; }"
                )
            except Exception:
                with suppress(Exception):
                    self.window_spin.setMinimumWidth(100)
        controls.addWidget(self.window_spin)
        # push remaining controls to the right
        controls.addStretch()
        # Show Clock should appear left of Clear Data
        controls.addWidget(self.clock_button)
        controls.addWidget(self.clear_button)

        # wire handlers
        self.window_spin.valueChanged.connect(self._on_window_change)
        self.unit_combo.currentTextChanged.connect(self._on_unit_change)

        layout.addLayout(controls)

        self.setLayout(layout)

        # Idle/clock behavior: switch to clock after inactivity (seconds)
        try:
            self._idle_seconds = max(1, int(os.environ.get("PI_TEMP_CLOCK_IDLE", "60")))
        except (TypeError, ValueError):
            self._idle_seconds = 60

        self._clock_timer = QTimer(self)
        self._clock_timer.setSingleShot(True)
        self._clock_timer.timeout.connect(self._show_clock)

        # Timer to update the clock display every second while visible
        self._clock_update_timer = QTimer(self)
        self._clock_update_timer.setInterval(1000)
        self._clock_update_timer.timeout.connect(self._update_clock_display)

        # Hint timer removed: the small hint label is no longer auto-hidden.

        # Start the idle timer so the clock will appear after inactivity
        with suppress(AttributeError, TypeError):
            self._clock_timer.start(self._idle_seconds * 1000)
        self._last_temp: Optional[float] = None
        self._last_hum: Optional[float] = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.read_once)
        # Enable auto-start by default (60s interval)
        with suppress(AttributeError, TypeError):
            # Setting the button checked will emit `toggled` and call `toggle_auto`
            self.auto_button.setChecked(True)

        # Configure daily pruning timer (best-effort). Read env vars each
        # time the window is created so runtime configuration can be set by
        # the launcher.
        prune_enabled = os.environ.get("PI_TEMP_PRUNE_ENABLED", "1").lower() not in (
            "0",
            "false",
            "no",
            "off",
            "",
        )
        try:
            prune_months = max(0, int(os.environ.get("PI_TEMP_PRUNE_MONTHS", "3")))
        except (TypeError, ValueError):
            prune_months = 3
        self._prune_enabled = prune_enabled and prune_months > 0
        self._prune_months = prune_months

        if self._prune_enabled:
            # schedule a repeating timer to prune once a day
            self._prune_timer = QTimer(self)
            # 24 hours in milliseconds
            self._prune_timer.setInterval(24 * 3600 * 1000)
            self._prune_timer.timeout.connect(self._run_prune)
            # start the repeating timer
            with suppress(AttributeError, TypeError):
                self._prune_timer.start()

    def read_once(self) -> None:
        try:
            temp_c, hum = _read_sensor()
        except (RuntimeError, OSError, ValueError) as exc:
            # show error on the main values label so user sees it without extra widgets
            self.values_label.setText(f"Error: {exc}")
            return

        self._last_temp = temp_c
        self._last_hum = hum
        # Determine time string for this reading (HH:MM). Prefer QDateTime
        now_dt = None
        try:
            now_dt = QDateTime.currentDateTime()
        except Exception:
            now_dt = None
        try:
            if now_dt is not None:
                time_text = now_dt.toString("HH:mm")
            else:
                raise RuntimeError("no QDateTime")
        except Exception:
            try:
                time_text = datetime.now().strftime("%H:%M")
            except Exception:
                time_text = "--:--"

        # Update single-line values label (temperature red, humidity blue via HTML)
        # Append the reading time in muted gray after the values.
        self.values_label.setText(
            f"<span style='color:red'>Temperature: {temp_c} °C</span>&nbsp;&nbsp;"
            f"<span style='color:#999'>&bull;</span>&nbsp;&nbsp;"
            f"<span style='color:blue'>Humidity: {hum} %</span>"
            f"&nbsp;&nbsp;<span style='color:#888'>({time_text})</span>"
        )
        # Ensure readings are saved to the DB on every read. If DB was not
        # available at init, attempt to create it now at the default path.
        # Attempt to ensure DB exists and save; ignore DB errors
        if not self.db_path:
            candidate = os.environ.get("PI_TEMP_DB", "readings.db")
            with suppress(sqlite3.Error):
                init_db(candidate)
                self.db_path = candidate

        if self.db_path:
            with suppress(sqlite3.Error):
                save_reading(self.db_path, temp_c, hum, None, None)

        # Append to chart series using a timestamp (ms) x-value
        now_dt = QDateTime.currentDateTime()
        now_ms = int(now_dt.toMSecsSinceEpoch())
        with suppress(TypeError, AttributeError):
            # Some PySide versions expect QPointF lists; ignore append errors
            self.temp_series.append(now_ms, float(temp_c))
            self.hum_series.append(now_ms, float(hum))

        # Trim points older than the window (keep series bounded)
        cutoff_ms = now_ms - int(self.window_seconds) * 1000
        with suppress(AttributeError):
            # Different Qt bindings expose differing series APIs; ignore if removePoints unsupported
            # remove leading points until all are within the cutoff
            while self.temp_series.count() and self.temp_series.at(0).x() < cutoff_ms:
                self.temp_series.removePoints(0, 1)
            while self.hum_series.count() and self.hum_series.at(0).x() < cutoff_ms:
                self.hum_series.removePoints(0, 1)

        # Update x-axis ranges (both axes) to show [cutoff .. now]
        # set range on the axes; suppress failures on unusual Qt bindings
        with suppress(AttributeError, TypeError):
            start_dt = QDateTime.fromMSecsSinceEpoch(cutoff_ms)
            with suppress(AttributeError, TypeError):
                self.x_axis_time.setRange(start_dt, now_dt)
            # record last shown range for overlays
            with suppress(AttributeError, TypeError, ValueError):
                self._last_start_ms = int(start_dt.toMSecsSinceEpoch())
                self._last_end_ms = int(now_dt.toMSecsSinceEpoch())

        # Keep humidity axis fixed to 0%–100%
        with suppress(AttributeError, TypeError):
            self.y_hum.setRange(0, 100)

    def _load_history_from_db(self) -> None:
        """Load recent readings from the DB and populate the chart series."""
        if not self.db_path:
            return
        try:
            rows = []
            try:
                from pi_temp_humid.storage import get_recent_readings

                rows = get_recent_readings(self.db_path, limit=self._max_points)
            except (ImportError, sqlite3.Error):
                return

            if not rows:
                return

            first_ts_ms = None
            last_ts_ms = None
            for ts_iso, temp_c, hum, _sensor, _pin in rows:
                try:
                    # parse ISO timestamp to ms since epoch
                    dt = datetime.fromisoformat(ts_iso)
                    ms = int(dt.timestamp() * 1000)
                except (ValueError, TypeError):
                    # skip bad rows
                    continue
                with suppress(AttributeError, TypeError):
                    self.temp_series.append(ms, float(temp_c))
                    self.hum_series.append(ms, float(hum))
                if first_ts_ms is None:
                    first_ts_ms = ms
                last_ts_ms = ms

                # update x-axis range to show the last window (or full history if small)
                if last_ts_ms is not None:
                    with suppress(AttributeError, TypeError, ValueError):
                        end_dt = QDateTime.fromMSecsSinceEpoch(last_ts_ms)
                        # Show the configured time window ending at the last
                        # reading so date ticks reflect the full window (e.g.
                        # one week) instead of shrinking to the small history
                        # range when only a few recent rows exist.
                        start_ms = max(0, last_ts_ms - int(self.window_seconds) * 1000)
                        start_dt = QDateTime.fromMSecsSinceEpoch(start_ms)
                        with suppress(AttributeError, TypeError):
                            self.x_axis_time.setRange(start_dt, end_dt)
                        # record last shown range for overlay calculations
                        with suppress(AttributeError, TypeError, ValueError):
                            self._last_start_ms = int(start_dt.toMSecsSinceEpoch())
                            self._last_end_ms = int(end_dt.toMSecsSinceEpoch())
            # update main values label with last reading (include HH:MM)
            if last_ts_ms is not None:
                with suppress(IndexError, TypeError, ValueError):
                    raw_iso = rows[-1][0]
                    time_text = "--:--"
                    with suppress(Exception):
                        # try QDateTime parsing if available
                        try:
                            qd = QDateTime.fromString(raw_iso, Qt.ISODate)
                            if qd.isValid():
                                time_text = qd.toString("HH:mm")
                            else:
                                raise ValueError("invalid QDateTime")
                        except Exception:
                            # fallback to Python parsing
                            dt = datetime.fromisoformat(raw_iso)
                            time_text = dt.strftime("%H:%M")

                    # Expose the last-read temperature/humidity to the rest of
                    # the UI so the clock stats and other helpers can display
                    # the most-recent values immediately after startup.
                    with suppress(Exception):
                        try:
                            self._last_temp = float(rows[-1][1])
                        except Exception:
                            self._last_temp = rows[-1][1]
                    with suppress(Exception):
                        try:
                            self._last_hum = float(rows[-1][2])
                        except Exception:
                            self._last_hum = rows[-1][2]

                    self.values_label.setText(
                        f"<span style='color:red'>Temperature: {rows[-1][1]} °C</span>&nbsp;&nbsp;"
                        f"<span style='color:#999'>&bull;</span>&nbsp;&nbsp;"
                        f"<span style='color:blue'>Humidity: {rows[-1][2]} %</span>"
                        f"&nbsp;&nbsp;<span style='color:#888'>({time_text})</span>"
                    )
        except sqlite3.Error:
            # be permissive — history loading DB errors should not stop the UI
            return

    def save_last(self) -> None:
        if self._last_temp is None or self._last_hum is None:
            self.values_label.setText("No reading to save")
            return
        if not self.db_path:
            self.values_label.setText("DB not available")
            return
        try:
            save_reading(self.db_path, self._last_temp, self._last_hum, None, None)
            # briefly show saved status on the values label
            self.values_label.setText(
                f"<span style='color:green'>Saved: {self._last_temp} °C, {self._last_hum} %</span>"
            )
        except sqlite3.Error as exc:
            self.values_label.setText(f"Save error: {exc}")

    def clear_data(self) -> None:
        """Clear all stored readings from the DB and the chart series."""
        # Ask user for confirmation before clearing stored readings
        with suppress(AttributeError, RuntimeError):
            with suppress(AttributeError, RuntimeError):
                resp = QMessageBox.question(
                    self,
                    "Confirm Clear",
                    "Are you sure you want to delete all stored readings?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if resp != QMessageBox.Yes:
                    return
        # Clear DB table if available
        if not self.db_path:
            self.values_label.setText("DB not available")
            return
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM readings")
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            self.values_label.setText(f"Clear error: {exc}")
            return

        # Clear series in the chart view
        with suppress(AttributeError, TypeError):
            try:
                # preferred API
                self.temp_series.clear()
                self.hum_series.clear()
            except (AttributeError, TypeError):
                # fallback: remove points one-by-one
                with suppress(AttributeError, TypeError):
                    while self.temp_series.count():
                        self.temp_series.removePoints(0, 1)
                with suppress(AttributeError, TypeError):
                    while self.hum_series.count():
                        self.hum_series.removePoints(0, 1)

        # Reset last values and inform user
        self._last_temp = None
        self._last_hum = None
        self.values_label.setText("<span style='color:green'>Cleared data</span>")

    def toggle_auto(self, on: bool) -> None:
        if on:
            # interval value is in minutes; convert to milliseconds
            minutes = max(1, int(self.interval_spin.value()))
            interval_ms = minutes * 60 * 1000
            self.timer.start(interval_ms)
            self.auto_button.setText("Stop Auto")
        else:
            self.timer.stop()
            self.auto_button.setText("Start Auto")

    def _run_prune(self) -> None:
        """Run a best-effort prune of old DB readings using configured months.

        This method is safe to call repeatedly; failures are ignored so the
        UI remains responsive even if the DB is temporarily unavailable.
        """
        if not getattr(self, "_prune_enabled", False):
            return
        if not self.db_path:
            return
        try:
            if deleted := prune_old_readings(self.db_path, months=self._prune_months):
                with suppress(OSError, IOError):
                    print(
                        f"Pruned {deleted} readings older than {self._prune_months} months"
                    )
        except sqlite3.Error:
            # ignore DB errors during background prune
            return

    def pan_by_pixels(self, dx_px: float) -> None:
        """Pan the time axis by a number of pixels (positive pans right).

        This converts pixel delta into a timestamp shift based on the
        currently shown range and the chart plot area width.
        """
        if self._last_start_ms is None or self._last_end_ms is None:
            return
        start = int(self._last_start_ms)
        end = int(self._last_end_ms)
        plot_area = self.chart.plotArea()
        plot_width = plot_area.width() if plot_area is not None else 0
        if plot_width <= 0:
            return
        ms_per_px = (end - start) / plot_width
        delta_ms = int(dx_px * ms_per_px)
        new_start = start + delta_ms
        new_end = end + delta_ms
        # Prevent panning past the beginning of epoch
        if new_start < 0:
            new_start = 0
            new_end = new_start + (end - start)
        # Prevent panning into the future: clamp new_end to now
        now_ms = int(QDateTime.currentDateTime().toMSecsSinceEpoch())
        if new_end > now_ms:
            # shift window back so end == now_ms
            shift_back = new_end - now_ms
            new_end = now_ms
            new_start = max(0, new_start - shift_back)
        start_dt = QDateTime.fromMSecsSinceEpoch(new_start)
        end_dt = QDateTime.fromMSecsSinceEpoch(new_end)
        with suppress(AttributeError, TypeError):
            with suppress(AttributeError, TypeError):
                self.x_axis_time.setRange(start_dt, end_dt)
        with suppress(AttributeError, TypeError, ValueError):
            self._last_start_ms = int(start_dt.toMSecsSinceEpoch())
            self._last_end_ms = int(end_dt.toMSecsSinceEpoch())

    def zoom_at(self, factor: float, mouse_x: int, widget_width: int) -> None:
        """Zoom the X axis by `factor` centered at the horizontal pixel `mouse_x`.

        `factor` > 1 zooms in, < 1 zooms out.
        """
        if self._last_start_ms is None or self._last_end_ms is None:
            return
        start = int(self._last_start_ms)
        end = int(self._last_end_ms)
        if widget_width <= 0:
            return
        rel = max(0.0, min(1.0, float(mouse_x) / float(widget_width)))
        center_ms = int(start + (end - start) * rel)
        half = int((end - start) / 2.0 / factor)
        new_start = center_ms - half
        new_end = center_ms + half
        # prevent zooming past the start of epoch
        if new_start < 0:
            new_start = 0
            new_end = new_start + 2 * half
        # prevent zooming into the future: clamp new_end to now
        now_ms = int(QDateTime.currentDateTime().toMSecsSinceEpoch())
        if new_end > now_ms:
            shift_back = new_end - now_ms
            new_end = now_ms
            new_start = max(0, new_start - shift_back)
        start_dt = QDateTime.fromMSecsSinceEpoch(new_start)
        end_dt = QDateTime.fromMSecsSinceEpoch(new_end)
        with suppress(AttributeError, TypeError):
            with suppress(AttributeError, TypeError):
                self.x_axis_time.setRange(start_dt, end_dt)
        with suppress(AttributeError, TypeError, ValueError):
            self._last_start_ms = int(start_dt.toMSecsSinceEpoch())
            self._last_end_ms = int(end_dt.toMSecsSinceEpoch())

    def reset_zoom(self) -> None:
        """Reset X axis to show the configured `window_seconds` ending now."""
        now_dt = QDateTime.currentDateTime()
        now_ms = int(now_dt.toMSecsSinceEpoch())
        start_ms = max(0, now_ms - int(self.window_seconds) * 1000)
        start_dt = QDateTime.fromMSecsSinceEpoch(start_ms)
        with suppress(AttributeError, TypeError):
            with suppress(AttributeError, TypeError):
                self.x_axis_time.setRange(start_dt, now_dt)
        with suppress(AttributeError, TypeError, ValueError):
            self._last_start_ms = int(start_dt.toMSecsSinceEpoch())
            self._last_end_ms = int(now_dt.toMSecsSinceEpoch())

    def _on_window_change(self, val: int) -> None:
        """Adjust the time-window (seconds) shown on the X axis."""
        unit = self.unit_combo.currentText()
        mult = {
            "Seconds": 1,
            "Minutes": 60,
            "Hours": 3600,
            "Days": 86400,
            "Weeks": 604800,
            "Months": 2592000,
        }.get(unit, 60)
        # compute window in seconds
        self.window_seconds = val * mult
        # Use QDateTime objects for setRange to be compatible across Qt versions
        now_dt = QDateTime.currentDateTime()
        now_ms = int(now_dt.toMSecsSinceEpoch())
        start_ms = max(0, now_ms - int(self.window_seconds) * 1000)
        start_dt = QDateTime.fromMSecsSinceEpoch(start_ms)
        with suppress(AttributeError, TypeError):
            self.x_axis_time.setRange(start_dt, now_dt)
        with suppress(AttributeError, TypeError, ValueError):
            # record last shown range for overlays
            self._last_start_ms = int(start_dt.toMSecsSinceEpoch())
            self._last_end_ms = int(now_dt.toMSecsSinceEpoch())

    def _on_unit_change(self, _unit: str) -> None:
        """Recompute window when the unit selection changes."""
        # Value from spinbox is an int; reuse window change logic
        self._on_window_change(self.window_spin.value())

    def _on_clock_button_toggled(self, checked: bool) -> None:
        """Handler for the manual clock toggle button.

        When checked, show the clock; when unchecked, return to the chart.
        """
        if checked:
            self._show_clock()
        else:
            self._hide_clock()

    # -- Clock / idle methods -------------------------------------------------
    def reset_clock_timer(self) -> None:
        """Reset the idle timer that will switch the UI to the clock display.

        If the clock is currently shown, hide it and restart the timer.
        """
        with suppress(AttributeError):
            # if the fullscreen clock dialog is visible, hide it via _hide_clock
            if (
                getattr(self, "_clock_dialog", None) is not None
                and getattr(self, "_clock_dialog", None).isVisible()
            ):
                self._hide_clock()
            # also support the in-layout clock widget visibility (legacy)
            elif (
                getattr(self, "clock_widget", None) is not None
                and getattr(self, "clock_widget", None).isVisible()
            ):
                self._hide_clock()
        with suppress(AttributeError, TypeError):
            # restart single-shot timer
            if getattr(self, "_clock_timer", None) is not None:
                self._clock_timer.start(self._idle_seconds * 1000)

    def _show_clock(self) -> None:
        """Show the large clock label and start updating its time every second."""
        if getattr(self, "clock_widget", None) is None:
            return
        # Preferred safe approach: switch the visible page in the
        # stacked layout. Avoid creating new top-level dialogs which
        # can trigger some QPA backends to reinitialize input devices.
        try:
            # Use stacked layout to switch pages instead of creating new
            # top-level dialogs or overlays which can reinitialize QPA.
            with suppress(Exception):
                # ensure fonts are scaled for the clock widget
                self._scale_time_font()
            # update labels and then switch to the clock page
            self._update_clock_display()
            if getattr(self, "_stack", None) is not None:
                with suppress(Exception):
                    self._stack.setCurrentWidget(self.clock_widget)
            else:
                with suppress(Exception):
                    self.clock_widget.raise_()
                    self.clock_widget.setVisible(True)
        except Exception:
            # Fallback: show the in-layout clock widget if stacked layout fails
            with suppress(Exception):
                self._scale_time_font()
            self._update_clock_display()
            with suppress(Exception):
                self.clock_widget.raise_()
                self.clock_widget.setVisible(True)
        # mark the manual toggle if present
        with suppress(AttributeError):
            with suppress(AttributeError, TypeError):
                self.clock_button.setChecked(True)
        # start per-second updates (ignore attribute/type errors)
        with suppress(AttributeError, TypeError):
            self._clock_update_timer.start()
        # Restore legacy behavior: hide the main chart and control widgets
        # when the clock is shown so the clock becomes the primary view.
        with suppress(AttributeError, TypeError):
            with suppress(Exception):
                if getattr(self, "chart_view", None) is not None:
                    self.chart_view.setVisible(False)
            with suppress(Exception):
                if getattr(self, "values_label", None) is not None:
                    self.values_label.setVisible(False)
            for w in (
                getattr(self, "interval_spin", None),
                getattr(self, "auto_button", None),
                getattr(self, "unit_combo", None),
                getattr(self, "window_spin", None),
                getattr(self, "clear_button", None),
                getattr(self, "clock_button", None),
            ):
                if w is None:
                    continue
                with suppress(Exception):
                    w.setVisible(False)
            # ensure the in-layout clock widget is visible as a fallback
            with suppress(Exception):
                self.clock_widget.setVisible(True)

    def _hide_clock(self) -> None:
        """Hide the clock label and stop per-second updates; show chart again."""
        if getattr(self, "clock_widget", None) is None:
            return
        # stacked layout handles view switching; no custom painting cleanup needed
        # Also remove any leftover overlay/dialog references created
        # by older code paths so subsequent calls recreate them if
        # needed (keeps behavior backward-compatible).
        with suppress(Exception):
            with suppress(Exception):
                if getattr(self, "_clock_overlay", None) is not None:
                    with suppress(Exception):
                        self._clock_overlay.hide()
                    with suppress(Exception):
                        self._clock_overlay.deleteLater()
                    self._clock_overlay = None
            if self._clock_dialog is not None:
                with suppress(Exception):
                    with suppress(Exception):
                        self._clock_dialog.hide()
                with suppress(Exception):
                    with suppress(Exception):
                        self._clock_dialog.setModal(False)
                        self._clock_dialog.close()
                with suppress(Exception):
                    self._clock_dialog = None
        # ensure manual toggle cleared if present
        with suppress(AttributeError):
            with suppress(AttributeError, TypeError):
                self.clock_button.setChecked(False)
        # hint timer removed; no action needed here

        with suppress(AttributeError, TypeError):
            self._clock_update_timer.stop()
        # flip-bar support removed; nothing to hide here

        # Restore legacy behavior: show the main chart and control widgets
        # again when the clock is hidden.
        with suppress(AttributeError, TypeError):
            with suppress(Exception):
                # switch back to the chart page in the stacked layout if present
                if getattr(self, "_stack", None) is not None:
                    try:
                        self._stack.setCurrentWidget(self.chart_view)
                    except Exception:
                        # fallback to making the chart visible directly
                        if getattr(self, "chart_view", None) is not None:
                            self.chart_view.setVisible(True)
                elif getattr(self, "chart_view", None) is not None:
                    self.chart_view.setVisible(True)
            with suppress(Exception):
                if getattr(self, "values_label", None) is not None:
                    self.values_label.setVisible(True)
            for w in (
                getattr(self, "interval_spin", None),
                getattr(self, "auto_button", None),
                getattr(self, "unit_combo", None),
                getattr(self, "window_spin", None),
                getattr(self, "clear_button", None),
                getattr(self, "clock_button", None),
            ):
                if w is None:
                    continue
                with suppress(Exception):
                    w.setVisible(True)
            with suppress(Exception):
                self.clock_widget.setVisible(False)
        # Try to restore application focus so input events go to the main
        # window and its controls (some backends require an explicit focus
        # transfer after a fullscreen child was closed).
        with suppress(Exception):
            with suppress(Exception):
                # activate window and set focus to a reasonable control
                self.activateWindow()
        with suppress(Exception):
            with suppress(Exception):
                # Prefer focusing a visible interactive control
                if (
                    getattr(self, "auto_button", None) is not None
                    and self.auto_button.isVisible()
                ):
                    self.auto_button.setFocus()
                else:
                    self.setFocus()

    # Custom painting mode removed; stacked layout switches views now
    # mouse/key/paint handlers required in MainWindow.

    def _scale_time_font(self) -> None:
        """Scale `time_label` font size so the `HH:mm` text fills the clock area.

        Uses a binary search to find the largest point size that fits both width
        and height constraints. Falls back safely if Qt font metrics unavailable.
        """
        if getattr(self, "clock_widget", None) is None:
            return
        if (
            getattr(self, "time_label", None) is None
            or getattr(self, "date_label", None) is None
        ):
            return
        with suppress(AttributeError, TypeError, ValueError):
            self._extracted_from__scale_time_font_()

    # TODO Rename this here and in `_scale_time_font`
    def _extracted_from__scale_time_font_(self):
        # available area: prefer the overlay if present, otherwise
        # use the in-layout clock widget. Overlay approach avoids
        # creating a top-level window and keeps input mapping stable.
        container = None
        try:
            if (
                getattr(self, "_clock_overlay", None) is not None
                and self._clock_overlay.isVisible()
            ):
                container = self._clock_overlay
        except Exception:
            container = None
        if container is None:
            container = self.clock_widget

        # available area: widget width, and height minus date label
        cw = max(10, container.width())
        ch = max(10, container.height())
        # If the widget is not yet laid out (very small), use a
        # sensible target resolution for embedded displays so the
        # computed font is appropriate for the target device.
        TARGET_W = int(os.environ.get("PI_TEMP_TARGET_W", "800"))
        TARGET_H = int(os.environ.get("PI_TEMP_TARGET_H", "480"))
        if cw < 200 or ch < 150:
            cw = TARGET_W
            ch = TARGET_H
        date_h = 0
        try:
            date_h = self.date_label.sizeHint().height()
        except Exception:
            date_h = 0
        padding = 20
        avail_w = max(10, cw - padding)
        avail_h = max(10, ch - date_h - padding)
        # target text to measure
        try:
            sample = QDateTime.currentDateTime().toString("HH:mm")
        except Exception:
            sample = datetime.now().strftime("%H:%M")

        lo = 6
        # upper bound tuned for embedded displays; large fonts above
        # 400pt are unlikely useful. Use target height to cap size.
        hi = min(400, max(72, int(avail_h * 1.5)))
        best = lo
        while lo <= hi:
            mid = (lo + hi) // 2
            f = QFont(self.time_label.font())
            # prefer preferred clock family (system or bundled) for measurement
            if getattr(self, "_preferred_clock_family", None):
                with suppress(Exception):
                    f.setFamily(self._preferred_clock_family)
            f.setPointSize(mid)
            try:
                fm = QFontMetrics(f)
                w = fm.horizontalAdvance(sample)
                h = fm.height()
            except Exception:
                # If font metrics fail, stop and use current font
                break
            if w <= avail_w and h <= avail_h:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

            # Apply best size
        with suppress(Exception):
            new_font = QFont(self.time_label.font())
            # ensure preferred clock family preserved if present
            if getattr(self, "_preferred_clock_family", None):
                with suppress(Exception):
                    new_font.setFamily(self._preferred_clock_family)
            new_font.setPointSize(best)
            self.time_label.setFont(new_font)

    # flip-bar removed: positioning helper no longer required

    def resizeEvent(self, event):
        # When the window resizes, rescale the clock time font so it fills
        # the available clock area. Keep behavior permissive across Qt binds.
        with suppress(Exception):
            if (
                getattr(self, "clock_widget", None) is not None
                and self.clock_widget.isVisible()
            ):
                with suppress(Exception):
                    self._scale_time_font()
        # call base implementation
        try:
            return super().resizeEvent(event)
        except Exception:
            return None

    def _on_about_to_quit(self) -> None:
        """Perform best-effort cleanup when the QApplication is quitting.

        Stops timers and requests any DHT driver cleanup so GPIO lines are
        released cleanly (avoids libgpiod 'unable to set line' errors).
        """
        # Stop periodic timers
        with suppress(Exception):
            self.timer.stop()
        with suppress(Exception):
            self._clock_update_timer.stop()
        with suppress(Exception):
            self._clock_timer.stop()
        # hint timer removed; nothing to stop
        with suppress(Exception):
            self._prune_timer.stop()
        # Ask CLI module to cleanup any active DHT device
        with suppress(Exception):
            cleanup_dht_device()

    def about_to_quit(self) -> None:
        """Public wrapper for application quit handlers.

        This calls the internal `_on_about_to_quit` so external code can
        connect without touching a protected member (avoids linter warnings).
        """
        with suppress(Exception):
            self._on_about_to_quit()

    def _update_clock_display(self) -> None:
        """Update the `clock_label` text to the current time."""
        if getattr(self, "clock_widget", None) is None:
            return
        now = QDateTime.currentDateTime()
        # Show time on one line, large. Use locale-friendly long time.
        # Time without seconds; blink the colon on/off each second.
        try:
            hours = now.toString("HH")
            minutes = now.toString("mm")
            # determine parity of current second for blinking colon
            try:
                sec = int(now.toString("ss"))
            except Exception:
                sec = datetime.now().second
        except (AttributeError, TypeError):
            dt = datetime.now()
            hours = dt.strftime("%H")
            minutes = dt.strftime("%M")
            sec = dt.second
        # Date in gray below time — format using French locale
        try:
            ql = QLocale(QLocale.French)
            # use long date format in French (e.g., "dimanche 7 décembre 2025")
            date_text = ql.toString(now.date(), QLocale.LongFormat)
        except (AttributeError, TypeError):
            # fallback to Python formatting (English) if QLocale unavailable
            date_text = datetime.now().strftime("%A, %d %b %Y")
        try:
            bg_color = self.palette().color(QPalette.Window).name()
        except Exception:
            bg_color = "#000000"
        sep_color = "#ffffff" if (sec % 2 == 0) else bg_color
        # Prefer the preferred clock family for HTML labels when present.
        font_css = ""
        try:
            if getattr(self, "_preferred_clock_family", None):
                font_css = f"font-family: '{self._preferred_clock_family}';"
            elif getattr(self, "_clock_family", None):
                font_css = f"font-family: '{self._clock_family}';"
        except Exception:
            font_css = ""
        # Use double-dot + space as the blinking separator and force bold.
        sep_html = f"<span style='color:{sep_color}'>:</span>"
        time_html = (
            # font-weight:bold;
            f"<span style='color:#ffffff; {font_css}'>{hours}{sep_html}{minutes}</span>"
        )

        with suppress(AttributeError, TypeError):
            self.time_label.setText(time_html)
            self.date_label.setText(f"<span style='color:#aaaaaa'>{date_text}</span>")
            # Update the small stats line with last recorded values
            try:
                stats_html = (
                    f"<span style='color:red'>{str(self._last_temp)} °C</span>&nbsp;&nbsp;<span style='color:#999'>&bull;</span>&nbsp;&nbsp;<span style='color:blue'>{str(self._last_hum)}"
                    + " %</span>"
                    if (
                        getattr(self, "_last_temp", None) is not None
                        and getattr(self, "_last_hum", None) is not None
                    )
                    else "<span style='color:#888'>No data</span>"
                )
            except Exception:
                stats_html = "<span style='color:#888'>No data</span>"

            with suppress(AttributeError, TypeError):
                self.clock_stats_label.setText(stats_html)

            # Also update the fullscreen dialog labels if present
            with suppress(Exception):
                if getattr(self, "_dlg_time_label", None) is not None:
                    self._dlg_time_label.setText(time_html)
                if getattr(self, "_dlg_date_label", None) is not None:
                    self._dlg_date_label.setText(
                        f"<span style='color:#aaaaaa'>{date_text}</span>"
                    )
                if getattr(self, "_dlg_stats_label", None) is not None:
                    self._dlg_stats_label.setText(stats_html)

            # Also update the overlay labels if present (child overlay)
            with suppress(Exception):
                if getattr(self, "_ov_time_label", None) is not None:
                    self._ov_time_label.setText(time_html)
                if getattr(self, "_ov_date_label", None) is not None:
                    self._ov_date_label.setText(
                        f"<span style='color:#aaaaaa'>{date_text}</span>"
                    )
                if getattr(self, "_ov_stats_label", None) is not None:
                    self._ov_stats_label.setText(stats_html)


def main(argv: Optional[list[str]] = None) -> None:
    """Start the Qt application.

    If `QT_QPA_PLATFORM` is not set and the environment variable
    `PIQT_FORCE_EGLFS` is set to a truthy value, this will set the platform
    to `eglfs` to run fullscreen on embedded devices.
    """
    # allow tests to pass argv through; not used currently
    if os.environ.get("PIQT_FORCE_EGLFS") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "eglfs"

    app = QApplication(argv or [])
    # Try to set an application icon early so macOS picks it up for the Dock
    # Prefer `.icns` (macOS app icon), then `.png`, then `.svg` from package
    with suppress(AttributeError, TypeError, OSError):
        pkg_dir = os.path.dirname(__file__)
        for name in ("icon.icns", "icon.png", "icon.svg"):
            icon_path = os.path.join(pkg_dir, name)
            if os.path.exists(icon_path):
                with suppress(AttributeError, TypeError, OSError):
                    app.setWindowIcon(QIcon(icon_path))
                break
    # Try to apply a dark Fusion palette for a consistent dark theme.
    with suppress(AttributeError, RuntimeError, TypeError):
        with suppress(AttributeError, RuntimeError, TypeError):
            app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(18, 18, 18))
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, QColor(28, 28, 28))
        palette.setColor(QPalette.AlternateBase, QColor(38, 38, 38))
        palette.setColor(QPalette.ToolTipBase, Qt.white)
        palette.setColor(QPalette.ToolTipText, Qt.white)
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, QColor(35, 35, 35))
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Link, QColor(42, 130, 218))
        palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.HighlightedText, Qt.black)
        app.setPalette(palette)
        app.setStyleSheet(
            "QToolTip { color: #ffffff; background-color: #2a82da; border: 1px solid white; }"
        )
    db_path = os.environ.get("PI_TEMP_DB", "readings.db")
    # Optional pruning configuration: enable with PI_TEMP_PRUNE_ENABLED (default true)
    # and set months via PI_TEMP_PRUNE_MONTHS (default 3).
    prune_enabled = os.environ.get("PI_TEMP_PRUNE_ENABLED", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )
    prune_months_raw = os.environ.get("PI_TEMP_PRUNE_MONTHS", "3")
    try:
        prune_months = max(0, int(prune_months_raw))
    except (TypeError, ValueError):
        prune_months = 3
    if prune_enabled and db_path and prune_months > 0:
        with suppress(sqlite3.Error):
            if deleted := prune_old_readings(db_path, months=prune_months):
                # Non-critical: print a short message to stdout so users running
                # the GUI from a terminal can see that pruning occurred.
                with suppress(OSError, IOError):
                    print(f"Pruned {deleted} readings older than {prune_months} months")
    w = MainWindow(db_path=db_path)
    w.setAttribute(Qt.WA_DeleteOnClose)
    # Default to a typical 800x480 embedded display size
    w.resize(800, 480)
    # Ensure the MainWindow can perform cleanup on application quit
    with suppress(Exception):
        app.aboutToQuit.connect(w.about_to_quit)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
