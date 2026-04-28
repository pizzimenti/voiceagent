#!/bin/sh
set -eu

# voiceagent-qatest.sh — single command that runs the full headless QA
# suite. Two stages:
#
#   1. `pytest tests/` — Python-side unit + integration tests
#      (including pytest-qt interaction tests against the real
#      MainWindow). This catches Python-side logic regressions and
#      Python↔QML signal-wiring drift.
#   2. `tests/qml/qmltest_main.py` — Qt Quick Test runner driven by
#      `QtQuickTest.QUICK_TEST_MAIN_WITH_SETUP`. The driver installs
#      the same `i18nCtx` context property the production window
#      installs, so production QML files load against a stock test
#      engine without ReferenceErrors. The Quick Tests under
#      `tests/qml/tst_*.qml` cover QML-side behavior — form-layout
#      shape, page-header actions, scroll-mode branching.
#
# Companion to `voiceagent-compiletest.sh`:
#   - compiletest = "does the QML load?" gate (qmllint + real engine
#     load via fakes).
#   - qatest      = "does the QML behave correctly?" gate.

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

if [ ! -x "${VENV_PYTHON}" ]; then
  echo "Missing virtualenv python at ${VENV_PYTHON}" >&2
  exit 1
fi

# Include the worktree root on PYTHONPATH so `tests.fakes` /
# `tests.qml.qmltest_main` are importable alongside the `voiceagent`
# package under `src/`.
if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}:${PYTHONPATH}"
else
  export PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}"
fi

# Mirror voiceagent-compiletest.sh: prepend the system Qt6 QML import
# paths (Kirigami lives at /usr/lib/qt6/qml/org/kde/kirigami) so the
# tests can resolve `org.kde.kirigami` against the host install.
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

# Stage 1: pytest (Python-side + pytest-qt interaction tests).
echo "==> pytest tests/"
"${VENV_PYTHON}" -m pytest tests/

# Stage 2: Qt Quick Tests via the QUICK_TEST_MAIN_WITH_SETUP driver.
# The driver registers `i18nCtx` on the engine before any tst_*.qml
# loads (see tests/qml/qmltest_main.py for the rationale). Pass any
# extra args through (e.g., `-v2` for verbose, single-test filtering).
echo "==> qmltestrunner via tests/qml/qmltest_main.py"
"${VENV_PYTHON}" -m tests.qml.qmltest_main "$@"
