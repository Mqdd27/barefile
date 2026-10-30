#!/usr/bin/env bash
# Development server. Bound to loopback on purpose.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Run ./scripts/setup-macos.sh first" >&2; exit 1; }
set -a; . ./.env; set +a
exec .venv/bin/flask --app 'app:create_app()' run --host 127.0.0.1 --port 5050
