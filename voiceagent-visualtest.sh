#!/bin/sh
set -eu

# Visual smoke runner — captures MainWindow renders at multiple widths
# under offscreen rendering. PNG outputs land in screenshots/ for
# visual review (gitignored). Companion to:
#
#   voiceagent-compiletest.sh = "does the QML load?" (qmllint + engine load)
#   voiceagent-qatest.sh      = "does the QML behave?" (Quick Tests + pytest-qt)
#   voiceagent-visualtest.sh  = "does the QML LOOK right?" (this — multi-width capture)
#
# Visual tests don't replace human judgment on animation feel or
# subjective spacing rhythm; they catch the gross structural breaks
# (overlap, clipping, misaligned columns) that compile + behavioral
# tests can't see.

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

if [ ! -x "${VENV_PYTHON}" ]; then
  echo "Missing virtualenv python at ${VENV_PYTHON}" >&2
  exit 1
fi

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

# Iterate scale factors. Plasma users run anywhere from 100% to 200%
# routinely; the layout has to behave at every step. Each scale launch
# is a fresh process because Qt resolves QT_SCALE_FACTOR at startup.
# `Kirigami.Units.gridUnit` scales with the font/DPI, so logical widths
# that look fine at 1.0x can break at 1.5x (gridUnit grows; same window
# width crosses different breakpoints).
SCALES="${VOICEAGENT_VISUAL_SCALES:-1.0 1.25 1.5}"

set -- ${SCALES}
for scale in "$@"; do
  echo "--- scale ${scale} ---"
  VOICEAGENT_VISUAL_SCALE="${scale}" \
  QT_SCALE_FACTOR="${scale}" \
  QT_SCALE_FACTOR_ROUNDING_POLICY=PassThrough \
    "${VENV_PYTHON}" "${ROOT_DIR}/tests/visual/visual_smoke.py" || exit $?
done
