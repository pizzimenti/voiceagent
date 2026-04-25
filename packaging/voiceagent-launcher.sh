#!/bin/sh
set -eu

VENDOR_DIR="/usr/lib/voiceagent/vendor"
SYSTEM_SITE_PACKAGES="$(/usr/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"

if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="${VENDOR_DIR}:${SYSTEM_SITE_PACKAGES}:${PYTHONPATH}"
else
  export PYTHONPATH="${VENDOR_DIR}:${SYSTEM_SITE_PACKAGES}"
fi

exec /usr/bin/python -m voiceagent "$@"
