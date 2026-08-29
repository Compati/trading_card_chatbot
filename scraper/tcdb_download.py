"""Download one or many TCDB checklist pages over plain HTTP.

TCDB serves checklist HTML to an ordinary HTTP client (normal User-Agent, no
Cloudflare challenge), so no headless browser is needed. Two things the URL
*does* require:

  * the **slug** — ``/Checklist.cfm/sid/<sid>/<slug>``. The bare
    ``/Checklist.cfm/sid/<sid>`` 404s. When a slug isn't supplied (single-set
    mode) we resolve it by fetching the sid's ``/ViewSet.cfm/sid/<sid>`` page
    (which *does* accept a bare sid) and reading its "Checklist" link.
  * pagination — checklists page at ``?PageIndex=1,2,…`` (100 cards/page). A
    page past the end still returns HTTP 200 but with zero card rows, which is
    our stop signal.

Two modes:
    Single set: --sid 462124 --sport football --year 2024 --set-name "2024 Donruss"
    Batch:      --from-index   (reads data/scrape_index.json)

Output: data/raw/tcdb/{sport}/{year}/set_{sid}_p{n}.html (one file per page)
plus a status entry in data/scrape_status.json so reruns can skip finished sets.
Rate-limited (randomized delay between sets; short delay between pages).
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from dataclasses import dataclass, field
from pathlib import Path

from parsers.tcdb_html import parse_tcdb_html

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw" / "tcdb"
INDEX_PATH = DATA_DIR / "scrape_index.json"
STATUS_PATH = DATA_DIR / "scrape_status.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

BASE = "https://www.tcdb.com"
CHECKLIST_URL = BASE + "/Checklist.cfm/sid/{sid}/{slug}"
VIEWSET_URL = BASE + "/ViewSet.cfm/sid/{sid}"

MAX_PAGES = 60  # safety cap; base sets are a handful of pages

_404_MARKER = "DefaultError404"


@dataclass
class DownloadJob:
    sid: int
    sport: str
    year: int
    set_name: str
    slug: str | None = None
    pages: list[Path] = field(default_factory=list)

    def out_dir(self) -> Path:
        return RAW_DIR / self.sport / str(self.year)

    def page_path(self, page: int) -> Path:
        return self.out_dir() / f"set_{self.sid}_p{page}.html"

    def pages_exist(self) -> bool:
        d = self.out_dir()
        return d.exists() and bool(list(d.glob(f"set_{self.sid}_p*.html")))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 30) -> tuple[str, str]:
    """Return (final_url, html). Raises on network/HTTP error."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return resp.geturl(), raw.decode("utf-8", errors="ignore")


