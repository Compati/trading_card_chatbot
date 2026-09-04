"""One-off + reusable repair: give every subset set its full TCDB product name.

Curated subsets discovered via ``tcdb_discover_subsets`` were stored under their
bare anchor text ("Prestige", "Recon", "Rookie Autographs"), losing the parent
product context ("2022 Panini Chronicles Draft Picks - Prestige"). The card data
is complete, but a set named bare "Prestige" is undiscoverable when a user asks
about "Chronicles Draft Picks", and ambiguous against the standalone Prestige
product.

The authoritative display name is the checklist page ``<title>`` (e.g.
``2022 Panini Chronicles Draft Picks - Prestige Football Checklist | Trading
Card Database``); ``set_display_name`` strips the trailing "<Sport> Checklist |
Trading Card Database" boilerplate. This module exposes that helper (imported by
``ingest.load`` so future pulls name sets the same way) and, run as a script,
backfills existing rows by a **direct name-only UPDATE** — it does NOT re-ingest
and does NOT touch brand_id/year, so it cannot trigger the (brand,name)-key
duplicate-set trap.
"""
from __future__ import annotations

import html
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "cards.db"
RAW_DIR = ROOT / "data" / "raw" / "tcdb"

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# Trailing site boilerplate: optional sport word + "Checklist | Trading Card Database"
_SUFFIX_RE = re.compile(
    r"\s*(?:Football|Basketball|Baseball|Soccer|Racing|Hockey|Wrestling|MMA|Golf)?"
    r"\s*Checklist\s*\|\s*Trading Card Database\s*$",
    re.IGNORECASE,
)


def title_from_html(text: str) -> str | None:
    """Extract TCDB's set display name from a checklist page's ``<title>``.

    Returns the product name with the site boilerplate stripped, or None if the
    page has no usable title (e.g. an error page).
    """
    m = _TITLE_RE.search(text)
    if not m:
        return None
    raw = html.unescape(m.group(1)).strip()
    name = _SUFFIX_RE.sub("", raw).strip()
    # If the suffix wasn't present, the page isn't a normal checklist page.
    if not name or name.lower() == raw.lower():
        return None
    return name


def title_for_sid(sport: str, year: int, sid: int) -> str | None:
    """Read the display name from a downloaded set's first raw page, if present.

    Tries the ``<sport>/<year>`` path first, then any year folder for that sport
    (a "Retro <old-year>" subset is filed on disk under its scrape year but its
    DB year is the retro theme year, so the two folders differ).
    """
    candidates = [
        RAW_DIR / sport / str(year) / f"set_{sid}_p1.html",
        RAW_DIR / sport / str(year) / f"set_{sid}.html",
    ]
    candidates += sorted(RAW_DIR.glob(f"{sport}/*/set_{sid}_p1.html"))
    candidates += sorted(RAW_DIR.glob(f"{sport}/*/set_{sid}.html"))
    for p1 in candidates:
        if p1.exists():
            head = p1.read_text(encoding="utf-8", errors="replace")[:8000]
            name = title_from_html(head)
            if name:
                return name
    return None


def _is_bare(name: str) -> bool:
    """A stored name is a bare subset name if it lacks a leading 4-digit year."""
    return not re.match(r"^\d{4}", (name or "").strip())


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT s.id, s.name, s.tcdb_sid, s.year, sp.name AS sport "
        "FROM sets s JOIN sports sp ON sp.id = s.sport_id"
    ).fetchall()

    bare = [r for r in rows if _is_bare(r["name"])]
    print(f"{len(rows)} sets total; {len(bare)} bare-named subsets to check")

    renamed = no_raw = no_change = collided = 0
    for i, r in enumerate(bare, 1):
        if r["tcdb_sid"] is None:
            no_raw += 1
            continue
        full = title_for_sid(r["sport"], r["year"], r["tcdb_sid"])
        if not full:
            no_raw += 1
            continue
        if full == r["name"]:
            no_change += 1
            continue
        try:
            # Name-only update; brand_id/year untouched so brand detection and
            # the (brand,name) uniqueness of pre-existing rows are unaffected.
            conn.execute("UPDATE sets SET name = ? WHERE id = ?", (full, r["id"]))
        except sqlite3.IntegrityError:
            # Would collide with an existing (brand,sport,year,name) row — rare;
            # leave this set's bare name in place rather than merging blindly.
            collided += 1
            print(f"  ! collision, left as-is: sid={r['tcdb_sid']} -> {full!r}")
            continue
        renamed += 1
        if renamed <= 12:
            print(f"  {r['name']!r:34} -> {full!r}")
        if i % 2000 == 0:
            print(f"  ... scanned {i}/{len(bare)}", flush=True)

    conn.commit()
    print(f"\nrenamed {renamed} sets; {no_change} already full; "
          f"{no_raw} had no usable raw title (left as-is); {collided} collisions skipped")
    conn.close()


if __name__ == "__main__":
    main()
