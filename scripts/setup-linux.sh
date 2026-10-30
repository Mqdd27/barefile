#!/usr/bin/env bash
# For Ubuntu and Debian. Checks the packages macOS has built in, then shares the macOS steps.
set -euo pipefail

missing=""
# Debian splits venv out of python3; ensurepip is what goes missing.
python3 -c 'import ensurepip, sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null || missing="python3 python3-venv"
PATH=/usr/bin:/bin command -v file >/dev/null || missing="$missing file"
if [ -n "$missing" ]; then
  echo "Install these first: sudo apt install $missing" >&2
  exit 1
fi

exec "$(dirname "$0")/setup-macos.sh"
