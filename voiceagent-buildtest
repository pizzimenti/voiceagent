#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

if [ ! -x "${VENV_PYTHON}" ]; then
  echo "Missing virtualenv python at ${VENV_PYTHON}" >&2
  exit 1
fi

if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH}"
else
  export PYTHONPATH="${ROOT_DIR}/src"
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

exec "${VENV_PYTHON}" -m voiceagent "$@"
