#!/usr/bin/env bash
# Point the app at another folder: ./scripts/set-root.sh ~/Movies
# Updates FILEMANAGER_STORAGE_ROOT in .env. Editing .env by hand works the same.
set -euo pipefail
[ $# -eq 1 ] || { echo "Usage: $0 <folder>" >&2; exit 2; }
root="$(cd -- "$1" && pwd -P)"
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Run the setup script first" >&2; exit 1; }
set -a; . ./.env; set +a
export FILEMANAGER_STORAGE_ROOT="$root"

# Runs the app's own startup checks, so a folder it would refuse leaves .env untouched.
.venv/bin/python - <<'PY'
import os
import re
import shlex

import config
from cli import CLI
from database import query
from files import Storage

root = os.environ["FILEMANAGER_STORAGE_ROOT"]
if "'" in root or "\n" in root:
    raise SystemExit("Folder paths with a quote or newline cannot be stored in .env")
cfg = config.load()
Storage(cfg, CLI(cfg)).init()

if os.path.exists(cfg.db):
    count = query(cfg.db, "SELECT COUNT(*) AS n FROM trash")[0]["n"]
    if count:
        print(f"Warning: {count} Recycle Bin item(s) will restore relative to the new folder")

line = f"FILEMANAGER_STORAGE_ROOT={shlex.quote(root)}"
active, commented = (rf"(?m)^{p}FILEMANAGER_STORAGE_ROOT=.*$" for p in ("", r"#\s*"))
with open(".env") as f:
    text = f.read()
if re.search(active, text):
    text = re.sub(active, lambda m: line, text)
elif re.search(commented, text):
    text = re.sub(commented, lambda m: line, text, count=1)
else:
    text = text.rstrip("\n") + f"\n{line}\n"
with open(".env", "w") as f:
    f.write(text)
PY

echo "Storage root: $root"
if [ "$(systemctl show -p WorkingDirectory --value barefile 2>/dev/null)" = "$PWD" ]; then
  sudo systemctl restart barefile
  echo "Service restarted"
else
  echo "Restart the app to use it: stop ./scripts/dev.sh and start it again"
fi
