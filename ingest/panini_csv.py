"""Load a Panini America "Download Full Checklist" CSV into the database.

Panini's official checklist site (paniniamerica.net/checklist.html) exports a
flat CSV per product — one row per card x cardset — with columns:

    SPORT, YEAR, BRAND, PROGRAM, CARD SET, ATHLETE, TEAM, POSITION,
    CARD NUMBER, SEQUENCE

This is a *complement* to the TCDB pipeline, valuable exactly where TCDB is weak:
premium autographs and serial-numbered parallels TCDB leaves uncatalogued. The
SEQUENCE column carries the serial print run.

Mapping to our schema (one CSV row -> one `cards` row):
    PROGRAM              -> set name "{YEAR} {BRAND} {PROGRAM}", brand via detect_brand
    CARD SET             -> cards.parallel_name (+ is_auto/is_relic/is_rookie flags)
    ATHLETE              -> players (normalized key + display), per sport
    TEAM                 -> teams
    CARD NUMBER          -> cards.card_number
    SEQUENCE             -> cards.print_run  (blank -> NULL)

Usage:
    python -m ingest.panini_csv --csv "path/to/file.csv"            # dry run (default)
    python -m ingest.panini_csv --csv "path/to/file.csv" --write    # write to DB

Safety: a dry run writes nothing. On --write, a set is matched on
(brand, sport, year, name); if a matching set already exists from a NON-panini
source the load aborts rather than clobbering TCDB data (a reconciliation the
caller must resolve deliberately). Re-loading a panini-sourced set replaces its
cards (idempotent).
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from db.connection import get_connection, get_or_create
from ingest.load import detect_brand, is_non_panini
from parsers.normalize import clean_display_name, normalize_player_name

_AUTO_TOKENS = ("auto", "autograph", "signature", "ink", "sig ")
_RELIC_TOKENS = ("relic", "patch", "jersey", "material", "memorabilia",
                 "laundry tag", "button", "swatch", "prime")
_ROOKIE_TOKENS = ("rookie", " rc ", " rc", "rated rookie")

REQUIRED_COLS = {"SPORT", "YEAR", "BRAND", "PROGRAM", "CARD SET",
                 "ATHLETE", "TEAM", "CARD NUMBER", "SEQUENCE"}


def _flag(cardset: str, tokens) -> bool:
    low = f" {cardset.lower()} "
    return any(t in low for t in tokens)


def _print_run(sequence: str) -> int | None:
    s = (sequence or "").strip()
    return int(s) if s.isdigit() else None


def _split_people(athlete: str, team: str) -> list[tuple[str, str | None]]:
    """Split a possibly-combo ATHLETE/TEAM cell into (name, team) pairs.

    Dual/triple cards join athletes with "/" ("VJ Edgecombe/Tre Johnson III").
    Teams are paired by position when they split to the same count, else the
    single shared team is used, else no team.
    """
    names = [a.strip() for a in athlete.split("/") if a.strip()]
    teams = [t.strip() for t in team.split("/") if t.strip()]
    if not names:
        return []
    out = []
    for i, name in enumerate(names):
        if len(teams) == len(names):
            tm = teams[i]
        elif len(teams) == 1:
            tm = teams[0]
        else:
            tm = None
        out.append((name, tm or None))
    return out


def _read_rows(path: Path) -> list[dict]:
    # utf-8-sig strips a BOM if present.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLS - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV missing expected columns: {sorted(missing)}")
        return [r for r in reader if (r.get("ATHLETE") or "").strip()]


def _group_sets(rows: list[dict]) -> dict[tuple, list[dict]]:
    """Group rows into one set per (sport, year, program)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        sport = r["SPORT"].strip().lower()
        year = int(str(r["YEAR"]).strip())
        program = r["PROGRAM"].strip()
        groups[(sport, year, program)].append(r)
    return groups


def _set_name(year: int, brand_field: str, program: str) -> str:
    # "2025 Panini Court Kings" — brand_field is Panini's manufacturer column.
    return f"{year} {brand_field.strip()} {program}".strip()


def summarize(rows: list[dict]) -> None:
    groups = _group_sets(rows)
    print(f"{len(rows)} card rows -> {len(groups)} set(s)\n")
    for (sport, year, program), grp in groups.items():
        brand_field = grp[0]["BRAND"].strip()
        name = _set_name(year, brand_field, program)
        brand = detect_brand(program)
        rival = is_non_panini(name)
        players = {normalize_player_name(r["ATHLETE"]) for r in grp}
        cardsets = {r["CARD SET"].strip() for r in grp}
        autos = sum(1 for r in grp if _flag(r["CARD SET"], _AUTO_TOKENS))
        relics = sum(1 for r in grp if _flag(r["CARD SET"], _RELIC_TOKENS))
        numbered = sum(1 for r in grp if _print_run(r["SEQUENCE"]) is not None)
        print(f"  SET  {name!r}")
        print(f"       sport={sport}  year={year}  brand={brand}"
              + (f"  !! RIVAL ({rival})" if rival else ""))
        print(f"       {len(grp)} cards | {len(cardsets)} cardsets | "
              f"{len(players)} players | {autos} auto | {relics} relic | {numbered} serial-numbered")
        sample = grp[0]
        print(f"       e.g. #{sample['CARD NUMBER']} {sample['ATHLETE']} "
              f"[{sample['CARD SET']}] /{sample['SEQUENCE'] or '-'}\n")


