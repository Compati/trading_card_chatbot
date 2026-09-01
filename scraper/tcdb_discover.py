"""Enumerate Panini sets on TCDB for a given sport/year over plain HTTP.

TCDB lists a year's sets at ``/ViewAll.cfm/sp/<SportName>/year/<year>`` (the
sport is the full capitalized name, e.g. ``Football`` — not a short code). Each
set is linked as ``/ViewSet.cfm/sid/<sid>/<slug>`` with the set's name as the
link text. We pull those links, keep the ones whose name matches a Panini brand,
and record ``sid`` + ``slug`` (the slug is what the checklist URL needs).

Output: data/scrape_index.json — the input to tcdb_download --from-index.
No browser needed; TCDB serves these pages to an ordinary HTTP client.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / "data" / "scrape_index.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

VIEWALL_URL = "https://www.tcdb.com/ViewAll.cfm/sp/{sport}/year/{year}"

# User-facing sport key -> TCDB's URL sport name.
SPORT_URL_NAMES = {
    "football": "Football",
    "basketball": "Basketball",
    "baseball": "Baseball",
    "hockey": "Hockey",
    "soccer": "Soccer",
    "racing": "Racing",
    "wwe": "Wrestling",
    "ufc": "MMA",
}

# Panini's brand families — we filter listings down to sets named with one of these.
PANINI_BRAND_NAMES = [
    "panini", "donruss", "prizm", "select", "mosaic", "optic", "absolute",
    "certified", "chronicles", "contenders", "crown royale", "elite",
    "flawless", "gold standard", "immaculate", "limited", "national treasures",
    "obsidian", "one and one", "phoenix", "pinnacle", "playbook", "playoff",
    "rookies and stars", "score", "spectra", "threads", "totally certified",
    "unparalleled", "xr", "zenith", "diamond kings", "court kings", "noir",
    "vertex", "instant", "instant impact", "encased", "impeccable", "luminance",
    "majestic", "origins", "pristine", "revolution", "status", "titanium",
    "titanium retail", "preferred", "stars and stripes", "elite extra edition",
    "kaboom", "downtown", "downtowns",
]

# Rival manufacturers whose set names share a word with a Panini sub-brand
# ("Topps Pristine", "Leaf Trinity Pristine", "Topps Triple Threads"). Rejected
# only when the maker LEADS the name (after an optional year) so ambiguous words
# mid-name in real Panini subsets ("... Wild Card Round", "... Prime Onyx") stay.
_RIVAL_MAKERS = [
    "topps", "bowman", "leaf", "fleer", "upper deck", "sage", "press pass",
    "goodwin", "goudey", "sportkings", "sports kings",
]
_RIVAL_MAKERS_YEAR_ONLY = ["onyx", "wild card"]
_LEADING_YEAR_RE = re.compile(r"^\s*(?:19|20)\d\d(?:-\d\d)?\s+", re.IGNORECASE)


def _is_rival_set(name: str) -> bool:
    lname = name.lower()
    had_year = bool(_LEADING_YEAR_RE.match(lname))
    rest = _LEADING_YEAR_RE.sub("", lname, count=1).strip()
    makers = _RIVAL_MAKERS + (_RIVAL_MAKERS_YEAR_ONLY if had_year else [])
    return any(re.match(rf"{re.escape(mk)}\b", rest) for mk in makers)


_VIEWSET_RE = re.compile(r"/ViewSet\.cfm/sid/(\d+)/([^\"'?<>\s]+)", re.IGNORECASE)


@dataclass
class IndexEntry:
    sid: int
    sport: str
    year: int
    set_name: str
    slug: str


def _is_panini_set(name: str) -> bool:
    if _is_rival_set(name):
        return False
    lname = name.lower()
    return any(brand in lname for brand in PANINI_BRAND_NAMES)


def _get(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="ignore")


def _discover_one(sport: str, year: int) -> list[IndexEntry]:
    url = VIEWALL_URL.format(sport=SPORT_URL_NAMES[sport], year=year)
    try:
        html = _get(url)
    except urllib.error.HTTPError as e:
        print(f"  ✗ {sport} {year}: HTTP {e.code}")
        return []
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {sport} {year}: {type(e).__name__} {e}")
        return []

    soup = BeautifulSoup(html, "lxml")
    seen: dict[int, tuple[str, str]] = {}  # sid -> (set_name, slug)
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text:  # Gallery/thumbnail links have empty text — skip
            continue
        m = _VIEWSET_RE.search(a["href"])
        if not m:
            continue
        sid = int(m.group(1))
        if sid in seen:
            continue
        if _is_panini_set(text):
            seen[sid] = (text, m.group(2))

    return [IndexEntry(sid=sid, sport=sport, year=year, set_name=name, slug=slug)
            for sid, (name, slug) in seen.items()]


def _run(sports: list[str], years: list[int], delay: float) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[int, dict] = {}
    if INDEX_PATH.exists():
        for entry in json.loads(INDEX_PATH.read_text(encoding="utf-8")):
            existing[entry["sid"]] = entry

    first = True
    for sport in sports:
        for year in years:
            if not first:
                time.sleep(delay)
            first = False
            print(f"Discovering {sport} {year}…")
            entries = _discover_one(sport, year)
            for e in entries:
                existing[e.sid] = asdict(e)
            print(f"  + {len(entries)} Panini sets")

    out = sorted(existing.values(), key=lambda e: (e["sport"], e["year"], e["set_name"]))
    INDEX_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {len(out)} entries to {INDEX_PATH}")


def main() -> None:
    p = argparse.ArgumentParser(description="Enumerate Panini sets on TCDB.")
    p.add_argument("--sport", action="append", help="repeatable; default: all known sports")
    p.add_argument("--year", action="append", type=int, required=True, help="repeatable; required")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between listing requests")
    args = p.parse_args()

    sports = args.sport or list(SPORT_URL_NAMES.keys())
    for s in sports:
        if s not in SPORT_URL_NAMES:
            p.error(f"Unknown sport '{s}'. Known: {', '.join(SPORT_URL_NAMES)}")

    _run(sports, args.year, args.delay)


if __name__ == "__main__":
    main()
