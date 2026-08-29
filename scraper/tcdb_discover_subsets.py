"""Discover per-set insert/parallel *subset* sids on TCDB and add them to the index.

The top-level year listing (``tcdb_discover``) only yields base products, e.g.
``2023-24 Panini Prizm`` (one sid). Every insert and parallel *subset* of that
product is its own set with its own checklist, listed on the product's dedicated
Inserts page: ``/Inserts.cfm/sid/<sid>/<slug>``.

That page has two sections:

* **Insert Sets** — the named inserts (``Deep Space``, ``Fast Break Autographs``)
  interleaved with their colour/finish parallels (``Deep Space Prizms Green``).
* **Parallel Sets** — pure base-set colour parallels (``Prizms Black``, …).

Each subset is linked as ``/Checklist.cfm/sid/<subsid>/<slug>`` with the subset
name as the anchor text, so we get the sid **and** the slug in one shot (no slug
resolution needed).

Scopes:

* ``curated`` (default) — keep the Insert Sets section only, and within it keep
  distinct inserts plus autograph/relic sub-inserts (unique checklists) while
  dropping pure colour/finish parallels. Skips the Parallel Sets section
  entirely. High value, low near-duplicate bloat.
* ``marquee`` — only the marquee products, but ALL their subsets (both sections,
  no parallel filtering).
* ``all`` — every subset of every product (both sections, no filtering).

Subsets inherit the parent's sport/year/Panini status, so we do not re-run the
brand filter on their (brand-less) names. Cross-referenced other-season sets are
dropped: a genuine same-season subset is named without a year prefix, so any
subset name that starts with a 4-digit year other than the parent's is skipped.

Output is merged into the same ``data/scrape_index.json`` consumed by
``tcdb_download --from-index``; existing entries are preserved.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
from dataclasses import asdict

from bs4 import BeautifulSoup

from scraper.tcdb_discover import (
    INDEX_PATH,
    IndexEntry,
    SPORT_URL_NAMES,
    _discover_one,
    _get,
)

INSERTS_URL = "https://www.tcdb.com/Inserts.cfm/sid/{sid}/{slug}"


def _get_backoff(url: str, tries: int = 4) -> str:
    """_get with exponential backoff on HTTP 429 (honours Retry-After)."""
    delay = 20.0
    for attempt in range(1, tries + 1):
        try:
            return _get(url)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries:
                wait = delay
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and retry_after.isdigit():
                    wait = max(wait, float(retry_after))
                print(f"    · 429; backing off {wait:.0f}s (attempt {attempt}/{tries})")
                time.sleep(wait)
                delay = min(delay * 2, 180.0)
                continue
            raise

_CHECKLIST_RE = re.compile(r"/Checklist\.cfm/sid/(\d+)/([^\"'?<>\s]*)")
_YEAR_PREFIX_RE = re.compile(r"^\s*(\d{4})")

# A subset whose name (after stripping an accepted base insert prefix) still
# contains one of these words is a distinct sub-insert with its own checklist —
# an autograph/relic/material type — not a mere colour parallel, so keep it.
_DISTINCT_RE = re.compile(
    r"\b(auto|autos|autograph|autographs|signature|signatures|sig|sigs|"
    r"relic|relics|material|materials|jersey|jerseys|patch|patches|"
    r"ink|booklet|dual|triple|quad|jumbo|prime|rookie|rookies|rpm|rps|"
    r"nameplate|tag|laundry|logoman|shoe|sneaker|button|memorabilia)\b",
    re.IGNORECASE,
)

# Marquee products — matched case-insensitively as substrings of the parent name.
MARQUEE_PRODUCTS = [
    "prizm", "select", "national treasures", "immaculate", "flawless",
]


def _parse_sections(html: str, parent_sid: int) -> dict[str, list[tuple[int, str]]]:
    """Return {'insert': [(sid, name)], 'parallel': [(sid, name)]} from an Inserts page."""
    soup = BeautifulSoup(html, "lxml")
    sections: dict[str, list[tuple[int, str]]] = {"insert": [], "parallel": []}
    current: str | None = None
    for el in soup.find_all(["h1", "h2", "h3", "h4", "a"]):
        if el.name[0] == "h":
            heading = el.get_text(" ", strip=True)
            if "Insert Sets" in heading:
                current = "insert"
            elif "Parallel Sets" in heading:
                current = "parallel"
        elif el.name == "a" and el.has_attr("href") and current is not None:
            m = _CHECKLIST_RE.search(el["href"])
            text = el.get_text(" ", strip=True)
            if m and text and int(m.group(1)) != parent_sid:
                sections[current].append((int(m.group(1)), text, m.group(2)))  # type: ignore[arg-type]
    return sections


def _curate_inserts(links: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    """Keep distinct inserts + auto/relic sub-inserts; drop colour/finish parallels.

    Processes names shortest-first so base inserts are seen before their variants.
    A name is a parallel when a shorter accepted base is a word-prefix of it and
    the trailing remainder is not a distinct-insert keyword.
    """
    ordered = sorted(links, key=lambda t: len(t[1]))
    bases: list[str] = []
    kept: list[tuple[int, str, str]] = []
    for sid, name, slug in ordered:
        longest_base: str | None = None
        for b in bases:
            if name.startswith(b + " ") and (longest_base is None or len(b) > len(longest_base)):
                longest_base = b
        if longest_base is not None:
            remainder = name[len(longest_base) + 1:]
            if _DISTINCT_RE.search(remainder):
                bases.append(name)
                kept.append((sid, name, slug))
            # else: colour/finish parallel — drop
        else:
            bases.append(name)
            kept.append((sid, name, slug))
    return kept


def _subsets_for_parent(
    parent: IndexEntry, scope: str
) -> tuple[list[IndexEntry], int]:
    """Fetch a parent's Inserts page and return (kept subset entries, dropped count)."""
    url = INSERTS_URL.format(sid=parent.sid, slug=parent.slug)
    try:
        html = _get_backoff(url)
    except urllib.error.HTTPError as e:
        print(f"    ✗ Inserts sid {parent.sid}: HTTP {e.code}")
        return [], 0
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ Inserts sid {parent.sid}: {type(e).__name__} {e}")
        return [], 0

    sections = _parse_sections(html, parent.sid)
    if scope == "curated":
        candidates = _curate_inserts(sections["insert"])
        total_seen = len(sections["insert"]) + len(sections["parallel"])
    else:  # marquee / all — everything, both sections
        candidates = sections["insert"] + sections["parallel"]
        total_seen = len(candidates)

    entries: list[IndexEntry] = []
    seen: set[int] = set()
    for sid, name, slug in candidates:
        if sid in seen:
            continue
        # Drop cross-referenced other-season sets (they carry an explicit,
        # non-matching year prefix; true same-season subsets are bare-named).
        ym = _YEAR_PREFIX_RE.match(name)
        if ym and int(ym.group(1)) != parent.year:
            continue
        seen.add(sid)
        entries.append(
            IndexEntry(sid=sid, sport=parent.sport, year=parent.year,
                       set_name=name, slug=slug)
        )
    dropped = total_seen - len(entries)
    return entries, dropped


