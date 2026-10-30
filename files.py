"""Path validation and filesystem operations, all delegated to native commands.

os.path is used only to validate (realpath, lexists, isdir, ...). Nothing here
lists, writes, moves or deletes managed files from Python.
"""
import fcntl
import mimetypes
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager

from cli import CommandError, text

CHUNK = 64 * 1024
PREVIEW_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 500
LS_LINE = re.compile(r"^\S+\s+\d+\s+\d+\s+\d+\s+(\S+)\s+(\S+\s+\d+\s+[\d:]+)\s(.*)$", re.S)
SORT_KEYS = {"name": ["-k3,3f"], "size": ["-k1,1n", "-k3,3f"], "date": ["-k2,2n", "-k3,3f"]}


class FileError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def validate_name(name):
    """A single new path component, supplied by the client."""
    try:
        encoded = name.encode("utf-8")
    except (UnicodeEncodeError, AttributeError):
        raise FileError("Invalid name") from None
    if name in ("", ".", "..") or "/" in name or len(encoded) > 255 or name != name.strip():
        raise FileError("Invalid name")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise FileError("Invalid name")
    return name


def human(size):
    """Only for entries ls -lh output could not be matched to (see Storage.list)."""
    value = float(size)
    for unit in "BKMGT":
        if value < 1024:
            return f"{value:.0f}{unit}" if value >= 10 or unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}P"


def parse_ls(output):
    """Return (total, {name: (size, date)}) from ls -lhn output.

    -n prints numeric ids, so an owner containing spaces cannot shift the columns.
    Lines are only trusted for display: identity comes from find/stat, and any
    name this misses (embedded newline, device file) falls back to stat values.
    """
    lines = output.split("\n")
    total = lines[0].split(" ", 1)[1] if lines[0].startswith("total ") else ""
    if total.isdigit():  # BSD ls prints blocks here even with -h
        total += " blocks"
    rows = {}
    for line in lines[1:]:
        match = LS_LINE.match(line)
        if match:
            size, date, name = match.groups()
            rows[name] = rows[name.split(" -> ")[0]] = (size, " ".join(date.split()))
    return total, rows


