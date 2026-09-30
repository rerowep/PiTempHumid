import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def db_path(tmp_path):
    from pi_temp_humid.storage import init_db

    path = str(tmp_path / "readings.db")
    init_db(path)
    return path
