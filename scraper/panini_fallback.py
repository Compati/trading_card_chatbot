"""Panini.com fallback scraper for sets TCDB doesn't have yet.

Placeholder for Phase 2+ — drives a Playwright browser to paniniamerica.net,
finds the downloadable XLSX/PDF for a specific set, and saves it under
data/raw/panini/{sport}/{year}/.

Out of scope for Phase 0. For the MVP, gaps go through data/manual/ instead.
"""
from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "panini_fallback is Phase 2+. Use data/manual/ + ingest.manual for now."
    )


if __name__ == "__main__":
    main()
