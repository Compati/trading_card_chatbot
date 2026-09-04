"""Link every subset/insert/parallel to its parent product via ``parent_set_id``.

The ``sets`` table is otherwise flat: each TCDB subset ("2022 Panini Chronicles
Draft Picks - Prestige") is its own row with no tie to its base product ("2022
Panini Chronicles Draft Picks"). A collector asking about a *product* wants the
whole family — base + every insert/parallel/auto/mem card — so we add a
self-referential ``parent_set_id`` (NULL on a base product, set on a subset).

Parent resolution is offline and name-based, made reliable by the title-based
naming fix: a subset name is ``<product> - <subset>``, so the text before the
first `" - "` is the product. It's linked to an existing base row (year-prefixed
name, no `" - "`); products whose base was never ingested get a synthetic 0-card
base row (source ``synthetic-parent``) so grouping is always complete.

Idempotent: re-running recomputes every link and reuses existing synthetic bases.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ingest.load import detect_brand, leading_year

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "cards.db"

_SEP = " - "


def _has_leading_year(name: str) -> bool:
    return bool(re.match(r"^\s*(?:19|20)\d\d", name or ""))


def _ensure_column(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sets)")]
    if "parent_set_id" not in cols:
        conn.execute("ALTER TABLE sets ADD COLUMN parent_set_id INTEGER REFERENCES sets(id)")
        print("added column sets.parent_set_id")


def _brand_id(conn: sqlite3.Connection, brand: str) -> int:
    row = conn.execute("SELECT id FROM brands WHERE name=?", (brand,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO brands (name) VALUES (?)", (brand,)).lastrowid


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    _ensure_column(conn)

    sets = conn.execute(
        "SELECT s.id, s.name, s.sport_id, s.year FROM sets s"
    ).fetchall()

    # Base products: year-prefixed, no separator. key -> set id
    base: dict[tuple[str, int], int] = {}
    for s in sets:
        if _has_leading_year(s["name"]) and _SEP not in s["name"]:
            base[(s["name"].strip(), s["sport_id"])] = s["id"]

    subsets = [s for s in sets if _SEP in s["name"]]

    # First pass: which parents are missing a base row?
    missing: dict[tuple[str, int], tuple[str, int]] = {}  # key -> (parent_name, year)
    for s in subsets:
        parent_name = s["name"].split(_SEP, 1)[0].strip()
        key = (parent_name, s["sport_id"])
        if key not in base:
            missing[key] = (parent_name, leading_year(parent_name) or s["year"])

    # Create synthetic base rows for missing parents.
    created = 0
    for (parent_name, sport_id), (pname, yr) in missing.items():
        brand_id = _brand_id(conn, detect_brand(pname))
        cur = conn.execute(
            "INSERT INTO sets (brand_id, sport_id, year, name, source) VALUES (?,?,?,?,?)",
            (brand_id, sport_id, yr, pname, "synthetic-parent"),
        )
        base[(parent_name, sport_id)] = cur.lastrowid
        created += 1
    print(f"created {created} synthetic base rows for orphan products")

    # Backfill parent_set_id for every subset; clear it on base rows.
    conn.execute("UPDATE sets SET parent_set_id=NULL")
    linked = 0
    for s in subsets:
        parent_name = s["name"].split(_SEP, 1)[0].strip()
        pid = base.get((parent_name, s["sport_id"]))
        if pid and pid != s["id"]:
            conn.execute("UPDATE sets SET parent_set_id=? WHERE id=?", (pid, s["id"]))
            linked += 1
    conn.commit()

    total_sub = len(subsets)
    base_ct = conn.execute("SELECT COUNT(*) FROM sets WHERE parent_set_id IS NULL").fetchone()[0]
    child_ct = conn.execute("SELECT COUNT(*) FROM sets WHERE parent_set_id IS NOT NULL").fetchone()[0]
    unlinked = conn.execute(
        f"SELECT COUNT(*) FROM sets WHERE name LIKE '%{_SEP}%' AND parent_set_id IS NULL"
    ).fetchone()[0]
    print(f"linked {linked}/{total_sub} subsets; base rows (parent_set_id NULL): {base_ct}; "
          f"children: {child_ct}; subsets still unlinked: {unlinked}")
    conn.close()


if __name__ == "__main__":
    main()
