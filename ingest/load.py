"""Load parsed TCDB checklists into the SQLite database.

Two entry points:
    python -m ingest.load --sid 543291          # one set
    python -m ingest.load --from-raw            # everything under data/raw/tcdb

Idempotent per set: re-loading replaces the set's cards (so re-runs after a
parser fix don't duplicate rows).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from db.connection import get_connection, get_or_create
from ingest.fix_set_names import title_from_html
from parsers.tcdb_html import ParsedCard, parse_file

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "tcdb"
STATUS_PATH = ROOT / "data" / "scrape_status.json"

# Panini sub-brands we recognize in set names.  First-match wins, so longer/more
# specific names appear before the plain "Panini" catch-all.
BRAND_PATTERNS = [
    "National Treasures", "Crown Royale", "Diamond Kings", "Court Kings",
    "Gold Standard", "One and One", "Rookies and Stars", "Stars and Stripes",
    "Totally Certified", "Elite Extra Edition", "Titanium Retail",
    "Absolute", "Certified", "Chronicles", "Contenders", "Donruss",
    "Encased", "Elite", "Flawless", "Immaculate", "Impeccable", "Instant",
    "Kaboom", "Limited", "Luminance", "Majestic", "Mosaic", "Noir",
    "Obsidian", "Optic", "Origins", "Phoenix", "Pinnacle", "Playbook",
    "Playoff", "Preferred", "Pristine", "Prizm", "Revolution", "Score",
    "Select", "Spectra", "Status", "Threads", "Titanium", "Unparalleled",
    "Vertex", "XR", "Zenith", "Panini",
]

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Rival manufacturers whose products slip into discovery because their set names
# share a word with a Panini sub-brand (e.g. "Topps Pristine", "Leaf Trinity
# Pristine", "Topps Triple Threads" -> matched "Pristine"/"Threads"). We reject a
# set only when the maker is the LEADING product token (after an optional year),
# never on a mere substring — otherwise legitimate Panini subsets that merely
# contain an ambiguous word ("Rookie Ticket Wild Card Round", "RPS ... Prime
# Onyx") would be wrongly dropped.
_RIVAL_MAKERS = [
    "Topps", "Bowman", "Leaf", "Fleer", "Upper Deck", "Sage", "Press Pass",
    "Goodwin", "Goudey", "Sportkings", "Sports Kings", "Donruss Americana",
]
# These read as Panini parallels/subsets when bare, but as a rival product when
# they lead a year-prefixed set name ("2024 Onyx Limited Edition ...").
# "Uno" is the UNO card game (e.g. "2024 UNO Elite Core Edition"), not a Panini
# trading-card product.
_RIVAL_MAKERS_YEAR_ONLY = ["Onyx", "Wild Card", "Uno"]

_LEADING_YEAR_RE = re.compile(r"^\s*(?:19|20)\d\d(?:-\d\d)?\s+", re.IGNORECASE)


def is_non_panini(set_name: str) -> str | None:
    """Return the rival maker name if set_name is a non-Panini product, else None.

    Matches only when the maker is the leading token (after an optional leading
    year), so ambiguous words appearing mid-name in real Panini subsets are kept.
    """
    had_year = bool(_LEADING_YEAR_RE.match(set_name))
    rest = _LEADING_YEAR_RE.sub("", set_name, count=1).strip()
    for mk in _RIVAL_MAKERS:
        if re.match(rf"{re.escape(mk)}\b", rest, re.IGNORECASE):
            return mk
    if had_year:
        for mk in _RIVAL_MAKERS_YEAR_ONLY:
            if re.match(rf"{re.escape(mk)}\b", rest, re.IGNORECASE):
                return mk
    return None


def detect_brand(set_name: str) -> str:
    """Return the most-specific Panini sub-brand mentioned in set_name.

    Substring (not word-boundary) match on purpose: Panini subset names routinely
    pluralize/possessive the brand word ("Canton Absolutes", "Draft Picks Prizms",
    "Diamond Kings"), which a \\bbrand\\b match would miss and mislabel "Panini".
    """
    low = set_name.lower()
    for brand in BRAND_PATTERNS:
        if brand.lower() in low:
            return brand
    return "Panini"


def detect_year(set_name: str, fallback: int) -> int:
    m = _YEAR_RE.search(set_name)
    return int(m.group(0)) if m else fallback


_LEADING_YEAR_INT_RE = re.compile(r"^\s*((?:19|20)\d\d)")


def leading_year(name: str) -> int | None:
    """The product/release year at the START of a full TCDB title.

    TCDB titles begin with the release year ("2023 Clearly Donruss - Retro
    1993", "2022-23 Panini Flawless - ..."), so the leading year is the product
    year even when the subset name embeds an older theme year. Returns None if
    the name has no leading year (e.g. a bare subset name)."""
    m = _LEADING_YEAR_INT_RE.match(name or "")
    return int(m.group(1)) if m else None


def _load_status() -> dict:
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    return {}


def _upsert_player(conn, sport_id: int, normalized: str, display: str) -> int:
    """Find-or-create a player by (sport, normalized_name).

    On a hit, upgrade the stored display name to the variant that carries
    period-initials ("C.J. Stroud" beats "CJ Stroud") — both normalize to the
    same key, and the proper form is the nicer one to show users.
    """
    row = conn.execute(
        "SELECT id, full_name FROM players WHERE sport_id=? AND normalized_name=?",
        (sport_id, normalized),
    ).fetchone()
    if row:
        if "." in display and "." not in row["full_name"]:
            conn.execute("UPDATE players SET full_name=? WHERE id=?", (display, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO players (sport_id, normalized_name, full_name) VALUES (?,?,?)",
        (sport_id, normalized, display),
    )
    return cur.lastrowid


def _load_set(conn, *, brand: str, sport: str, year: int, set_name: str,
              source: str, source_url: str | None, source_file: str | None,
              tcdb_sid: int | None, cards: list[ParsedCard]) -> int:
    sport_id = get_or_create(conn, "sports", {"name": sport})
    brand_id = get_or_create(conn, "brands", {"name": brand})

    # Find or create the set; if it exists, wipe its cards first (idempotency).
    # Prefer the stable TCDB set id as the identity key: matching on
    # (brand,name) meant that any rename on re-ingest orphaned the old row and
    # created a duplicate. Keying on tcdb_sid lets a re-ingest refresh the row's
    # name/brand/year in place. Legacy rows without a tcdb_sid fall back to the
    # old (brand,sport,year,name) match.
    row = None
    if tcdb_sid is not None:
        row = conn.execute("SELECT id FROM sets WHERE tcdb_sid=?", (tcdb_sid,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM sets WHERE brand_id=? AND sport_id=? AND year=? AND name=?",
            (brand_id, sport_id, year, set_name),
        ).fetchone()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if row:
        set_id = row["id"]
        conn.execute("DELETE FROM cards WHERE set_id=?", (set_id,))
        conn.execute(
            "UPDATE sets SET name=?, brand_id=?, year=?, source=?, source_url=?, "
            "source_file=?, tcdb_sid=?, ingested_at=? WHERE id=?",
            (set_name, brand_id, year, source, source_url, source_file, tcdb_sid, now, set_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO sets (brand_id, sport_id, year, name, source, source_url, source_file, tcdb_sid, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (brand_id, sport_id, year, set_name, source, source_url, source_file, tcdb_sid, now),
        )
        set_id = cur.lastrowid

    inserted = 0
    for c in cards:
        if not c.player_display:
            continue
        player_id = _upsert_player(conn, sport_id, c.player_normalized, c.player_display)
        team_id = None
        if c.team:
            team_id = get_or_create(conn, "teams", {"sport_id": sport_id, "name": c.team.strip()})
        conn.execute(
            "INSERT INTO cards (set_id, card_number, player_id, team_id, parallel_name, print_run, "
            "is_auto, is_relic, is_rookie, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (set_id, c.card_number, player_id, team_id, c.parallel_name, c.print_run,
             int(c.flags.is_auto), int(c.flags.is_relic), int(c.flags.is_rookie), c.notes),
        )
        inserted += 1
    return inserted


def _ingest_tcdb_set(conn, html_paths: list[Path], sport: str, year: int,
                     set_name: str, sid: int, source_url: str | None) -> int:
    """Parse every downloaded page for one set and load the merged cards."""
    ordered = sorted(html_paths, key=_page_sort_key)
    # The DISPLAY name comes from the checklist page <title>, which carries the
    # full parent product ("2022 Panini Chronicles Draft Picks - Prestige") that
    # the bare subset index name drops. Falls back to the index name.
    display_name = title_from_html(ordered[0].read_text(encoding="utf-8", errors="replace")) or set_name
    # Rival check runs on the FULL title, not the bare name: a rival product's
    # subsets ("2025 Leaf Optichrome - Aquatic Autographs ...") have bare index
    # names with no maker word, so only the title reveals them as non-Panini.
    rival = is_non_panini(display_name) or is_non_panini(set_name)
    if rival:
        print(f"  ~ skipping non-Panini set sid={sid}: {display_name!r} ({rival})")
        return 0
    cards: list[ParsedCard] = []
    for path in ordered:
        cards.extend(parse_file(path))
    if not cards:
        print(f"  ! no cards parsed from sid={sid} ({len(html_paths)} file(s))")
        return 0
    # Brand is detected from the (bare) index name so it stays stable.
    brand = detect_brand(set_name)
    # Year comes from the title's leading (product) year, so a "Retro 1993"
    # insert of a 2023 product is filed under 2023 — not the embedded 1993 that
    # detect_year() on the bare name would grab. Falls back to the bare-name
    # scan then the index year.
    year = leading_year(display_name) or detect_year(set_name, fallback=year)
    rel_files = ", ".join(str(p.relative_to(ROOT)) for p in ordered)
    return _load_set(
        conn,
        brand=brand, sport=sport, year=year, set_name=display_name,
        source="tcdb",
        source_url=source_url or f"https://www.tcdb.com/ViewSet.cfm/sid/{sid}",
        source_file=rel_files,
        tcdb_sid=sid,
        cards=cards,
    )


# set_{sid}.html (legacy single-page) or set_{sid}_p{n}.html (paginated)
_PAGE_RE = re.compile(r"set_(\d+)(?:_p(\d+))?\.html$")


def _page_sort_key(path: Path) -> tuple[int, int]:
    m = _PAGE_RE.match(path.name)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def _walk_raw() -> list[tuple[int, str, int, list[Path]]]:
    """Yield (sid, sport, year, [html_paths]) for every downloaded TCDB set,
    grouping the per-page files that belong to the same sid."""
    out: list[tuple[int, str, int, list[Path]]] = []
    if not RAW_DIR.exists():
        return out
    for sport_dir in RAW_DIR.iterdir():
        if not sport_dir.is_dir():
            continue
        sport = sport_dir.name
        for year_dir in sport_dir.iterdir():
            if not year_dir.is_dir():
                continue
            try:
                year = int(year_dir.name)
            except ValueError:
                continue
            groups: dict[int, list[Path]] = {}
            for html in year_dir.glob("set_*.html"):
                m = _PAGE_RE.match(html.name)
                if m:
                    groups.setdefault(int(m.group(1)), []).append(html)
            for sid, paths in groups.items():
                out.append((sid, sport, year, paths))
    return out


def _pages_for_sid(sport: str, year: int, sid: int) -> list[Path]:
    d = RAW_DIR / sport / str(year)
    paths = list(d.glob(f"set_{sid}_p*.html"))
    legacy = d / f"set_{sid}.html"
    if legacy.exists():
        paths.append(legacy)
    return paths


def main() -> None:
    p = argparse.ArgumentParser(description="Load parsed TCDB checklists into SQLite.")
    p.add_argument("--sid", type=int, help="load just one set")
    p.add_argument("--from-raw", action="store_true",
                   help="load every TCDB file under data/raw/tcdb/")
    args = p.parse_args()

    status = _load_status()
    conn = get_connection(read_only=False)
    try:
        if args.sid:
            entry = status.get(str(args.sid))
            if not entry:
                p.error(f"sid {args.sid} not in scrape_status.json — download it first")
            paths = _pages_for_sid(entry["sport"], entry["year"], args.sid)
            if not paths:
                p.error(f"no downloaded pages for sid {args.sid} under data/raw/tcdb/")
            n = _ingest_tcdb_set(conn, paths, entry["sport"], entry["year"],
                                 entry["set_name"], args.sid, entry.get("checklist_url"))
            conn.commit()
            print(f"Loaded {n} cards from sid={args.sid} ({entry['set_name']})")
        elif args.from_raw:
            total = 0
            sets = _walk_raw()
            for sid, sport, year, paths in sets:
                st = status.get(str(sid)) or {}
                set_name = st.get("set_name") or f"sid {sid}"
                n = _ingest_tcdb_set(conn, paths, sport, year, set_name, sid, st.get("checklist_url"))
                print(f"  {sid}: {n} cards from {set_name}")
                total += n
            conn.commit()
            print(f"Loaded {total} cards across {len(sets)} set(s)")
        else:
            p.error("Provide --sid or --from-raw")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
