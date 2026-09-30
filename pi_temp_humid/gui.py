"""Qt touchscreen GUI for pi_temp_humid.

Shows the latest reading and a temperature/humidity chart with pan and zoom,
and switches to a large clock after a period of inactivity. Built for a small
800x480 display running Qt's EGLFS platform: views are switched inside one
window instead of opening new top-level windows.
"""

from __future__ import annotations

import math
import os
import signal
import sqlite3
import sys
import threading
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta

from PySide6.QtCharts import QCategoryAxis, QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import (
    QDate,
    QDateTime,
    QEasingCurve,
    QEvent,
    QLocale,
    QMargins,
    QObject,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
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
MUTED_COLOR = "#999999"
# Day and month names on the chart axis and the clock.
FRENCH = QLocale(QLocale.French)

# Readings kept in memory (~6 months at 5 min). The chart itself only gets
# the visible ones, thinned to about one per pixel, so this barely affects
# drawing speed.
MAX_HISTORY = 50000
# Smallest time span the chart can be zoomed into.
MIN_SPAN_MS = 60 * 1000
# Pans or zooms ending at most this many pixels before the live edge keep following.
LIVE_SLACK_PX = 12
TOUCH_HEIGHT = 56
STEP_BUTTON_WIDTH = 48
# Candidate time-axis tick spacings in seconds, up to two weeks; longer
# ranges tick on the first of the month.
TICK_STEPS_S = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 2 * 86400, 7 * 86400, 14 * 86400)
# Horizontal room one time-axis label needs, so labels never overlap.
TIME_LABEL_PX = 100
WINDOW_UNITS = {"Minutes": 60, "Hours": 3600, "Days": 86400, "Weeks": 604800, "Months": 2592000}
ACTIVITY_EVENTS = {QEvent.MouseButtonPress, QEvent.TouchBegin, QEvent.KeyPress, QEvent.Wheel}


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
    """Return a font of ``point_size`` as it would be at 96 dpi.

    Sized in pixels so text does not depend on the DPI the display reports;
    on EGLFS that DPI comes from QT_QPA_EGLFS_PHYSICAL_* and is often wrong.
    """
    font = QFont()
    font.setPixelSize(round(point_size * 96 / 72))
    font.setBold(bold)
    return font


# Grey dot between values, shared by the chart header and the clock.
# Rich text collapses runs of spaces, so the padding is non-breaking spaces.
VALUE_SEPARATOR = f"&nbsp;&nbsp;&nbsp;<span style='color:{MUTED_COLOR}'>&bull;</span>&nbsp;&nbsp;&nbsp;"


def _values_html(temp: object, hum: object, labelled: bool = False) -> str:
    # short labels: the long ones wrapped on the Pi's 800 px screen
    temp_label, hum_label = ("Temp: ", "Hum: ") if labelled else ("", "")
    return (
        f"<span style='color:{TEMP_COLOR}'>{temp_label}{temp}°C</span>{VALUE_SEPARATOR}"
        f"<span style='color:{HUM_COLOR}'>{hum_label}{hum}%</span>"
    )


def _header_html(temp: object, hum: object, time: str) -> str:
    time_html = f"<span style='color:{MUTED_COLOR}'>Heure: {time}</span>"
    return f"{_values_html(temp, hum, labelled=True)}{VALUE_SEPARATOR}{time_html}"


def _add_months(day: datetime, months: int) -> datetime:
    years, month = divmod(day.month - 1 + months, 12)
    return day.replace(year=day.year + years, month=month + 1)


