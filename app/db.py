"""SQLite schema and helpers.

One database for the whole build. Tables for later steps (tickets, documents,
uploads, jobs) are created now so no migration is needed when those modules
land; they stay empty until then.

Set APP_DB to override the path (tests use a temporary file).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def db_path() -> Path:
    override = os.getenv("APP_DB")
    return Path(override) if override else ROOT / "data" / "app.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT,
    language      TEXT,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | expired | closed
    summary       TEXT,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    expires_at    TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,                      -- customer | assistant | system
    content     TEXT NOT NULL,
    masked      TEXT,
    language    TEXT,
    triage      TEXT,                               -- JSON blob
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS escalations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    condition   TEXT NOT NULL,
    summary     TEXT,
    queue       TEXT,
    risk_level  TEXT,
    sentiment   TEXT,
    scheduled_for TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_escalations_session ON escalations(session_id);

CREATE TABLE IF NOT EXISTS tickets (
    id             TEXT PRIMARY KEY,
    session_id     TEXT,
    subject        TEXT,
    issue          TEXT,
    severity       TEXT,
    priority       REAL,
    status         TEXT NOT NULL DEFAULT 'open',
    team           TEXT,
    assignee       TEXT,
    sla_due_at     TEXT,
    sla_warned_at  TEXT,
    sla_breached_at TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    detail         TEXT                             -- JSON blob
);

CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    product       TEXT,
    region        TEXT,
    access_level  TEXT,
    effective_at  TEXT,
    expires_at    TEXT,
    content_hash  TEXT,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | quarantined | retired
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL REFERENCES documents(id),
    version      INTEGER NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | active | rejected | rolled_back
    quality      TEXT,                              -- JSON blob
    created_at   TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE(document_id, version)
);

CREATE TABLE IF NOT EXISTS uploads (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    filename    TEXT,
    sha256      TEXT,
    status      TEXT NOT NULL DEFAULT 'received',   -- received | queued | processed | rejected | deleted
    extracted   TEXT,                               -- JSON blob
    reason      TEXT,
    created_at  TEXT NOT NULL,
    delete_at   TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',    -- pending | running | done | failed
    payload     TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    run_after   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT,
    action     TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action, id);
"""


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive changes only: existing data must survive a schema tweak."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(escalations)")}
    if "sentiment" not in columns:
        conn.execute("ALTER TABLE escalations ADD COLUMN sentiment TEXT")
    if "scheduled_for" not in columns:
        conn.execute("ALTER TABLE escalations ADD COLUMN scheduled_for TEXT")

    ticket_columns = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
    if "sla_breached_at" not in ticket_columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN sla_breached_at TEXT")


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | list = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> int:
    """Run a write and return lastrowid (or rowcount for updates)."""
    with connect() as conn:
        cursor = conn.execute(sql, params)
        return cursor.lastrowid or cursor.rowcount


def healthy() -> bool:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False
