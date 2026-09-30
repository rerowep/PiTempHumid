from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import QMessageBox

from pi_temp_humid import gui
from pi_temp_humid.storage import get_recent_readings, save_reading


class FakeReader(gui.SensorReader):
    """Deliver queued readings synchronously instead of reading hardware."""

    def __init__(self):
        super().__init__(sensor="fake", pin=0)
        self.queue = []

    def start(self):
        if self.queue:
            self.finished.emit(*self.queue.pop(0))


@pytest.fixture
def window(qapp, db_path, monkeypatch):
    monkeypatch.setenv("PI_TEMP_PRUNE_ENABLED", "0")
    monkeypatch.setenv("PI_TEMP_START_CLOCK", "0")
    win = gui.MainWindow(db_path=db_path, reader=FakeReader())
    win.resize(800, 480)
    win.show()
    qapp.processEvents()
    yield win
    win.shutdown()
    win.close()
    win.deleteLater()


def _view_end(win):
    return int(win.x_axis.max())


def test_reading_is_charted_and_saved(window, db_path):
    window._reader.queue.append((21.5, 45.0))
    window.read_once()

    assert window.temp_series.count() == 1
    assert "21.5" in window.values_label.text()
    assert get_recent_readings(db_path)[-1][1:4] == (21.5, 45.0, "fake")


def test_history_label_shows_local_time(qapp, db_path, monkeypatch):
    monkeypatch.setenv("PI_TEMP_PRUNE_ENABLED", "0")
    save_reading(db_path, 19.0, 55.0, None, None)
    win = gui.MainWindow(db_path=db_path, reader=FakeReader())
    try:
        assert win.temp_series.count() == 1
        assert f"Heure: {datetime.now():%H:%M}" in win.values_label.text()
    finally:
        win.shutdown()
        win.deleteLater()


def test_shrinking_window_keeps_history(window):
    old = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000)
    window._append_points([(old, 20.0, 50.0)])
    window.unit_combo.setCurrentText("Hours")
    window._reader.queue.append((21.0, 51.0))
    window.read_once()
    window.unit_combo.setCurrentText("Weeks")

    assert window.temp_series.count() == 2


def test_history_capped(window, monkeypatch):
    monkeypatch.setattr(gui, "MAX_HISTORY", 5)
    now = QDateTime.currentMSecsSinceEpoch()
    window._append_points([(now - i * 1000, float(i), 50.0) for i in range(8, 0, -1)])
    assert len(window._times) == 5

    window._reader.queue.append((99.0, 51.0))
    window.read_once()
    assert len(window._times) == 5
    assert window._temp_points[-1].y() == 99.0


def test_chart_gets_only_visible_points_thinned(window):
    now = QDateTime.currentMSecsSinceEpoch()
    minute = 60 * 1000
    # 30 days at one reading per minute; the default view shows one week
    window._append_points([(now - i * minute, 20.0, 50.0) for i in range(30 * 24 * 60, -1, -1)])

    count = window.temp_series.count()
    assert 0 < count <= window.chart_view.width() + 3
    start, _end = window._view_range_ms()
    assert window.temp_series.at(1).x() >= start
    assert window.temp_series.at(count - 1).x() == window._times[-1]


def test_pan_into_past_stops_following(window):
    window.pan_by_pixels(-200)
    panned_end = _view_end(window)
    assert panned_end < QDateTime.currentMSecsSinceEpoch() - 60_000

    window._reader.queue.append((21.0, 51.0))
    window.read_once()
    assert _view_end(window) == panned_end

    window.reset_view()
    assert _view_end(window) >= QDateTime.currentMSecsSinceEpoch() - 1000


def test_small_pan_and_edge_zoom_keep_following(window):
    window.pan_by_pixels(-1)
    assert window._follow_live
    window.zoom_at(1.2, window.chart.plotArea().right())
    assert window._follow_live


def test_unexpected_read_error_does_not_stop_polling(monkeypatch):
    def broken():
        raise KeyError("boom")

    monkeypatch.setattr(gui, "read_simulated", broken)
    reader = gui.SensorReader(sensor="fake", pin=0, simulate=True)
    errors = []
    reader.failed.connect(errors.append)
    reader._busy = True
    reader._run()
    assert not reader._busy
    assert errors == ["'boom'"]


def test_view_never_extends_into_future(window):
    window.pan_by_pixels(10_000)
    assert _view_end(window) <= QDateTime.currentMSecsSinceEpoch()
    window.zoom_at(1 / 1.2, 400)
    assert _view_end(window) <= QDateTime.currentMSecsSinceEpoch()


def test_zoom_keeps_anchor_and_respects_minimum(window):
    start, end = window._view_range_ms()
    area = window.chart.plotArea()
    window.zoom_at(2.0, area.left())
    new_start, new_end = window._view_range_ms()
    assert new_start == pytest.approx(start, abs=1000)
    assert new_end - new_start == pytest.approx((end - start) / 2, rel=0.01)

    for _ in range(100):
        window.zoom_at(2.0, area.center().x())
    new_start, new_end = window._view_range_ms()
    assert new_end - new_start >= gui.MIN_SPAN_MS


