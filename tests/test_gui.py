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
        return True


@pytest.fixture
def window(qapp, db_path, monkeypatch):
    monkeypatch.setenv("PI_TEMP_PRUNE_ENABLED", "0")
    win = gui.MainWindow(db_path=db_path, reader=FakeReader())
    win.resize(800, 480)
    win.show()
    qapp.processEvents()
    yield win
    win.shutdown()
    win.close()
    win.deleteLater()


def _view_end(win):
    return win.x_axis.max().toMSecsSinceEpoch()


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
        assert f"({datetime.now():%H:%M})" in win.values_label.text()
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


def test_series_capped_at_max_points(window, monkeypatch):
    monkeypatch.setattr(gui, "MAX_POINTS", 5)
    now = QDateTime.currentMSecsSinceEpoch()
    window._append_points([(now - i * 1000, float(i), 50.0) for i in range(8, 0, -1)])
    assert window.temp_series.count() == 5

    window._reader.queue.append((99.0, 51.0))
    window.read_once()
    assert window.temp_series.count() == 5
    assert window.temp_series.at(4).y() == 99.0


def test_pan_into_past_stops_following(window):
    window.pan_by_pixels(-200)
    panned_end = _view_end(window)
    assert panned_end < QDateTime.currentMSecsSinceEpoch() - 60_000

    window._reader.queue.append((21.0, 51.0))
    window.read_once()
    assert _view_end(window) == panned_end

    window.reset_view()
    assert _view_end(window) >= QDateTime.currentMSecsSinceEpoch() - 1000


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


def test_activity_leaves_clock(window):
    window.show_clock()
    assert window.is_clock_shown()
    assert not window.controls.isVisible()

    window.on_user_activity()
    assert not window.is_clock_shown()
    assert window.controls.isVisible()


def test_interval_change_applies_while_running(window):
    window.interval_spin.setValue(7)
    assert window._read_timer.interval() == 7 * 60 * 1000


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
