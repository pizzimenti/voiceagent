import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Ensure the Kirigami / QtQuick.Controls QML modules are findable from
# any test that loads real QML (e.g., `tests/test_replay_toast.py`).
# `voiceagent-compiletest.sh` and `voiceagent-qatest.sh` set this from
# the shell; conftest mirrors that for direct `pytest` invocations
# (e.g., a developer running a single test file).
def _ensure_qml_import_path() -> None:
    candidates = ["/usr/lib/qt6/qml", "/usr/lib/qt/qml"]
    extra = [c for c in candidates if Path(c).is_dir()]
    if not extra:
        return
    for env_name in ("QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
        existing = os.environ.get(env_name, "")
        existing_parts = existing.split(":") if existing else []
        merged = extra + [p for p in existing_parts if p and p not in extra]
        os.environ[env_name] = ":".join(merged)


_ensure_qml_import_path()

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