def test_starts_on_clock_by_default(qapp, db_path, monkeypatch):
    monkeypatch.setenv("PI_TEMP_PRUNE_ENABLED", "0")
    monkeypatch.delenv("PI_TEMP_START_CLOCK", raising=False)
    win = gui.MainWindow(db_path=db_path, reader=FakeReader())
    try:
        assert win.is_clock_shown()
        win.on_user_activity()
        assert not win.is_clock_shown()
    finally:
        win.shutdown()
        win.deleteLater()


def test_activity_leaves_clock(window):
    window.show_clock()
    assert window.is_clock_shown()
    assert not window.controls.isVisible()

    window.on_user_activity()
    assert not window.is_clock_shown()
    assert window.controls.isVisible()


def test_clock_cards_show_time(window):
    # the minute may tick over during show_clock(), so accept either side of it
    before = QDateTime.currentDateTime()
    window.show_clock()
    after = QDateTime.currentDateTime()
    face = window.clock_widget
    shown = face.hours_card.text + face.minutes_card.text
    assert shown in {before.toString("HHmm"), after.toString("HHmm")}


def test_clock_wakes_at_next_minute(window):
    window.show_clock()
    second = QDateTime.currentDateTime().time().second()
    assert window._clock_tick.isActive()
    assert window._clock_tick.remainingTime() <= (60 - second) * 1000 + 20
    window.hide_clock()
    assert not window._clock_tick.isActive()


def test_new_reading_updates_clock(window):
    window.show_clock()
    window._reader.queue.append((23.4, 55.0))
    window.read_once()
    assert "23.4" in window.clock_widget.stats_label.text()


def test_flip_card_keeps_only_current_faces(window):
    window.show_clock()
    card = window.clock_widget.minutes_card
    for minute in range(10):
        card.set_text(f"{minute:02d}")
        card.grab()
    assert set(card._faces) <= {"08", "09"}


def test_clock_info_aligned_and_date_fits(window):
    window.show_clock()
    face = window.clock_widget
    assert face.date_label.geometry().left() >= face.hours_card.geometry().left()
    assert face.stats_label.geometry().right() <= face.minutes_card.geometry().right()

    face.set_info("Mercredi 30 septembre " * 3, "")
    shrunk = face.date_label.font().pixelSize()
    metrics = gui.QFontMetrics(face.date_label.font())
    assert metrics.horizontalAdvance(face.date_label.text()) <= face.date_label.width()

    face.set_info("Jeudi 1 octobre", "")
    assert face.date_label.font().pixelSize() > shrunk


def test_flip_card_animates_and_finishes(window, qapp):
    window.show_clock()
    card = window.clock_widget.minutes_card
    card.set_text("98", animate=False)
    card.set_text("99")
    assert card._anim.state() == gui.QVariantAnimation.Running
    # paint both flip phases: old top turning down, new bottom landing
    card._on_progress(0.25)
    card.grab()
    card._on_progress(0.75)
    card.grab()

    card._anim.setCurrentTime(card.FLIP_MS)
    assert card._progress == 1.0
    assert card.text == "99"


def test_interval_change_applies_while_running(window):
    window.interval_spin.setValue(7)
    assert window._read_timer.interval() == 7 * 60 * 1000


def test_stepper_buttons_change_window(window):
    minus, plus = window.window_spin.parent().findChildren(gui.QPushButton)
    plus.click()
    plus.click()
    minus.click()
    assert window.window_spin.value() == 2
    assert window.window_seconds == 2 * gui.WINDOW_UNITS["Weeks"]


def test_controls_fit_small_display(window):
    # sizeHint, not minimumSizeHint: below it the button labels get clipped
    assert window.controls.sizeHint().width() <= 800 - 16


def test_clear_data(window, db_path, monkeypatch):
    save_reading(db_path, 20.0, 50.0, None, None)
    window._reader.queue.append((21.0, 51.0))
    window.read_once()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    window.clear_data()
    assert window.temp_series.count() == 0
    assert get_recent_readings(db_path) == []


def test_shutdown_stops_timers(window):
    window.shutdown()
    assert not any(t.isActive() for t in window.findChildren(gui.QTimer))


def _ms(when):
    return int(when.timestamp() * 1000)


def test_time_ticks_one_week_ticks_each_midnight():
    end = datetime(2026, 10, 1, 20, 42)
    ticks = gui._time_ticks(_ms(end - timedelta(weeks=1)), _ms(end), max_ticks=7)
    assert [datetime.fromtimestamp(ms / 1000) for ms, _ in ticks] == [
        datetime(2026, 9, day, 0, 0) for day in range(25, 31)
    ] + [datetime(2026, 10, 1)]
    assert ticks[-1][1] == "jeu. 1"


def test_time_ticks_hours_show_date_at_midnight():
    end = datetime(2026, 10, 1, 20, 42)
    labels = [label for _, label in gui._time_ticks(_ms(end - timedelta(days=1)), _ms(end), max_ticks=7)]
    assert labels == ["01.10.", "06:00", "12:00", "18:00"]


def test_time_ticks_long_range_ticks_on_months():
    end = datetime(2026, 10, 1, 20, 42)
    ticks = gui._time_ticks(_ms(end - timedelta(days=180)), _ms(end), max_ticks=7)
    assert all(datetime.fromtimestamp(ms / 1000).day == 1 for ms, _ in ticks)
    assert 2 <= len(ticks) <= 7
