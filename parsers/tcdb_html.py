"""Parse a TCDB checklist HTML page into normalized card rows.

Real TCDB checklist pages (``/Checklist.cfm/sid/<sid>/<slug>``) render one
``<tr>`` per card, but the table has **no header row** — the columns are
positional and unlabeled. What every card row *does* have is a set of anchor
links that identify each column unambiguously:

    * the player cell links to ``/Person.cfm/pid/<pid>``
    * the card-number cell links to ``/ViewCard.cfm/...`` (its text is the number)
    * the team cell links to ``/Team.cfm/tid/<tid>``

So the primary parser identifies columns by *link target*, not header text.
A header-based fallback is kept for simple/legacy tables (and the synthetic
smoke-test fixture) that do carry a labeled header and no per-cell links.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from .normalize import (
    ParallelFlags,
    clean_display_name,
    detect_flags,
    extract_print_run,
    normalize_player_name,
)


@dataclass
class ParsedCard:
    card_number: str | None
    player_display: str
    player_normalized: str
    team: str | None
    parallel_name: str | None
    print_run: int | None
    flags: ParallelFlags
    notes: str | None


_PERSON_RE = re.compile(r"/Person\.cfm/pid/\d+", re.IGNORECASE)
_VIEWCARD_RE = re.compile(r"/ViewCard\.cfm/", re.IGNORECASE)
_TEAM_RE = re.compile(r"/Team\.cfm/tid/\d+", re.IGNORECASE)


def parse_tcdb_html(html: str) -> list[ParsedCard]:
    """Parse a single TCDB checklist page's HTML into card rows.

    Tries link-based row parsing first (real TCDB pages); falls back to
    header-based column classification (legacy/simple tables) if that yields
    nothing.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = _parse_by_links(soup)
    if cards:
        return cards
    return _parse_by_header(soup)


def parse_file(path: str | Path) -> list[ParsedCard]:
    html = Path(path).read_text(encoding="utf-8", errors="ignore")
    return parse_tcdb_html(html)


# --------------------------------------------------------------------------- #
# Primary: link-based row parsing
# --------------------------------------------------------------------------- #
def _parse_by_links(soup: BeautifulSoup) -> list[ParsedCard]:
    """Pull one card per <tr> that contains a /Person.cfm player link.

    Robust to the headerless, positional TCDB checklist layout and to the
    surrounding navigation/insert tables (those rows have no player link).
    """
    parsed: list[ParsedCard] = []
    seen_rows = 0
    for tr in soup.find_all("tr"):
        person = tr.find("a", href=_PERSON_RE)
        if person is None:
            continue
        raw_player = person.get_text(" ", strip=True)
        if not raw_player:
            continue
        seen_rows += 1

        card_number = _card_number_from_row(tr)
        team = _team_from_row(tr)

        # TCDB puts markers such as ``AU`` outside the player anchor:
        # ``<a>Rome Odunze</a> AU``. Inspect the whole player cell for flags,
        # while keeping the anchor text as the canonical display name.
        player_cell = person.find_parent("td")
        player_context = (
            player_cell.get_text(" ", strip=True)
            if player_cell is not None
            else raw_player
        )

        display = clean_display_name(raw_player)
        # TCDB packs the parallel/variation descriptor into the player cell,
        # trailing the player anchor: "<a>Arch Manning</a> SN49 Burnt Orange".
        # Everything after the anchor name is the parallel label ("Base",
        # "SN250 Silver", "AU, SN1 Black"); an empty tail = a plain base card.
        parallel = _parallel_from_cell(player_context, raw_player)
        flags = detect_flags(player_context, card_number or "")
        parsed.append(
            ParsedCard(
                card_number=card_number,
                player_display=display,
                player_normalized=normalize_player_name(display),
                team=team,
                parallel_name=parallel,
                print_run=extract_print_run(parallel or player_context),
                flags=flags,
                notes=None,
            )
        )
    return parsed


