"""Ingest user-supplied files dropped into data/manual/.

Layout expected:
    data/manual/{sport}/{year}/2025_Prizm_Football.xlsx
    data/manual/{sport}/{year}/2025_Prizm_Football.xlsx.meta.json

Sidecar JSON:
    {"brand": "Prizm", "sport": "football", "year": 2025,
     "set_name": "2025 Prizm Football"}

For Phase 0 the XLSX/PDF parsers are stubs — this module just walks the tree,
prints what it found, and exits. Wire up parsers/panini_xlsx.py and
parsers/panini_pdf.py in Phase 2 when you actually need them.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL_DIR = ROOT / "data" / "manual"


def main() -> None:
    if not MANUAL_DIR.exists():
        print(f"No manual directory at {MANUAL_DIR}; nothing to ingest.")
        return
    found = []
    for meta in MANUAL_DIR.rglob("*.meta.json"):
        data_file = meta.with_name(meta.name.replace(".meta.json", ""))
        if not data_file.exists():
            print(f"  ! sidecar without data file: {meta}")
            continue
        meta_obj = json.loads(meta.read_text(encoding="utf-8"))
        found.append((data_file, meta_obj))
        print(f"  + {data_file.name}: {meta_obj}")

    if not found:
        print("No manual files found.")
        return
    print(f"\nFound {len(found)} manual file(s).")
    print("XLSX/PDF parsing for Panini fallback is Phase 2 — not implemented yet.")


if __name__ == "__main__":
    main()
