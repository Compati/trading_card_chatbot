"""SQLite connection + schema init for the trading card database."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "cards.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    path = db_path or DB_PATH
    if read_only:
        wal_path = Path(f"{path}-wal")
        if wal_path.exists():
            uri = f"file:{path.resolve().as_posix()}?mode=ro"
        else:
            uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | None = None) -> None:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection(path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()
    print(f"Initialized database at {path}")


def get_or_create(conn: sqlite3.Connection, table: str, lookup: dict, insert: dict | None = None) -> int:
    """Return the id for a row matching `lookup`, inserting `insert` (or `lookup`) if absent.

    Used for the small dimension tables (sports, brands, teams, players, sets).
    """
    where = " AND ".join(f"{k} = ?" for k in lookup)
    row = conn.execute(f"SELECT id FROM {table} WHERE {where}", tuple(lookup.values())).fetchone()
    if row:
        return row["id"]
    payload = insert or lookup
    cols = ", ".join(payload)
    placeholders = ", ".join("?" for _ in payload)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", tuple(payload.values()))
    return cur.lastrowid


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init_db()
    else:
        print("Usage: python -m db.connection init")
        sys.exit(1)


if __name__ == "__main__":
    main()