def _parallel_from_cell(player_context: str, raw_player: str) -> str | None:
    """The parallel label is the player-cell text after the player's name.

    ``player_context`` is the full cell ("Arch Manning SN49 Burnt Orange");
    ``raw_player`` is the anchor text ("Arch Manning"). Strip the name prefix and
    any leading separators. Returns None for a bare base card (no trailing text).
    """
    if not player_context or not raw_player:
        return None
    # Only extract when the anchor name is a clean prefix of the cell. If it
    # isn't (unexpected markup), don't guess — returning None keeps the old
    # behavior rather than dumping the player's name into parallel_name.
    if not player_context.startswith(raw_player):
        return None
    tail = player_context[len(raw_player):].lstrip(" ,-–—")  # sep chars
    return tail.strip() or None


def _card_number_from_row(tr) -> str | None:
    """The card number is the text of the row's /ViewCard.cfm link (the image
    thumbnail links to the same href but has empty text — skip it)."""
    for a in tr.find_all("a", href=_VIEWCARD_RE):
        txt = a.get_text(" ", strip=True)
        if txt:
            return txt
    return None


def _team_from_row(tr) -> str | None:
    team = tr.find("a", href=_TEAM_RE)
    if team is not None:
        txt = team.get_text(" ", strip=True)
        return txt or None
    return None


# --------------------------------------------------------------------------- #
# Fallback: header-based column classification (legacy / simple tables)
# --------------------------------------------------------------------------- #
_HEADER_MAP = {
    "card": "number",
    "no.": "number",
    "no": "number",
    "#": "number",
    "player": "player",
    "name": "player",
    "subject": "player",
    "team": "team",
    "club": "team",
    "notes": "notes",
    "info": "notes",
    "rc": "rookie_marker",
}


def _classify_columns(header_cells: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for idx, raw in enumerate(header_cells):
        key = raw.strip().lower()
        if key in _HEADER_MAP:
            out[idx] = _HEADER_MAP[key]
            continue
        for prefix, name in _HEADER_MAP.items():
            if key.startswith(prefix):
                out[idx] = name
                break
    return out


def _row_text(cells: list, columns: dict[int, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for idx, cell in enumerate(cells):
        name = columns.get(idx)
        if not name:
            continue
        out[name] = cell.get_text(" ", strip=True)
    return out


def _parse_by_header(soup: BeautifulSoup) -> list[ParsedCard]:
    table = (
        soup.select_one("table#cardlist")
        or soup.select_one("table.set-checklist")
        or soup.select_one("table.checklist")
        or _largest_table(soup)
    )
    if table is None:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    header_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    columns = _classify_columns(header_cells)
    if "player" not in columns.values():
        thead = table.find("thead")
        if thead:
            header_cells = [c.get_text(" ", strip=True) for c in thead.find_all(["th", "td"])]
            columns = _classify_columns(header_cells)
        if "player" not in columns.values():
            return []

    parsed: list[ParsedCard] = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        text = _row_text(cells, columns)
        raw_player = text.get("player", "").strip()
        if not raw_player:
            continue

        notes = text.get("notes")
        parallel = _extract_parallel(notes, raw_player)
        flags = detect_flags(parallel or "", notes or "", raw_player)
        display = clean_display_name(raw_player)

        parsed.append(
            ParsedCard(
                card_number=text.get("number") or None,
                player_display=display,
                player_normalized=normalize_player_name(display),
                team=text.get("team") or None,
                parallel_name=parallel,
                print_run=extract_print_run(parallel or notes or ""),
                flags=flags,
                notes=notes or None,
            )
        )

    return parsed


def _largest_table(soup: BeautifulSoup):
    tables = soup.find_all("table")
    if not tables:
        return None
    return max(tables, key=lambda t: len(t.find_all("tr")))


_PARALLEL_HINT = re.compile(r"(prizm|refractor|parallel|/\d+|gold|silver|black|holo|wave)", re.IGNORECASE)


def _extract_parallel(notes: str | None, player_text: str) -> str | None:
    if notes and _PARALLEL_HINT.search(notes):
        return notes.strip()
    return None
