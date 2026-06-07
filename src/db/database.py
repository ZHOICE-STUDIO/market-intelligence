"""Database connection and initialization helpers.

SQLite is the starting store: zero setup, single file, easy to back up and
inspect. If the dataset outgrows it later, the schema ports cleanly to Postgres.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Repo layout: src/db/database.py -> repo root is two levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "market.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a connection with foreign keys on and row access by column name."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# Columns added after the initial schema. Because CREATE TABLE IF NOT EXISTS
# won't alter an existing table, we add any missing ones here (idempotent).
MIGRATIONS = {
    "games": [("summary", "TEXT")],
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, coltype in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
    conn.commit()


def init_db(db_path: Path | str = DB_PATH) -> None:
    """Create the database file, apply the schema, and migrate (idempotent)."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(db_path) as conn:
        conn.executescript(schema)
        _apply_migrations(conn)
    print(f"Initialized database at {db_path}")


if __name__ == "__main__":
    init_db()
