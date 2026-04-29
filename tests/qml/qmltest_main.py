"""Driver for VoiceAgent's Qt Quick Test (qmltestrunner) suite.

Why this exists rather than calling `qmltestrunner` directly:

The production QML files reference `i18nCtx.i18n(...)` as a *context
property* injected by `MainWindow` at engine bring-up. Bare
`qmltestrunner` has no hook to set engine-level context properties,
so loading SessionSetupPane.qml (or any file under
`src/voiceagent/qml/`) against a stock qmltestrunner engine fails
with `ReferenceError: i18nCtx is not defined`.

`QtQuickTest.QUICK_TEST_MAIN_WITH_SETUP` accepts a *setup type*
(plain Python class) whose instance receives `qmlEngineAvailable(...)`
before any tst_*.qml file loads. We use that hook to install a
`TranslatorContext` shim — the same identity-pass shim the production
window installs — under the `i18nCtx` name. With that in place, all
the production QML files load correctly under qmltest.

Usage:
    .venv/bin/python -m tests.qml.qmltest_main \\
        [extra qmltestrunner args]

`voiceagent-qatest.sh` invokes this module after the pytest pass.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Allow `XMLHttpRequest` against `file://` URLs from inside the loaded
# QML tests. `tst_layout_policy.qml` uses synchronous XHR to slurp the
# production QML source files and assert the AGENTS.md
# "Responsive layout policy" invariant that no `Behavior on opacity`
# clauses exist anywhere — the QML object tree does NOT expose
# `Behavior` value-interceptors through `children`/`data`/`resources`,
# so a runtime tree walk cannot enforce the same invariant. Reading the
# source text is the only path. Qt 6 disables file:// reads from QML
# XHR by default for security; this `setdefault` flips the gate ON for
# the test runner only (production engines never set this var).
os.environ.setdefault("QML_XHR_ALLOW_FILE_READ", "1")

from PySide6 import QtQuickTest  # noqa: E402
from PySide6.QtCore import QObject, Slot  # noqa: E402
from PySide6.QtQml import QQmlEngine  # noqa: E402

from voiceagent.i18n import TranslatorContext  # noqa: E402


class QmlTestSetup(QObject):
    """Setup object handed to `QUICK_TEST_MAIN_WITH_SETUP`.

    Qt Quick Test calls `qmlEngineAvailable(QQmlEngine)` on this
    instance after creating the engine and before parsing any
    `tst_*.qml` file. We use that window to register the same
    `i18nCtx` context property the production app installs, so the
    test files can load `SessionSetupPane.qml` / `MainWindow.qml`
    against the real Kirigami import path without ReferenceErrors.
    """

    def __init__(self) -> None:
        super().__init__()
        # Hold a strong reference so Qt does not GC the translator
        # while the engine still binds against it.
        self._translator = TranslatorContext(self)

    @Slot(QQmlEngine)
    def qmlEngineAvailable(self, engine: QQmlEngine) -> None:  # noqa: N802 - Qt API
        engine.rootContext().setContextProperty("i18nCtx", self._translator)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    # `dir` is the input directory for tst_*.qml files. Pass our
    # `tests/qml/` directory so the tests live next to this driver.
    return QtQuickTest.QUICK_TEST_MAIN_WITH_SETUP(
        "voiceagent_qmltest",
        QmlTestSetup,
        argv,
        str(_HERE),
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
