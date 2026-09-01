"""Normalization helpers for player names, parallels, and print runs.

Single source of truth used by all parsers. Keeping this small + pure so it can
be unit-tested without touching scrapers or the DB.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Suffixes we strip when generating the normalized lookup key (keep them on the
# display name though — "LeBron James Jr." is what we want to show users).
_SUFFIX_PATTERN = re.compile(
    r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$",
    flags=re.IGNORECASE,
)

# Common "card-only" tokens that show up in TCDB player columns and aren't part
# of the player's actual name. e.g. "Patrick Mahomes II AU /99"
_NOISE_PATTERN = re.compile(
    r"\s+(AU|RC|SP|SSP|RPA|JSY|/\d+).*$",
)

# Patterns inside parallel-name strings that tell us serial numbering / print run.
# TCDB writes serials two ways: "/99" and its own "SN99" convention (e.g.
# "SN250 Silver", "AU, SN1 Black"). Match either; digits must follow immediately.
_PRINT_RUN_PATTERN = re.compile(r"(?:/\s*|\bSN)(\d{1,5})\b", re.IGNORECASE)

# Quick membership tests for parallel-text flags.
_AUTO_TOKENS = ("auto", "autograph", "signature")
_RELIC_TOKENS = ("relic", "patch", "jersey", "mem", "memorabilia")
_ROOKIE_TOKENS = ("rookie", " rc ", " rc.", " rc/", " rc-")


def normalize_player_name(name: str) -> str:
    """Lowercase, strip accents + suffixes + card-only noise.

    Used as the lookup key for the players table — two raw names that
    normalize identically are merged into one player row.
    """
    if not name:
        return ""
    # Strip accents
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    # Strip card-only tokens
    name = _NOISE_PATTERN.sub("", name)
    # Strip suffix
    name = _SUFFIX_PATTERN.sub("", name)
    # Drop periods so initials collapse ("C.J." == "CJ", "St." == "St") — this is
    # the lookup KEY only; clean_display_name keeps the periods for display.
    name = name.replace(".", "")
    # Collapse whitespace, lowercase
    return re.sub(r"\s+", " ", name).strip().lower()


def clean_display_name(name: str) -> str:
    """Strip card-only noise but keep capitalization + suffixes for display."""
    if not name:
        return ""
    name = _NOISE_PATTERN.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


def extract_print_run(text: str) -> int | None:
    if not text:
        return None
    m = _PRINT_RUN_PATTERN.search(text)
    return int(m.group(1)) if m else None


@dataclass
class ParallelFlags:
    is_auto: bool
    is_relic: bool
    is_rookie: bool


_AUTO_ABBREVIATION_PATTERN = re.compile(r"\bau\b", re.IGNORECASE)

def detect_flags(*texts: str) -> ParallelFlags:
    """Look across several text fields (parallel name, notes, card number suffix)
    and return boolean flags for auto / relic / rookie."""
    haystack = " ".join((t or "").lower() for t in texts)
    haystack = f" {haystack} "  # pad for ' rc ' word-boundary checks
    return ParallelFlags(
        is_auto=any(t in haystack for t in _AUTO_TOKENS) or bool(_AUTO_ABBREVIATION_PATTERN.search(haystack)),
        is_relic=any(t in haystack for t in _RELIC_TOKENS),
        is_rookie=any(t in haystack for t in _ROOKIE_TOKENS),
    )
