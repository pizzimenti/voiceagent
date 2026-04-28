#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

if [ ! -x "${VENV_PYTHON}" ]; then
  echo "Missing virtualenv python at ${VENV_PYTHON}" >&2
  exit 1
fi

# Include the worktree root on PYTHONPATH so `tests.fakes` is importable
# alongside the `voiceagent` package under `src/`.
if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}:${PYTHONPATH}"
else
  export PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}"
fi

QML_PATHS=""
for candidate in /usr/lib/qt6/qml /usr/lib/qt/qml; do
  if [ -d "${candidate}" ]; then
    if [ -n "${QML_PATHS}" ]; then
      QML_PATHS="${QML_PATHS}:${candidate}"
    else
      QML_PATHS="${candidate}"
    fi
  fi
done

if [ -n "${QML_PATHS}" ]; then
  if [ -n "${QML_IMPORT_PATH:-}" ]; then
    export QML_IMPORT_PATH="${QML_PATHS}:${QML_IMPORT_PATH}"
  else
    export QML_IMPORT_PATH="${QML_PATHS}"
  fi
  if [ -n "${QML2_IMPORT_PATH:-}" ]; then
    export QML2_IMPORT_PATH="${QML_PATHS}:${QML2_IMPORT_PATH}"
  else
    export QML2_IMPORT_PATH="${QML_PATHS}"
  fi
fi

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

"${VENV_PYTHON}" -m py_compile \
  "${ROOT_DIR}/src/voiceagent/app.py" \
  "${ROOT_DIR}/src/voiceagent/window.py" \
  "${ROOT_DIR}/src/voiceagent/controller.py"

# Lint MainWindow.qml in full AND each extracted component standalone.
# The standalone invocations catch errors local to a component (missing
# import, malformed binding) without depending on a `voiceAgent`
# definition; the full MainWindow.qml lint catches type errors at the
# top level. The runtime engine.load() below is what actually exercises
# every voiceAgent property/slot binding, against the real MainWindow.
qmllint \
  "${ROOT_DIR}/src/voiceagent/qml/MainWindow.qml" \
  "${ROOT_DIR}/src/voiceagent/qml/MicButton.qml" \
  "${ROOT_DIR}/src/voiceagent/qml/CatalogList.qml" \
  "${ROOT_DIR}/src/voiceagent/qml/ConversationPane.qml"

# Compile-load the real MainWindow against the real
# QQmlApplicationEngine. tests.fakes.build_compiletest_window wires
# fake backends behind a real `MainWindow`, so every QML binding to
# `voiceAgent.*` resolves against the live class — drift between QML
# and the Python surface fails immediately. Previously this used a
# hand-maintained `StubVoiceAgent` heredoc that silently drifted.
"${VENV_PYTHON}" - <<'PY'
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from tests.fakes import build_compiletest_window

app = QApplication([])
window = build_compiletest_window()
QTimer.singleShot(0, app.quit)
app.exec()
window.shutdown()
print("compile ok")
PY
