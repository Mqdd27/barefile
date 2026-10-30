# barefile

A barebone, self-hosted web file manager. Flask handles login, validation and
HTTP; every filesystem operation is a native Unix command run through
`subprocess`. No frontend framework, no ORM, no build step, no Docker.

## Features

- Browse folders. Each view runs `ls -lh` and shows Name, Size and Last Edit
- Sort by name, size or date, ascending or descending, using `sort`
- Add file, create folder, rename, copy, move, multi-select
- Upload (multiple files, drag and drop, real byte progress) and download (HTTP Range)
- Browser-native preview for images, PDF, video and text
- Filename search with `find`
- Recycle Bin with restore, permanent delete and Empty Trash
- Live command output and item-count progress over Server-Sent Events
- Single account, created at first run. No public signup

## Architecture

```
app.py       Routes, security headers, the /api/op event stream
auth.py      Login, first-account registration, CSRF, rate limiting
files.py     Path validation and file operations
trash.py     Recycle Bin
cli.py       Command allowlist, runner, BSD/GNU adapter
database.py  SQLite helpers
config.py    FILEMANAGER_* settings
```

### Native commands

| Operation | Command |
|---|---|
| Listing | `ls -lhnA` (`-n` keeps a spaced owner name from shifting columns), plus `find -exec stat` for exact names, bytes and timestamps |
| Sorting | `sort`, with raw bytes and epoch timestamps as keys |
| Folder size | `du -sh`, fetched after the page loads so large folders never delay it |
| Add file / folder | `touch` / `mkdir -v` |
| Copy | `cp -RPpv` into a temp path, then `mv` into place |
| Move, rename, delete, restore | `mv -vn` (`-vnT` on GNU) |
| Permanent delete | `rm -v`, `rm -rv` |
| Upload | Request body piped to `dd`, published with `ln` |
| Download, preview | `dd` for bytes, `head -c` for text, `stat` for size, `file --mime-type` for unknown types |
| Search | `find -iname` |

Also required: `ln`, `head`. All are present by default on macOS, Ubuntu and Debian.

### CLI execution

- Only commands and options in `ALLOWED` (`cli.py`) can run. There is no `shell=True`.
- Binaries come from `FILEMANAGER_CLI_PATH` (default `/usr/bin:/bin`), not the
  user's `PATH`.
- Paths are always absolute and follow `--` where the command supports it. `find`
  has no `--`, and `dd` takes `if=`/`of=`; both get absolute paths, so a name
  cannot be read as an option.
- Each command is logged with its arguments, exit status and duration.
  Short commands time out after 30 s, long ones after 1 hour.
- BSD or GNU is detected by probing `stat --version`, not from `uname`, because
  macOS machines can have GNU tools installed.

Hard parts handled deliberately:

- **Odd filenames.** `ls` output cannot be parsed reliably, so identity comes from
  `find -exec stat`, framed by a random per-request marker. `ls` supplies the
  displayed size and date and the total. If a name cannot be matched in `ls` output
  (for example it contains a newline), values formatted from `stat` are shown.
- **Sort output.** Rows sent to `sort` end with an index and only the index is read
  back, so a tab or newline in a name cannot corrupt the result.
- **`mv -n` exits 0 when it skips.** Each move checks the destination under a file
  lock, then confirms the source is gone.
- **Uploads** are written to a temp file outside storage and published with `ln`.
  `link(2)` is atomic and never overwrites, so a partial file cannot appear.

### Where Python touches the filesystem

Listing, writing, moving and deleting managed files is always a native command.
The exceptions are:

- `os.path.realpath`, `lexists`, `isdir` and similar, for validation only
- `os.stat` once at startup, to compare filesystem ids
- The SQLite database and a lock file, both in the data directory
- `sqlite3`, `fcntl` and the HTTP layer itself: file bytes pass through Python on
  their way to and from `dd`, in 64 KiB chunks, so nothing is held in memory

## macOS installation

```sh
./scripts/setup-macos.sh
```

This finds Python 3.9+, creates `.venv`, installs dependencies, checks the native
commands, creates `.env` with a random secret (an existing `.env` is kept), and
creates the storage folders and database. Re-running it is safe.

## Linux installation

For a local install on Ubuntu or Debian:

```sh
./scripts/setup-linux.sh
```

