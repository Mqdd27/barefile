#!/usr/bin/env bash
# Idempotent: safe to re-run. Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "1. Finding Python 3.9+"
PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
[ -n "$PYTHON" ] || { echo "Python 3.9+ not found. Install it: brew install python" >&2; exit 1; }
echo "   $("$PYTHON" --version) at $(command -v "$PYTHON")"

echo "2. Virtual environment"
[ -d .venv ] || "$PYTHON" -m venv .venv

echo "3. Dependencies"
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

echo "4. Native commands"
for name in ls touch mkdir cp mv ln rm find sort stat du file head dd; do
  PATH=/usr/bin:/bin command -v "$name" >/dev/null || { echo "Missing: $name" >&2; exit 1; }
done
echo "   all found in /usr/bin:/bin"

echo "5. Configuration"
if [ -f .env ]; then
  echo "   keeping existing .env"
else
  secret="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
  sed "s/^FILEMANAGER_SECRET_KEY=$/FILEMANAGER_SECRET_KEY=$secret/" .env.example > .env
  chmod 600 .env
  echo "   created .env"
fi

echo "6. Storage folders and SQLite"
set -a; . ./.env; set +a
.venv/bin/python -c 'import app; app.create_app()'

echo
echo "Done. Start the app:  ./scripts/dev.sh"
echo "Then open:            http://127.0.0.1:5050"
echo "Read-only by default. To allow changes, set FILEMANAGER_READ_ONLY=0 in .env"