def _get_retrying(url: str, *, max_retries: int = 5, base_backoff: float = 20.0) -> tuple[str, str]:
    """_get with exponential backoff on HTTP 429 (TCDB rate-limiting).

    A 429 is transient — TCDB throttles bursts but recovers. Rather than fail
    the whole set, sleep (honoring Retry-After when present) and retry the same
    page. Backoff grows 20s, 40s, 80s, … Raises the last error if all retries
    are exhausted or the error isn't a 429.
    """
    for attempt in range(max_retries + 1):
        try:
            return _get(url)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == max_retries:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(retry_after) if retry_after else base_backoff * (2 ** attempt)
            except ValueError:
                wait = base_backoff * (2 ** attempt)
            wait = min(wait, 180.0)
            print(f"    429 — backing off {wait:.0f}s (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


def _is_404(final_url: str, html: str) -> bool:
    return _404_MARKER in final_url or "404 - Page Not Found" in html


# --------------------------------------------------------------------------- #
# Slug resolution
# --------------------------------------------------------------------------- #
def resolve_slug(sid: int) -> str | None:
    """Fetch the (bare-sid-tolerant) ViewSet page and read the checklist slug."""
    try:
        final_url, html = _get(VIEWSET_URL.format(sid=sid))
    except Exception as e:  # noqa: BLE001
        print(f"    slug resolve failed for sid {sid}: {e}", file=sys.stderr)
        return None
    if _is_404(final_url, html):
        return None
    m = re.search(rf"/Checklist\.cfm/sid/{sid}/([^\"'?<>\s]+)", html, re.IGNORECASE)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _clear_old_pages(job: DownloadJob) -> None:
    """Remove any prior files for this sid so a shrunk set leaves no orphans."""
    d = job.out_dir()
    if not d.exists():
        return
    for p in list(d.glob(f"set_{job.sid}_p*.html")) + list(d.glob(f"set_{job.sid}.html")):
        try:
            p.unlink()
        except OSError:
            pass


def download_set(job: DownloadJob, page_delay: float = 3.0) -> tuple[str, str]:
    """Download every checklist page for one set. Returns (status, message)."""
    slug = job.slug or resolve_slug(job.sid)
    if not slug:
        return "failed", "could not resolve checklist slug"
    job.slug = slug

    job.out_dir().mkdir(parents=True, exist_ok=True)
    _clear_old_pages(job)

    total_cards = 0
    seen_numbers: set[str] = set()  # card numbers gathered so far, for dup-page detection
    for page in range(1, MAX_PAGES + 1):
        # Percent-encode the slug so non-ASCII chars (e.g. the é in "Béisbol")
        # don't blow up urllib's ascii-only request line. safe="" so any
        # already-raw non-ASCII byte is encoded; ASCII slugs are unchanged.
        url = CHECKLIST_URL.format(sid=job.sid, slug=quote(slug, safe="-_./")) + f"?PageIndex={page}"
        try:
            final_url, html = _get_retrying(url)
        except urllib.error.HTTPError as e:
            return "failed", f"HTTP {e.code} on page {page}"
        except Exception as e:  # noqa: BLE001
            return "failed", f"{type(e).__name__} on page {page}: {e}"

        if _is_404(final_url, html):
            if page == 1:
                return "failed", "checklist 404 (bad slug/sid)"
            break  # past the last page
        # Stop on the first page with no real checklist rows. We parse for
        # actual card rows rather than counting raw /Person.cfm links: for small
        # subset sets, TCDB serves a nav-only page (still full of Person.cfm
        # sidebar links) for any out-of-range PageIndex, so a raw link count
        # never reaches zero and the loop would run to MAX_PAGES (hammering the
        # server into 429s). Parsed card rows are the true end-of-set signal.
        cards = parse_tcdb_html(html)
        if not cards:
            break

        # Duplicate-page guard: for some sets (e.g. slugs containing '&'/parens)
        # and under bulk load, TCDB ignores an out-of-range PageIndex and re-serves
        # page 1's rows instead of the empty/redirect stop page — so `cards` is
        # non-empty on every page and the loop runs to MAX_PAGES, writing dozens of
        # identical pages that later inflate the DB (each row is inserted, not
        # deduped). A real next page always introduces new card numbers; a page
        # that introduces none is a repeat → end of set.
        page_numbers = {c.card_number for c in cards if getattr(c, "card_number", None)}
        if page > 1 and page_numbers and page_numbers <= seen_numbers:
            break
        seen_numbers |= page_numbers

        path = job.page_path(page)
        path.write_text(html, encoding="utf-8")
        job.pages.append(path)
        total_cards += len(cards)
        if page < MAX_PAGES:
            time.sleep(page_delay)

    if not job.pages:
        return "empty", "no card rows found"
    return "ok", f"{len(job.pages)} page(s), ~{total_cards} cards"


# --------------------------------------------------------------------------- #
# Status + batch orchestration
# --------------------------------------------------------------------------- #
def _load_status() -> dict:
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    return {}


def _save_status(status: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")


def _run(jobs: list[DownloadJob], skip_existing: bool, min_delay: float, max_delay: float) -> None:
    status = _load_status()
    for i, job in enumerate(jobs, 1):
        key = str(job.sid)
        prev = status.get(key, {})
        if skip_existing and prev.get("status") == "ok" and job.pages_exist():
            print(f"[{i}/{len(jobs)}] sid={job.sid} already downloaded, skipping")
            continue

        result, message = download_set(job)
        entry = {
            "sid": job.sid,
            "sport": job.sport,
            "year": job.year,
            "set_name": job.set_name,
            "status": result,
            "message": message,
        }
        if job.slug:
            entry["slug"] = job.slug
            entry["checklist_url"] = CHECKLIST_URL.format(sid=job.sid, slug=job.slug)
        if job.pages:
            entry["pages"] = len(job.pages)
        status[key] = entry
        _save_status(status)
        print(f"[{i}/{len(jobs)}] sid={job.sid} → {result}: {message}")

        if i < len(jobs):
            time.sleep(random.uniform(min_delay, max_delay))


def _jobs_from_index() -> list[DownloadJob]:
    if not INDEX_PATH.exists():
        print(f"No discovery index at {INDEX_PATH}. Run tcdb_discover first.", file=sys.stderr)
        sys.exit(2)
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    jobs = []
    for e in raw:
        jobs.append(DownloadJob(
            sid=e["sid"], sport=e["sport"], year=e["year"],
            set_name=e["set_name"], slug=e.get("slug"),
        ))
    return jobs


def main() -> None:
    p = argparse.ArgumentParser(description="Download TCDB checklist HTML pages over HTTP.")
    p.add_argument("--sid", type=int)
    p.add_argument("--sport")
    p.add_argument("--year", type=int)
    p.add_argument("--set-name")
    p.add_argument("--slug", help="optional; auto-resolved from the sid if omitted")
    p.add_argument("--from-index", action="store_true")
    p.add_argument("--no-skip", action="store_true", help="re-download even if already done")
    p.add_argument("--min-delay", type=float, default=2.0)
    p.add_argument("--max-delay", type=float, default=4.0)
    args = p.parse_args()

    if args.from_index:
        jobs = _jobs_from_index()
    elif args.sid and args.sport and args.year and args.set_name:
        jobs = [DownloadJob(sid=args.sid, sport=args.sport, year=args.year,
                            set_name=args.set_name, slug=args.slug)]
    else:
        p.error("Provide either --from-index OR (--sid + --sport + --year + --set-name)")

    _run(jobs, skip_existing=not args.no_skip, min_delay=args.min_delay, max_delay=args.max_delay)


if __name__ == "__main__":
    main()