class Storage:
    def __init__(self, cfg, cli):
        self.cfg, self.cli, self.root = cfg, cli, cfg.storage_root

    def init(self):
        self.cli.run("mkdir", "-p", "--", self.root)
        self.cli.run("mkdir", "-p", "-m", "700", "--", self.cfg.data_dir)
        self.cli.run("mkdir", "-p", "-m", "700", "--", self.cfg.trash, self.cfg.tmp)
        if os.stat(self.root).st_dev != os.stat(self.cfg.tmp).st_dev:
            raise SystemExit("FILEMANAGER_DATA_DIR must be on the same filesystem as "
                             "FILEMANAGER_STORAGE_ROOT: uploads and trash rely on atomic rename/link")

    @contextmanager
    def lock(self):
        """Serialise check-then-publish across threads and workers."""
        with open(os.path.join(self.cfg.data_dir, "lock"), "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    # Path validation -------------------------------------------------------

    def rel(self, path):
        return os.path.relpath(path, self.root)

    def resolve(self, rel):
        """Absolute real path of rel, following symlinks; must stay inside the root."""
        try:
            (rel or "").encode("utf-8")
        except UnicodeEncodeError:
            raise FileError("Invalid path") from None
        parts = [part for part in (rel or "").split("/") if part not in ("", ".")]
        if "\0" in (rel or "") or ".." in parts:
            raise FileError("Path not allowed", 403)
        real = os.path.realpath(os.path.join(self.root, *parts))
        if real != self.root and not real.startswith(self.root + "/"):
            raise FileError("Path not allowed", 403)
        return real

    def entry(self, rel):
        """Path of an item acted on itself: parent resolved, a final symlink is not followed."""
        parts = [part for part in (rel or "").split("/") if part not in ("", ".")]
        if ".." in parts:
            raise FileError("Path not allowed", 403)
        if not parts:
            raise FileError("The storage root cannot be changed")
        path = os.path.join(self.resolve("/".join(parts[:-1])), parts[-1])
        if not os.path.lexists(path):
            raise FileError("Not found", 404)
        return path

    def folder(self, rel):
        path = self.resolve(rel)
        if not os.path.isdir(path):
            raise FileError("Folder not found", 404)
        return path

    def file(self, rel):
        path = self.resolve(rel)
        if not os.path.isfile(path):  # also keeps dd away from FIFOs and devices
            raise FileError("File not found", 404)
        return path

    def _new(self, rel_dir, name):
        path = os.path.join(self.folder(rel_dir), validate_name(name))
        if os.path.lexists(path):
            raise FileError(f"Already exists: {name}", 409)
        return path

    def _target(self, rel, dest):
        src, folder = self.entry(rel), self.folder(dest)
        dst = os.path.join(folder, os.path.basename(src))
        if folder == src or folder.startswith(src + "/"):
            raise FileError("Cannot place a folder inside itself")
        if os.path.lexists(dst):
            raise FileError(f"Already exists: /{self.rel(dst)}", 409)
        return src, dst

    # Listing ---------------------------------------------------------------

    def _stat_records(self, base, *expr):
        """find + stat, as [{name, path, size, mtime, is_dir}] and a hidden-name count."""
        mark = uuid.uuid4().hex
        proc = self.cli.run("find", base, *expr, "-exec", self.cli.bins["stat"],
                            *self.cli.stat_args(mark), "{}", "+", check=False,
                            timeout=self.cli.timeout)
        if proc.returncode and not proc.stdout:
            raise CommandError("Could not read folder")
        records, skipped = [], 0
        for record in text(proc.stdout).split(mark)[1:]:
            size, mtime, kind, path = record[:-1].split("|", 3)
            try:
                path.encode("utf-8")
            except UnicodeEncodeError:
                skipped += 1  # ponytail: non-UTF-8 names are hidden, not browser-safe; show them if ever needed
                continue
            records.append({"name": os.path.basename(path), "path": self.rel(path), "size": int(size),
                            "mtime": int(mtime), "is_dir": kind.lower() == "directory"})
        return records, skipped

    def _sort(self, records, key, descending):
        """Sort with sort(1). Rows carry an index and only the index is read back,
        so tabs or newlines in a name cannot corrupt the result."""
        rows = []
        for index, entry in enumerate(records):
            name = re.sub(r"[\t\n]", " ", entry["name"])
            rows.append(f"{entry['size']}\t{entry['mtime']}\t{name}\t{index}\n")
        # Direction goes on each key: a bare -r is ignored for keys with modifiers.
        args = ["-t", "\t", *(k + "r" if descending else k for k in SORT_KEYS[key])]
        output = self.cli.run("sort", *args, input="".join(rows).encode("utf-8")).stdout
        return [records[int(line.rsplit(b"\t", 1)[1])] for line in output.split(b"\n")[:-1]]

    def list(self, rel, key="name", order="asc"):
        path = self.folder(rel)
        total, rows = parse_ls(text(self.cli.run("ls", "-lhnA", "--", path).stdout))
        records, skipped = self._stat_records(path, "-mindepth", "1", "-maxdepth", "1")
        for entry in records:
            fallback = human(entry["size"]), time.strftime("%b %d %H:%M", time.localtime(entry["mtime"]))
            entry["size_h"], entry["date"] = rows.get(entry["name"], fallback)
        return {"entries": self._sort(records, key, order == "desc"), "total": total, "skipped": skipped}

    def du(self, rel):
        output = self.cli.run("du", "-sh", "--", self.folder(rel)).stdout
        return text(output).split("\t")[0].strip()

    def search(self, query):
        """Case-insensitive filename match. find does not follow symlinks by default."""
        pattern = re.sub(r"([*?\[\]\\])", r"\\\1", query)
        records, _ = self._stat_records(self.root, "-mindepth", "1", "-iname", f"*{pattern}*")
        for entry in records:
            entry["parent"] = os.path.dirname(entry["path"])
        return self._sort(records, "name", False)[:MAX_SEARCH_RESULTS]  # ponytail: hard cap, add paging if 500 hits is ever too few

    # Metadata and content --------------------------------------------------

    def size(self, path):
        mark = uuid.uuid4().hex
        output = self.cli.run("stat", *self.cli.stat_args(mark), "--", path).stdout
        return int(text(output).split(mark)[1].split("|", 1)[0])

    def mime(self, path):
        guess = mimetypes.guess_type(os.path.basename(path))[0]
        if guess:
            return guess
        return text(self.cli.run("file", "--mime-type", "-b", "--", path).stdout).strip()

    def head(self, path):
        output = self.cli.run("head", "-c", str(PREVIEW_BYTES), "--", path).stdout
        return output.decode("utf-8", "replace")

    def read(self, path, start, length):
        """Yield length bytes from start through dd. dd seeks to the block holding
        start, then the sub-block remainder is dropped here (bs=1 would be far slower)."""
        skip, drop = divmod(start, CHUNK)
        proc = self.cli.popen("dd", f"if={path}", f"bs={CHUNK}", f"skip={skip}",
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while length > 0:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                chunk = chunk[drop:length + drop]
                drop = 0
                length -= len(chunk)
                if chunk:
                    yield chunk
        finally:
            self.cli.finish(proc)

    def upload(self, rel_dir, name, stream, length):
        """Stream into dd in a private temp file, then publish with ln: link(2) is
        atomic and never overwrites, so a partial file can never appear."""
        dst = self._new(rel_dir, name)
        tmp = os.path.join(self.cfg.tmp, uuid.uuid4().hex)
        proc = self.cli.popen("dd", f"of={tmp}", f"bs={CHUNK}", stdin=subprocess.PIPE, bufsize=0,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        received = 0
        try:
            try:
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    received += len(chunk)
            except BrokenPipeError:
                raise FileError("Could not write file (disk full?)", 507) from None
            proc.stdin.close()
            if proc.wait():
                raise FileError(self.cli.clean(proc.stderr.read()).split("\n")[-1] or "Could not write file", 507)
            if received != length:
                raise FileError("Upload incomplete")
            with self.lock():
                if os.path.lexists(dst):
                    raise FileError(f"Already exists: {name}", 409)
                self.cli.run("ln", self.cli.ln_flags, "--", tmp, dst)
        finally:
            self.cli.finish(proc)
            self.cli.run("rm", "-rf", "--", tmp, check=False)

    # Operations: generators yielding one line of output at a time ----------

    def publish(self, src, dst):
        """No-clobber rename. mv -n exits 0 when it skips, so success is verified."""
        with self.lock():
            if os.path.lexists(dst):
                raise FileError(f"Already exists: /{self.rel(dst)}", 409)
            proc = self.cli.run("mv", self.cli.mv_flags, "--", src, dst, timeout=self.cli.op_timeout)
            if os.path.lexists(src):
                raise FileError(f"Already exists: /{self.rel(dst)}", 409)
        return self.cli.clean(proc.stdout)

    def add_file(self, rel_dir, name):
        path = self._new(rel_dir, name)
        with self.lock():  # touch alone would quietly accept an existing file
            if os.path.lexists(path):
                raise FileError(f"Already exists: {name}", 409)
            self.cli.run("touch", "--", path)
        yield f"created /{self.rel(path)}"

    def add_folder(self, rel_dir, name):
        path = self._new(rel_dir, name)
        yield self.cli.clean(self.cli.run("mkdir", "-v", "--", path).stdout)

    def rename(self, rel, name):
        src = self.entry(rel)
        yield self.publish(src, os.path.join(os.path.dirname(src), validate_name(name)))

    def move(self, rel, dest):
        src, dst = self._target(rel, dest)
        yield self.publish(src, dst)

    def copy(self, rel, dest):
        """Copy into temp, then publish, so a failed or cancelled cp leaves nothing
        behind and the lock is not held for the length of the copy."""
        src, dst = self._target(rel, dest)
        tmp = os.path.join(self.cfg.tmp, uuid.uuid4().hex)
        try:
            yield from self.cli.stream("cp", "-RPpv", "--", src, tmp, replace=[(tmp, "/" + self.rel(dst))])
            self.publish(tmp, dst)
        finally:
            self.cli.run("rm", "-rf", "--", tmp, check=False, timeout=self.cli.op_timeout)
