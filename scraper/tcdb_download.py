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
from dataclasses import dataclass, field
from pathlib import Path

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

_PERSON_RE = re.compile(r"/Person\.cfm/pid/\d+", re.IGNORECASE)
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


def _is_404(final_url: str, html: str) -> bool:
    return _404_MARKER in final_url or "404 - Page Not Found" in html


def _person_count(html: str) -> int:
    return len(_PERSON_RE.findall(html))


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


def download_set(job: DownloadJob, page_delay: float = 1.5) -> tuple[str, str]:
    """Download every checklist page for one set. Returns (status, message)."""
    slug = job.slug or resolve_slug(job.sid)
    if not slug:
        return "failed", "could not resolve checklist slug"
    job.slug = slug

    job.out_dir().mkdir(parents=True, exist_ok=True)
    _clear_old_pages(job)

    total_cards = 0
    for page in range(1, MAX_PAGES + 1):
        url = CHECKLIST_URL.format(sid=job.sid, slug=slug) + f"?PageIndex={page}"
        try:
            final_url, html = _get(url)
        except urllib.error.HTTPError as e:
            return "failed", f"HTTP {e.code} on page {page}"
        except Exception as e:  # noqa: BLE001
            return "failed", f"{type(e).__name__} on page {page}: {e}"

        if _is_404(final_url, html):
            if page == 1:
                return "failed", "checklist 404 (bad slug/sid)"
            break  # past the last page
        n = _person_count(html)
        if n == 0:
            break  # past the last page (200 but empty)

        path = job.page_path(page)
        path.write_text(html, encoding="utf-8")
        job.pages.append(path)
        total_cards += n
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
