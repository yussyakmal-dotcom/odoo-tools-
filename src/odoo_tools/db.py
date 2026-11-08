"""SQLite persistence. All user/auth data stays on a persistent Docker volume."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    csrf_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_failures_ip ON login_failures(ip_address, created_at);
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_memberships (
    user_id INTEGER NOT NULL REFERENCES users(id),
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    role TEXT NOT NULL CHECK(role IN ('workspace_admin', 'editor', 'viewer')),
    PRIMARY KEY(user_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    workspace_id INTEGER REFERENCES workspaces(id),
    target TEXT,
    created_at TEXT NOT NULL
);
"""


def open_db(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def initialize(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open_db(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


@contextmanager
def transaction(path: str) -> Iterator[sqlite3.Connection]:
    with open_db(path) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
