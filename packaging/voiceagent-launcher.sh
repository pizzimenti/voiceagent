#!/bin/sh
set -eu

VENDOR_DIR="/usr/lib/voiceagent/vendor"
SYSTEM_SITE_PACKAGES="/usr/lib/python3.14/site-packages"

if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="${VENDOR_DIR}:${SYSTEM_SITE_PACKAGES}:${PYTHONPATH}"
else
  export PYTHONPATH="${VENDOR_DIR}:${SYSTEM_SITE_PACKAGES}"
fi

exec /usr/bin/python -m voiceagent "$@"