def load(conn, rows: list[dict], source_file: str) -> tuple[int, int]:
    """Write rows to the DB. Returns (sets_written, cards_written)."""
    groups = _group_sets(rows)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    sets_written = cards_written = 0

    for (sport, year, program), grp in groups.items():
        brand_field = grp[0]["BRAND"].strip()
        name = _set_name(year, brand_field, program)
        rival = is_non_panini(name)
        if rival:
            print(f"  ~ skipping non-Panini set {name!r} ({rival})")
            continue

        sport_id = get_or_create(conn, "sports", {"name": sport})
        brand_id = get_or_create(conn, "brands", {"name": detect_brand(program)})

        row = conn.execute(
            "SELECT id, source FROM sets WHERE brand_id=? AND sport_id=? AND year=? AND name=?",
            (brand_id, sport_id, year, name),
        ).fetchone()
        if row and row["source"] not in (None, "panini"):
            raise SystemExit(
                f"REFUSING to overwrite existing {row['source']!r}-sourced set {name!r} "
                f"(id={row['id']}). Resolve this reconciliation before loading."
            )
        if row:
            set_id = row["id"]
            conn.execute("DELETE FROM cards WHERE set_id=?", (set_id,))
            conn.execute(
                "UPDATE sets SET source=?, source_file=?, ingested_at=? WHERE id=?",
                ("panini", source_file, now, set_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO sets (brand_id, sport_id, year, name, source, source_file, ingested_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (brand_id, sport_id, year, name, "panini", source_file, now),
            )
            set_id = cur.lastrowid
        sets_written += 1

        for r in grp:
            cardset = r["CARD SET"].strip()
            card_no = (r["CARD NUMBER"] or "").strip() or None
            print_run = _print_run(r["SEQUENCE"])
            is_auto = int(_flag(cardset, _AUTO_TOKENS))
            is_relic = int(_flag(cardset, _RELIC_TOKENS))
            is_rookie = int(_flag(cardset, _ROOKIE_TOKENS))
            # Dual/triple cards list several athletes (and teams) joined by "/".
            # Emit one row per real player so each is searchable, recording the
            # full pairing in notes rather than storing a bogus joined-name player.
            people = _split_people(r["ATHLETE"], r["TEAM"])
            notes = f"Combo: {r['ATHLETE'].strip()}" if len(people) > 1 else None
            for athlete, team_name in people:
                player_id = _upsert_player(conn, sport_id, athlete)
                team_id = None
                if team_name:
                    team_id = get_or_create(conn, "teams",
                                            {"sport_id": sport_id, "name": team_name})
                conn.execute(
                    "INSERT INTO cards (set_id, card_number, player_id, team_id, parallel_name, "
                    "print_run, is_auto, is_relic, is_rookie, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (set_id, card_no, player_id, team_id, cardset or None, print_run,
                     is_auto, is_relic, is_rookie, notes),
                )
                cards_written += 1

    return sets_written, cards_written


def _upsert_player(conn, sport_id: int, athlete: str) -> int:
    """Find-or-create by (sport, normalized_name); prefer the period-form display."""
    normalized = normalize_player_name(athlete)
    display = clean_display_name(athlete)
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


def main() -> None:
    p = argparse.ArgumentParser(description="Load a Panini checklist CSV into the DB.")
    p.add_argument("--csv", required=True, help="path to the Panini 'Download Full Checklist' CSV")
    p.add_argument("--write", action="store_true",
                   help="actually write to the DB (default is a dry run that writes nothing)")
    args = p.parse_args()

    path = Path(args.csv).expanduser()
    if not path.exists():
        raise SystemExit(f"CSV not found: {path}")
    rows = _read_rows(path)

    if not args.write:
        print(f"DRY RUN — {path.name}\n")
        summarize(rows)
        print("(dry run: nothing written; re-run with --write to load)")
        return

    conn = get_connection(read_only=False)
    try:
        sets_written, cards_written = load(conn, rows, source_file=path.name)
        conn.commit()
        print(f"Wrote {cards_written} cards across {sets_written} set(s) from {path.name}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