It tells you which packages to `apt install` if any are missing, then runs the
same steps as the macOS script. It does not need root. For a server, see
[Linux deployment](#linux-deployment-no-docker).

## Running

```sh
./scripts/dev.sh
```

Open <http://127.0.0.1:5050>. The dev server listens on loopback only.

## Login and first registration

With no account yet, the app sends you to `/register`. After that account exists,
`/register` is closed permanently. To start again, stop the app and delete
`data/app/barefile.db`. Your files and the bin contents stay on disk, but
restore records are lost.

## Tests

```sh
./scripts/test.sh
```

Runs the `unittest` suite against real commands in a temporary directory, using
the system tools and then GNU coreutils if Homebrew has them.

## Configuration

Set these in `.env`. See `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `FILEMANAGER_SECRET_KEY` | required | Signs session cookies |
| `FILEMANAGER_STORAGE_ROOT` | `./data/storage` | The folder served. `/` is refused |
| `FILEMANAGER_DATA_DIR` | `./data/app` | Database, trash, upload temp files |
| `FILEMANAGER_COOKIE_SECURE` | `0` | Set `1` behind HTTPS |
| `FILEMANAGER_READ_ONLY` | `1` | Refuses every change on the server and hides those controls. Set `0` to allow changes |
| `FILEMANAGER_CLI_PATH` | `/usr/bin:/bin` | Where native commands are found |
| `FILEMANAGER_CMD_TIMEOUT` | `30` | Seconds, short commands |
| `FILEMANAGER_OP_TIMEOUT` | `3600` | Seconds, long commands |
| `FILEMANAGER_MAX_UPLOAD` | `10737418240` | Bytes per file, `0` for no limit |

## Storage location

Files live in `FILEMANAGER_STORAGE_ROOT`. The Recycle Bin (`trash/`), upload temp
files (`tmp/`) and `barefile.db` live in `FILEMANAGER_DATA_DIR`, which must not
overlap the storage root and must be on the same filesystem, because uploads and
trash rely on atomic link and rename. The app refuses to start otherwise.

## Linux deployment (no Docker)

For Ubuntu or Debian. The app runs as your normal login user, so no extra account
or ownership change is needed.

```sh
sudo apt install python3-venv file
sudo mkdir /opt/barefile
sudo chown "$USER": /opt/barefile
git clone git@github.com:Mqdd27/barefile.git /opt/barefile/app
cd /opt/barefile/app
./scripts/setup-linux.sh
```

Then set `FILEMANAGER_COOKIE_SECURE=1` in `.env`. Files are served from `data/storage`
unless you change `FILEMANAGER_STORAGE_ROOT`.

`/etc/systemd/system/barefile.service`:

```ini
[Unit]
Description=barefile
After=network.target

[Service]
User=your-login-user
WorkingDirectory=/opt/barefile/app
EnvironmentFile=/opt/barefile/app/.env
ExecStart=/opt/barefile/app/.venv/bin/gunicorn 'app:create_app()' --bind 127.0.0.1:5050 --workers 1 --threads 8
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl enable --now barefile
```

Put a TLS-terminating reverse proxy in front and register immediately, since the
first visitor creates the account. With nginx, set `proxy_request_buffering off`
and `proxy_buffering off` so upload and operation progress stay live, and raise
`client_max_body_size`.

Use threads, as shown: each streaming operation holds one for its duration.

## Known limitations

- One account. No per-user storage.
- Copy, move and restore progress counts selected items, not bytes. A single large
  folder shows 0 of 1 with live `cp -v` output until it finishes.
- Closing the tab during a copy cancels it and removes the partial copy.
- Renaming only the case of a name (`a.txt` to `A.txt`) on a case-insensitive
  volume, such as default APFS, is rejected as a collision. Rename through a
  temporary name.
- Names that are not valid UTF-8 are hidden from listings and counted in a notice.
  Such names cannot occur on APFS.
- With `LC_ALL=C`, name sorting folds ASCII case only. Non-ASCII names sort by byte.
- BSD `ls -lh` reports its total in 512-byte blocks rather than a human size, and
  that value is shown as is. `du` gives the human-readable figure.
- Search returns at most 500 results.
- Temp files left by a hard crash stay in `data/app/tmp` until removed by hand.
  They never appear in storage.

## Security

- Every route requires login unless explicitly public, and `POST` requires a CSRF token.
- Passwords are hashed by Werkzeug. Login is limited to 5 failures per IP and
  username in 5 minutes. Behind a proxy, all clients share the proxy's IP, so the
  limit becomes per username.
- Cookies are `HttpOnly` and `SameSite=Lax`, plus `Secure` when configured.
- Paths are resolved with `realpath` and must stay inside the root. Operations on
  an item resolve its parent and act on a final symlink itself, so deleting a link
  never touches its target.
- `find`, `du` and `cp -P` do not follow symlinks.
- Check-then-act steps run under a lock, so app requests cannot race each other.
  A separate process with write access to storage could still swap a path for a
  symlink between validation and use; `realpath` cannot close that window.
- Stored files are outside `static/`. Only images, PDF and video display inline.
  HTML and SVG always download, with `nosniff`, a sandboxing CSP and a strict
  site-wide CSP.
- Internal paths are removed from messages shown to the browser.
- Logging out clears the browser's session, but Flask sessions are signed cookies,
  so a stolen copy stays valid until `FILEMANAGER_SECRET_KEY` changes.
