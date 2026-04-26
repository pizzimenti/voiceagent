import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# pytest-qt fixture overrides ----------------------------------------------
#
# By default pytest-qt instantiates `QApplication` (from QtWidgets) the
# first time any test requests `qtbot`. The trouble is that some tests
# import-time (or via `pytest`'s collection) cause a bare
# `QCoreApplication` (from QtCore) to be created first — pytest-qt then
# logs a `RuntimeWarning: Existing QApplication ... is not an instance of
# qapp_cls: <class 'QApplication'>` and downstream qtbot / waitSignal
# behavior subtly diverges from the documented contract.
#
# Pinning the class via `qapp_cls` is the documented mechanism, but on
# its own it does not run early enough — the fixture must actually be
# *requested* before any code touches `QCoreApplication.instance()`.
# The `_eager_qapp` fixture does that: session-scoped, autouse, requests
# `qapp` so the QApplication is created before the first test body
# runs.

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp_cls():
    return QApplication


@pytest.fixture(scope="session", autouse=True)
def _eager_qapp(qapp):
    # Force `qapp` to materialize at session start so any test that
    # later does `QCoreApplication.instance()` gets the QApplication
    # rather than racing to create a bare `QCoreApplication([])`.
    return qapp
