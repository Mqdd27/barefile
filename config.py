"""Configuration, read from FILEMANAGER_* environment variables."""
import os
from dataclasses import dataclass

BASE = os.path.dirname(os.path.abspath(__file__))
# Pinned so a user's PATH (e.g. Homebrew gnubin) cannot change which binaries run.
DEFAULT_CLI_PATH = "/usr/bin:/bin"


@dataclass
class Config:
    storage_root: str
    data_dir: str
    secret_key: str
    cookie_secure: bool = False
    read_only: bool = False
    cli_path: str = DEFAULT_CLI_PATH
    cmd_timeout: int = 30
    op_timeout: int = 3600
    max_upload: int = 10 * 1024 ** 3

    def __post_init__(self):
        self.storage_root = os.path.realpath(self.storage_root)
        self.data_dir = os.path.realpath(self.data_dir)
        if not self.secret_key:
            raise SystemExit("FILEMANAGER_SECRET_KEY is required (run the setup script in scripts/)")
        if self.storage_root == "/":
            raise SystemExit("FILEMANAGER_STORAGE_ROOT must not be the filesystem root")
        root, data = self.storage_root + "/", self.data_dir + "/"
        if data.startswith(root) or root.startswith(data):
            raise SystemExit("FILEMANAGER_DATA_DIR and FILEMANAGER_STORAGE_ROOT must not overlap")

    @property
    def trash(self):
        return os.path.join(self.data_dir, "trash")

    @property
    def tmp(self):
        return os.path.join(self.data_dir, "tmp")

    @property
    def db(self):
        return os.path.join(self.data_dir, "barefile.db")


def load():
    env = os.environ.get
    return Config(
        storage_root=env("FILEMANAGER_STORAGE_ROOT", os.path.join(BASE, "data", "storage")),
        data_dir=env("FILEMANAGER_DATA_DIR", os.path.join(BASE, "data", "app")),
        secret_key=env("FILEMANAGER_SECRET_KEY", ""),
        cookie_secure=env("FILEMANAGER_COOKIE_SECURE", "0") == "1",
        read_only=env("FILEMANAGER_READ_ONLY", "0") == "1",
        cli_path=env("FILEMANAGER_CLI_PATH", DEFAULT_CLI_PATH),
        cmd_timeout=int(env("FILEMANAGER_CMD_TIMEOUT", "30")),
        op_timeout=int(env("FILEMANAGER_OP_TIMEOUT", "3600")),
        max_upload=int(env("FILEMANAGER_MAX_UPLOAD", str(10 * 1024 ** 3))),
    )
