"""SQLite helpers. One short-lived connection per statement."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trash (
    id INTEGER PRIMARY KEY,
    original_path TEXT NOT NULL,
    trash_path TEXT NOT NULL UNIQUE,
    deleted_at TEXT NOT NULL,
    original_name TEXT NOT NULL
);
"""


def init(path):
    con = sqlite3.connect(path)
    with con:
        con.executescript(SCHEMA)
    con.close()


def execute(path, sql, args=()):
    """Run one statement, returning (rows, rowcount)."""
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        with con:
            cur = con.execute(sql, args)
            return cur.fetchall(), cur.rowcount
    finally:
        con.close()


def query(path, sql, args=()):
    return execute(path, sql, args)[0]