def _time_ticks(start_ms: int, end_ms: int, max_ticks: int) -> list[tuple[int, str]]:
    """Return time-axis ticks on round local times, with their labels.

    Ticks fall on whole minutes/hours, midnights, Mondays or the first of a
    month, so a week shows one label per day instead of seven arbitrary
    "dd.MM. HH:mm" timestamps.

    :param start_ms: Start of the visible range (ms since the epoch).
    :type start_ms: int
    :param end_ms: End of the visible range (ms since the epoch).
    :type end_ms: int
    :param max_ticks: Most labels that fit side by side.
    :type max_ticks: int
    :returns: ``(ms, label)`` pairs inside the range, in increasing order.
    :rtype: list[tuple[int, str]]
    """
    span_s = (end_ms - start_ms) / 1000
    start = datetime.fromtimestamp(start_ms / 1000)
    end = datetime.fromtimestamp(end_ms / 1000)
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)

    step_s = next((step for step in TICK_STEPS_S if span_s / step <= max_ticks), None)
    if step_s is None:
        months = next((m for m in (1, 2, 3, 6) if span_s / (m * 30 * 86400) <= max_ticks), None)
        months = months or 12 * math.ceil(span_s / (365 * 86400) / max_ticks)
        align = min(months, 12)
        tick = midnight.replace(day=1, month=(start.month - 1) // align * align + 1)

        def advance(when: datetime) -> datetime:
            return _add_months(when, months)

        def label(when: datetime) -> str:
            return FRENCH.toString(QDate(when.year, when.month, 1), "MMM yyyy")
    else:
        if step_s >= 7 * 86400:
            tick = midnight - timedelta(days=midnight.weekday())  # Monday
        else:
            # Naive local datetimes keep ticks on wall-clock times across DST.
            tick = midnight + timedelta(seconds=step_s * math.ceil((start - midnight).total_seconds() / step_s))

        def advance(when: datetime) -> datetime:
            return when + timedelta(seconds=step_s)

        def label(when: datetime) -> str:
            if step_s >= 7 * 86400:
                return f"{when:%d.%m.}"
            if step_s >= 86400:
                return FRENCH.toString(QDate(when.year, when.month, when.day), "ddd d")
            # a day boundary inside an hourly axis shows the date instead
            return f"{when:%d.%m.}" if when.hour == when.minute == 0 else f"{when:%H:%M}"

    ticks = []
    while tick <= end:
        if tick > start:
            ticks.append((int(tick.timestamp() * 1000), label(tick)))
        tick = advance(tick)
    return ticks


def _make_touch_friendly(widget: QWidget, min_width: int, point_size: int = 14) -> None:
    widget.setFont(_font(point_size, bold=True))
    widget.setFixedHeight(TOUCH_HEIGHT)
    widget.setMinimumWidth(min_width)
    widget.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    if isinstance(widget, QSpinBox):
        widget.setStyleSheet("QSpinBox { padding: 6px 2px; }")
    elif isinstance(widget, QComboBox):
        widget.setStyleSheet("QComboBox { padding: 8px 6px; }")
    else:
        widget.setStyleSheet("QPushButton { padding: 8px 6px; }")


def _touch_stepper(spin: QSpinBox) -> QWidget:
    """Put large − and + buttons on either side of ``spin``.

    QSpinBox stacks its arrows vertically, so each one is only half the row
    height and hard to hit on a touchscreen.

    :param spin: Spin box to wrap; its own arrows are hidden.
    :type spin: QSpinBox
    :returns: Widget holding the buttons and the spin box.
    :rtype: QWidget
    """
    spin.setButtonSymbols(QSpinBox.NoButtons)
    spin.setAlignment(Qt.AlignCenter)
    stepper = QWidget()
    layout = QHBoxLayout(stepper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    for text, step in (("−", spin.stepDown), ("+", spin.stepUp)):
        button = QPushButton(text, autoRepeat=True, focusPolicy=Qt.NoFocus)
        button.setFont(_font(22, bold=True))
        button.setFixedSize(STEP_BUTTON_WIDTH, TOUCH_HEIGHT)
        button.clicked.connect(step)
        layout.addWidget(button)
    layout.insertWidget(1, spin)
    return stepper


def _clock_font_family() -> str | None:
    """Prefer Helvetica for the flip cards."""
    return "Helvetica" if "Helvetica" in QFontDatabase.families() else None


class FlipCard(QWidget):
    """One split-flap card, like on a mechanical flip clock.

    Setting a new text folds the upper flap down over the old value: first the
    old top half turns down to the hinge, then the new bottom half drops into
    place. The flaps are scaled around the hinge line and shaded as they turn.
    """

    ASPECT = 1.15  # card width / height
    FLIP_MS = 600

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.text = ""
        self._old_text = ""
        self._progress = 1.0
        self._faces: dict[str, QPixmap] = {}
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=self.FLIP_MS)
        self._anim.setEasingCurve(QEasingCurve.InQuad)
        self._anim.valueChanged.connect(self._on_progress)

    def set_text(self, text: str, animate: bool = True) -> None:
        """Show ``text``, flipping from the current value if ``animate``."""
        if text == self.text:
            return
        self._old_text, self.text = self.text, text
        # only the faces of the current flip are ever drawn
        self._faces = {key: face for key, face in self._faces.items() if key in (text, self._old_text)}
        self._anim.stop()
        if animate and self._old_text and self.isVisible():
            self._progress = 0.0
            self._anim.start()
        else:
            self._progress = 1.0
        self.update()

    def _on_progress(self, value: float) -> None:
        self._progress = value
        self.update()

    def resizeEvent(self, event):
        self._faces.clear()
        super().resizeEvent(event)

    def _card_rect(self) -> QRectF:
        height = min(self.height(), self.width() / self.ASPECT)
        width = height * self.ASPECT
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _face(self, text: str) -> QPixmap:
        """Render the complete, unsplit card showing ``text`` (cached per size)."""
        if face := self._faces.get(text):
            return face
        rect = self._card_rect()
        dpr = self.devicePixelRatioF()
        face = QPixmap(int(rect.width() * dpr), int(rect.height() * dpr))
        face.setDevicePixelRatio(dpr)
        face.fill(Qt.transparent)
        width, height = rect.width(), rect.height()
        painter = QPainter(face)
        painter.setRenderHint(QPainter.Antialiasing)
        gradient = QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0.0, QColor("#3a3a3a"))
        gradient.setColorAt(0.5, QColor("#262626"))
        gradient.setColorAt(0.5001, QColor("#2e2e2e"))
        gradient.setColorAt(1.0, QColor("#1c1c1c"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(0, 0, width, height), height * 0.07, height * 0.07)

        font = QFont(self.font())
        if family := _clock_font_family():
            font.setFamily(family)
        font.setBold(True)
        font.setPixelSize(max(1, int(height * 0.78)))
        if (advance := QFontMetrics(font).horizontalAdvance(text)) > width * 0.86:
            font.setPixelSize(max(1, int(font.pixelSize() * width * 0.86 / advance)))
        painter.setFont(font)
        painter.setPen(QColor("#f2f2f2"))
        painter.drawText(QRectF(0, 0, width, height), Qt.AlignCenter, text)
        painter.end()
        self._faces[text] = face
        return face

    def paintEvent(self, event):
        if not self.text:
            return
        rect = self._card_rect()
        top = QRectF(rect.left(), rect.top(), rect.width(), rect.height() / 2)
        bottom = top.translated(0, top.height())
        hinge = top.bottom()
        new_face = self._face(self.text)
        old_face = self._face(self._old_text) if self._progress < 1 else new_face
        dpr = new_face.devicePixelRatio()

        def draw_half(face: QPixmap, half: QRectF) -> None:
            source = QRectF(0, (half.top() - rect.top()) * dpr, face.width(), half.height() * dpr)
            painter.drawPixmap(half, face, source)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        # what is left once the flap has turned: new top, old bottom
        draw_half(new_face, top)
        draw_half(old_face, bottom)
        if self._progress < 1:
            # first half of the flip: old top turns down; second: new bottom lands
            turn = math.cos(self._progress * math.pi)
            flap, face = (top, old_face) if turn > 0 else (bottom, new_face)
            painter.save()
            painter.translate(0, hinge)
            painter.scale(1, abs(turn))
            painter.translate(0, -hinge)
            draw_half(face, flap)
            path = QPainterPath()
            path.addRoundedRect(rect, rect.height() * 0.07, rect.height() * 0.07)
            clip = QPainterPath()
            clip.addRect(flap)
            painter.fillPath(path.intersected(clip), QColor(0, 0, 0, int(160 * (1 - abs(turn)))))
            painter.restore()

        # the gap between the flaps and the hinge notches on both sides
        gap = max(2.0, rect.height() * 0.012)
        painter.fillRect(QRectF(rect.left(), hinge - gap / 2, rect.width(), gap), QColor(BG_COLOR))
        notch_w, notch_h = rect.width() * 0.025, rect.height() * 0.09
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#0b0b0b"))
        for x in (rect.left() - notch_w / 2, rect.right() - notch_w / 2):
            painter.drawRoundedRect(QRectF(x, hinge - notch_h / 2, notch_w, notch_h), notch_w / 2, notch_w / 2)


class ClockFace(QWidget):
    """Hours and minutes flip cards with the date and last reading below.

    Placed by hand instead of with a layout so the info line lines up with
    the card edges and scales with the cards.
    """

    MARGIN = 16
    CARD_GAP = 24
    INFO_RATIO = 0.13  # info line height / card height
    INFO_SPACING = 14

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hours_card = FlipCard(self)
        self.minutes_card = FlipCard(self)
        self.date_label = QLabel(self, alignment=Qt.AlignLeft | Qt.AlignVCenter)
        self.date_label.setStyleSheet(f"color: {MUTED_COLOR};")
        self.stats_label = QLabel(self, alignment=Qt.AlignRight | Qt.AlignVCenter)

        self._info_px = 1

    def set_info(self, date_text: str, stats_html: str) -> None:
        if (date_text, stats_html) == (self.date_label.text(), self.stats_label.text()):
            return
        self.date_label.setText(date_text)
        self.stats_label.setText(stats_html)
        self._apply_info_fonts()

    def resizeEvent(self, event):
        width, height = self.width() - 2 * self.MARGIN, self.height() - 2 * self.MARGIN
        card_h = min(
            (width - self.CARD_GAP) / 2 / FlipCard.ASPECT,
            (height - self.INFO_SPACING) / (1 + self.INFO_RATIO),
        )
        card_w = card_h * FlipCard.ASPECT
        info_h = card_h * self.INFO_RATIO
        left = (self.width() - 2 * card_w - self.CARD_GAP) / 2
        top = (self.height() - card_h - self.INFO_SPACING - info_h) / 2
        right = left + card_w + self.CARD_GAP
        info_top = top + card_h + self.INFO_SPACING
        self.hours_card.setGeometry(QRectF(left, top, card_w, card_h).toRect())
        self.minutes_card.setGeometry(QRectF(right, top, card_w, card_h).toRect())
        # inset by the card corner radius so the text lines up with the straight edges
        inset = card_h * 0.07
        for label, x in ((self.date_label, left + inset), (self.stats_label, right)):
            label.setGeometry(QRectF(x, info_top, card_w - inset, info_h).toRect())
        self._info_px = max(1, int(info_h * 0.75))
        self._apply_info_fonts()
        super().resizeEvent(event)

    def _apply_info_fonts(self) -> None:
        """Size the info line to the cards, shrinking the date if a long day or month overflows."""
        font = QFont()
        font.setPixelSize(self._info_px)
        self.stats_label.setFont(font)
        width, text = self.date_label.width(), self.date_label.text()
        while width > 0 and font.pixelSize() > 1 and QFontMetrics(font).horizontalAdvance(text) > width:
            font.setPixelSize(font.pixelSize() - 1)
        self.date_label.setFont(font)


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

    def start(self) -> None:
        """Start a read unless one is already running."""
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            temp, hum = read_simulated() if self.simulate else read_sensor(self.sensor, self.pin)
        except Exception as exc:
            # Thread boundary: anything escaping here would leave `_busy` set
            # and silently stop all further reads.
            self._busy = False
            self.failed.emit(str(exc) or type(exc).__name__)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # the number of drawn points depends on the width
        self._window._refresh_series()


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
        self._live_end_ms = 0
        self._last_temp: float | None = None
        self._last_hum: float | None = None
        # Full history, sorted by time; `_times` holds the x values for bisect.
        self._times: list[int] = []
        self._temp_points: list[QPointF] = []
        self._hum_points: list[QPointF] = []

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
        # restarted by _update_clock_display() to fire right after each full minute
        self._clock_tick = QTimer(self, singleShot=True, timerType=Qt.PreciseTimer, timeout=self._update_clock_display)
        if self._prune_months:
            QTimer(self, interval=24 * 3600 * 1000, timeout=self._run_prune).start()
        self._idle_timer.start(self._idle_ms)
        # Any tap or key press anywhere counts as activity for the idle clock.
        QApplication.instance().installEventFilter(self)
        # Emits `toggled`, which starts polling and takes a first reading.
        self.auto_button.setChecked(True)
        if _env_flag("PI_TEMP_START_CLOCK", "1"):
            self.show_clock()

    # -- UI construction ------------------------------------------------------
    def _build_ui(self) -> None:
        self.values_label = QLabel(_header_html("--", "--", "--:--"))
        self.values_label.setFont(_font(18, bold=True))
        self.values_label.setAlignment(Qt.AlignCenter)
        # long error messages wrap instead of widening the window past the screen
        self.values_label.setWordWrap(True)
        self.values_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.chart_view = self._build_chart()
        self.clock_widget = ClockFace()
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
        self.window_spin = QSpinBox(minimum=1, maximum=999, value=1)
        self.window_spin.valueChanged.connect(self._on_window_change)
        self.clock_button = QPushButton("Clock")
        self.clock_button.clicked.connect(self.show_clock)
        self.clear_button = QPushButton("Clear Data")
        self.clear_button.clicked.connect(self.clear_data)

        _make_touch_friendly(self.interval_spin, 44, point_size=16)
        _make_touch_friendly(self.auto_button, 100)
        _make_touch_friendly(self.unit_combo, 100, point_size=16)
        _make_touch_friendly(self.window_spin, 44, point_size=16)
        _make_touch_friendly(self.clock_button, 80)
        _make_touch_friendly(self.clear_button, 100)

        self.controls = QWidget()
        controls = QHBoxLayout(self.controls)
        controls.setSpacing(6)
        controls.setContentsMargins(6, 6, 6, 6)
        controls.addWidget(_touch_stepper(self.interval_spin))
        controls.addWidget(self.auto_button)
        controls.addWidget(self.unit_combo)
        controls.addWidget(_touch_stepper(self.window_spin))
        controls.addStretch()
        controls.addWidget(self.clock_button)
        controls.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 4, 8, 8)
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
        # the default margins and rounded background waste a lot of the small screen
        self.chart.setBackgroundRoundness(0)
        self.chart.layout().setContentsMargins(0, 0, 0, 0)
        self.chart.setMargins(QMargins(0, 6, 0, 0))

        axis_font = _font(13)
        # A category axis lets _update_time_ticks() put labels on round local
        # times; QDateTimeAxis can only split the range into equal parts.
        self.x_axis = QCategoryAxis()
        self.x_axis.setLabelsPosition(QCategoryAxis.AxisLabelsPositionOnValue)
        self.x_axis.setLabelsColor(QColor(180, 180, 180))
        # 0-30 in six ticks (steps of 6) lines up with humidity 0-100 in steps of 20
        self.y_temp = QValueAxis(labelFormat="%d", tickCount=6)
        self.y_temp.setRange(0, 30)
        self.y_temp.setLabelsColor(QColor(TEMP_COLOR))
        self.y_hum = QValueAxis(labelFormat="%d%%", tickCount=6)
        self.y_hum.setRange(0, 100)
        self.y_hum.setLabelsColor(QColor(HUM_COLOR))
        # same tick positions as the temperature axis; one set of grid lines is enough
        self.y_hum.setGridLineVisible(False)
        axes = ((self.x_axis, Qt.AlignBottom), (self.y_temp, Qt.AlignLeft), (self.y_hum, Qt.AlignRight))
        for axis, alignment in axes:
            axis.setLabelsFont(axis_font)
            self.chart.addAxis(axis, alignment)
        for series, y_axis in ((self.temp_series, self.y_temp), (self.hum_series, self.y_hum)):
            series.attachAxis(self.x_axis)
            series.attachAxis(y_axis)

        view = InteractiveChartView(self.chart, self)
        view.setMinimumHeight(200)
        return view

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
        if self.is_clock_shown():
            self._update_clock_display()

    def _on_read_error(self, message: str) -> None:
        self.values_label.setText(f"Error: {message}")

    def _show_values(self, temp: float, hum: float, when: datetime) -> None:
        self.values_label.setText(_header_html(temp, hum, f"{when:%H:%M}"))

    def _append_points(self, points: list[tuple[int, float, float]]) -> None:
        """Add readings, sorted by time and newer than the existing ones."""
        for ms, temp, hum in points:
            self._times.append(ms)
            self._temp_points.append(QPointF(ms, temp))
            self._hum_points.append(QPointF(ms, hum))
        if (excess := len(self._times) - MAX_HISTORY) > 0:
            for history in (self._times, self._temp_points, self._hum_points):
                del history[:excess]
        self._refresh_series()

    def _refresh_series(self) -> None:
        """Give the chart only the visible points, thinned to about one per pixel.

        QtCharts recomputes every point of a series on each redraw, so passing
        the full history made panning slow on a Pi.
        """
        start, end = self._view_range_ms()
        # one point beyond each edge so the lines reach the plot borders
        lo = max(0, bisect_left(self._times, start) - 1)
        hi = min(len(self._times), bisect_right(self._times, end) + 1)
        width = self.chart_view.width()
        step = max(1, math.ceil((hi - lo) / (width if width >= 100 else 800)))
        for series, history in ((self.temp_series, self._temp_points), (self.hum_series, self._hum_points)):
            visible = history[lo:hi:step]
            if step > 1 and (hi - 1 - lo) % step:
                # always draw the newest visible reading
                visible.append(history[hi - 1])
            # replace() updates the chart once; append(list) once per point
            series.replace(visible)

    def _load_history(self) -> None:
        if not self.db_path:
            return
        try:
            rows = get_recent_readings(self.db_path, limit=MAX_HISTORY)
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
        for history in (self._times, self._temp_points, self._hum_points):
            history.clear()
        self._refresh_series()
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
        return int(self.x_axis.min()), int(self.x_axis.max())

    def _set_view(self, start_ms: int, end_ms: int) -> None:
        # Shift the range back so it never extends into the future.
        span = end_ms - start_ms
        end_ms = min(end_ms, _now_ms())
        start_ms = max(0, end_ms - span)
        self.x_axis.setRange(start_ms, end_ms)
        self._update_time_ticks(start_ms, end_ms)
        self._refresh_series()

    def _update_time_ticks(self, start_ms: int, end_ms: int) -> None:
        width = self.chart.plotArea().width()
        max_ticks = max(2, int((width if width >= 100 else 600) / TIME_LABEL_PX))
        for label in self.x_axis.categoriesLabels():
            self.x_axis.remove(label)
        self.x_axis.setStartValue(start_ms)
        for index, (ms, label) in enumerate(_time_ticks(start_ms, end_ms, max_ticks)):
            # category labels must be unique, but "06:00" can recur on another
            # day; invisible zero-width spaces tell them apart
            self.x_axis.append(label + "\u200b" * index, ms)

    def reset_view(self) -> None:
        """Show the configured time window ending now and follow new readings."""
        self._follow_live = True
        now = self._live_end_ms = _now_ms()
        self._set_view(now - self.window_seconds * 1000, now)

    def _keeps_live(self, end_ms: int, span_ms: int, width: float) -> bool:
        # Compare with the end of the last live view, not with "now": the axis
        # end lags "now" by up to one poll interval, so any tiny pan or a zoom
        # anchored at the right edge would otherwise count as leaving live.
        return end_ms >= self._live_end_ms - LIVE_SLACK_PX * span_ms / width

    def pan_by_pixels(self, dx_px: float) -> None:
        """Shift the time axis by ``dx_px`` pixels; positive moves towards now."""
        width = self.chart.plotArea().width()
        if width <= 0:
            return
        start, end = self._view_range_ms()
        delta = int(dx_px * (end - start) / width)
        self._follow_live = self._keeps_live(end + delta, end - start, width)
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
        self._follow_live = self._keeps_live(new_end, new_end - new_start, area.width())
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
        self._update_clock_display(animate=False)

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
        # runs for every event in the app, so keep it minimal
        if event.type() in ACTIVITY_EVENTS:
            self.on_user_activity()
        return False

    def _update_clock_display(self, animate: bool = True) -> None:
        now = QDateTime.currentDateTime()
        face = self.clock_widget
        face.hours_card.set_text(now.toString("HH"), animate)
        face.minutes_card.set_text(now.toString("mm"), animate)
        date_text = FRENCH.toString(now.date(), "dddd d MMMM")
        if self._last_temp is None or self._last_hum is None:
            stats_html = f"<span style='color:{MUTED_COLOR}'>No data</span>"
        else:
            stats_html = _values_html(self._last_temp, self._last_hum)
        face.set_info(date_text[:1].upper() + date_text[1:], stats_html)
        time = now.time()
        self._clock_tick.start(60_000 - time.second() * 1000 - time.msec() + 20)

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
        (QPalette.Text, Qt.white),
        (QPalette.Button, QColor(35, 35, 35)),
        (QPalette.ButtonText, Qt.white),
        (QPalette.Highlight, QColor(42, 130, 218)),
        (QPalette.HighlightedText, Qt.black),
    ):
        palette.setColor(role, color)
    app.setPalette(palette)


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
    eglfs = os.environ.get("QT_QPA_PLATFORM") == "eglfs"
    if eglfs:
        # Lay out in real screen pixels. Qt otherwise derives a scale factor
        # from the reported physical size, which makes text too big and lets
        # touch positions drift away from the drawn buttons.
        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

    app = QApplication(argv if argv is not None else sys.argv)
    app.setWindowIcon(QIcon(os.path.join(PKG_DIR, "icon.svg")))
    _apply_dark_theme(app)

    window = MainWindow(db_path=os.environ.get("PI_TEMP_DB", "readings.db"))
    app.aboutToQuit.connect(window.shutdown)
    _install_quit_handlers(app, window)
    screen = app.primaryScreen()
    print(
        f"Screen {screen.size().width()}x{screen.size().height()}, scale {screen.devicePixelRatio()},"
        f" {screen.logicalDotsPerInch():.0f} dpi",
        file=sys.stderr,
    )
    if eglfs:
        # Fullscreen from the start, so the first layout already has the final size.
        window.showFullScreen()
    else:
        window.resize(800, 480)
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
