"""Recycle Bin: mv into a uuid-named slot outside the storage root, tracked in SQLite."""
import os
import time
import uuid

from database import execute, query
from files import FileError


class Trash:
    def __init__(self, cfg, cli, store):
        self.cfg, self.cli, self.store = cfg, cli, store

    def items(self):
        return query(self.cfg.db, "SELECT * FROM trash ORDER BY id DESC")

    def _row(self, item_id):
        rows = query(self.cfg.db, "SELECT * FROM trash WHERE id = ?", (item_id,))
        if not rows:
            raise FileError("Not in Recycle Bin", 404)
        return rows[0]

    def delete(self, rel):
        src = self.store.entry(rel)
        slot = uuid.uuid4().hex
        # Row first: a crash leaves a stale row, never an untracked file in trash.
        execute(self.cfg.db,
                "INSERT INTO trash (original_path, trash_path, deleted_at, original_name) VALUES (?, ?, ?, ?)",
                (self.store.rel(src), slot, time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(src)))
        try:
            self.store.publish(src, os.path.join(self.cfg.trash, slot))
        except Exception:
            execute(self.cfg.db, "DELETE FROM trash WHERE trash_path = ?", (slot,))
            raise
        yield f"/{self.store.rel(src)} -> Recycle Bin"

    def restore(self, item_id):
        row = self._row(item_id)
        parent = self.store.resolve(os.path.dirname(row["original_path"]))
        dst = os.path.join(parent, row["original_name"])
        if not os.path.isdir(parent):  # the original folder was deleted too
            self.cli.run("mkdir", "-pv", "--", parent)
        self.store.publish(os.path.join(self.cfg.trash, row["trash_path"]), dst)
        execute(self.cfg.db, "DELETE FROM trash WHERE id = ?", (item_id,))
        yield f"Recycle Bin -> /{row['original_path']}"

    def purge(self, item_id):
        row = self._row(item_id)
        path = os.path.join(self.cfg.trash, row["trash_path"])
        name = row["original_name"]
        if os.path.lexists(path):
            is_dir = os.path.isdir(path) and not os.path.islink(path)
            yield from self.cli.stream("rm", "-rv" if is_dir else "-v", "--", path,
                                       replace=[(path, name)])
        execute(self.cfg.db, "DELETE FROM trash WHERE id = ?", (item_id,))
