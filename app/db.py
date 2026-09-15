"""Conexión SQLite y esquema del índice (resources + sync_runs).

Comparte la misma base de datos que el resto de la API (favoritos, ratings, ...)
a través de la variable de entorno RECURSOS_DB.
"""
import sqlite3
from pathlib import Path

from . import config


def _ensure_dir() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    title_ca TEXT DEFAULT '',
    title_en TEXT DEFAULT '',
    description TEXT DEFAULT '',
    description_ca TEXT DEFAULT '',
    description_en TEXT DEFAULT '',
    author TEXT DEFAULT '',
    license TEXT DEFAULT '',
    license_known INTEGER NOT NULL DEFAULT 0,
    language TEXT DEFAULT '[]',
    resource_type TEXT DEFAULT '',
    format TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    educational_stage TEXT DEFAULT '',
    educational_level TEXT DEFAULT '[]',
    tags TEXT DEFAULT '[]',
    source_url TEXT DEFAULT '',
    play_url TEXT DEFAULT '',
    download_url TEXT DEFAULT '',
    reuse_url TEXT DEFAULT '',
    thumbnail_url TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at_source TEXT DEFAULT '',
    updated_at_source TEXT DEFAULT '',
    indexed_at TEXT DEFAULT '',
    last_synced_at TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_resources_provider ON resources(provider);
CREATE INDEX IF NOT EXISTS idx_resources_format ON resources(format);
CREATE INDEX IF NOT EXISTS idx_resources_subject ON resources(subject);
CREATE INDEX IF NOT EXISTS idx_resources_stage ON resources(educational_stage);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    started_at TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    fetched INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_log TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_provider ON sync_runs(provider);
"""


def init_index_schema() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Migracions idempotents per a bases existents (noves columnes)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(resources)")}
    for col in ("title_en", "description_en"):
        if col not in existing:
            conn.execute(f"ALTER TABLE resources ADD COLUMN {col} TEXT DEFAULT ''")