def _run(sports: list[str], years: list[int], scope: str, delay: float,
         dry_run: bool, limit_parents: int | None) -> None:
    existing: dict[int, dict] = {}
    if INDEX_PATH.exists():
        for entry in json.loads(INDEX_PATH.read_text(encoding="utf-8")):
            existing[entry["sid"]] = entry

    new_entries: dict[int, IndexEntry] = {}
    for sport in sports:
        for year in years:
            parents = _discover_one(sport, year)
            if scope == "marquee":
                parents = [p for p in parents
                           if any(m in p.set_name.lower() for m in MARQUEE_PRODUCTS)]
            if limit_parents:
                parents = parents[:limit_parents]
            print(f"\n{sport} {year}: {len(parents)} parent products (scope={scope})")
            for i, p in enumerate(parents, 1):
                subs, dropped = _subsets_for_parent(p, scope)
                fresh = sum(1 for s in subs if s.sid not in existing and s.sid not in new_entries)
                for s in subs:
                    new_entries.setdefault(s.sid, s)
                print(f"  [{i:>2}/{len(parents)}] {p.set_name[:44]:44} "
                      f"+{len(subs):>3} subsets ({fresh} new, {dropped} skipped)")
                time.sleep(delay)

    truly_new = {sid: e for sid, e in new_entries.items() if sid not in existing}
    print(f"\n=== {len(new_entries)} subset sids found; "
          f"{len(truly_new)} new (not already in index) ===")

    if dry_run:
        print("(dry-run: index not written)")
        return

    for sid, e in new_entries.items():
        existing.setdefault(sid, asdict(e))
    out = sorted(existing.values(), key=lambda e: (e["sport"], e["year"], e["set_name"]))
    INDEX_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {len(out)} total entries to {INDEX_PATH} (+{len(truly_new)} new subsets)")


def main() -> None:
    p = argparse.ArgumentParser(description="Discover TCDB insert/parallel subset sids.")
    p.add_argument("--sport", action="append", required=True, help="repeatable")
    p.add_argument("--year", action="append", type=int, required=True, help="repeatable")
    p.add_argument("--scope", choices=["curated", "marquee", "all"], default="curated")
    p.add_argument("--delay", type=float, default=1.5,
                   help="seconds between parent Inserts-page fetches")
    p.add_argument("--dry-run", action="store_true", help="print counts, don't write index")
    p.add_argument("--limit-parents", type=int, help="only process the first N parents (testing)")
    args = p.parse_args()

    for s in args.sport:
        if s not in SPORT_URL_NAMES:
            p.error(f"Unknown sport '{s}'. Known: {', '.join(SPORT_URL_NAMES)}")

    _run(args.sport, args.year, args.scope, args.delay, args.dry_run, args.limit_parents)


if __name__ == "__main__":
    main()
