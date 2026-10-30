#!/usr/bin/env bash
# Runs the suite against the system tools, then GNU coreutils when Homebrew has them.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== System tools (/usr/bin:/bin)"
FILEMANAGER_CLI_PATH=/usr/bin:/bin .venv/bin/python -m unittest discover -s tests "$@"

gnubin=/opt/homebrew/opt/coreutils/libexec/gnubin
if [ "$(uname)" = Darwin ] && [ -d "$gnubin" ]; then
  echo "== GNU coreutils ($gnubin)"
  FILEMANAGER_CLI_PATH="$gnubin:/usr/bin:/bin" .venv/bin/python -m unittest discover -s tests "$@"
fi
