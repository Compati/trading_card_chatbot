"""SQL-backed tools Claude calls from the Streamlit chat app.

Two layers:
    - Python implementations (search_player, cards_for_player, ...) that take
      kwargs and return JSON-serializable dicts.
    - TOOL_SCHEMAS — Anthropic tool-use definitions describing the same
      functions to Claude.

The app dispatches each tool_use block by name to the matching function.
"""
from __future__ import annotations

import difflib
import json
import sqlite3
import sys
from typing import Any

from db.connection import get_connection
from parsers.normalize import normalize_player_name


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# Minimum similarity (0-1) for a fuzzy player match to be offered. Tuned so
# realistic typos ("Mahoms", "Wembanyma") match but unrelated strings don't.
_FUZZY_CUTOFF = 0.72


def _fuzzy_score(query_norm: str, full_norm: str) -> float:
    """Best difflib similarity between the (normalized) query and a stored name.

    Takes the max of the whole-string ratio and every query-token vs name-token
    ratio, so a last-name-only typo ("mahoms") still scores against the matching
    token ("mahomes") even though the full stored name is "patrick mahomes".
    """
    best = difflib.SequenceMatcher(None, query_norm, full_norm).ratio()
    q_tokens = query_norm.split()
    n_tokens = full_norm.split()
    for qt in q_tokens:
        for nt in n_tokens:
            r = difflib.SequenceMatcher(None, qt, nt).ratio()
            if r > best:
                best = r
    return best


def _fuzzy_player_search(conn, name: str, sport: str | None, limit: int) -> list[dict]:
    """Last-resort typo-tolerant match: score every player's normalized name and
    keep those at/above the cutoff. Only called when FTS + LIKE both miss."""
    query_norm = normalize_player_name(name)
    if not query_norm:
        return []
    sql = (
        "SELECT p.id, p.full_name, p.normalized_name, s.name AS sport, "
        "       (SELECT COUNT(*) FROM cards c WHERE c.player_id = p.id) AS card_count "
        "FROM players p JOIN sports s ON s.id = p.sport_id"
    )
    params: list[Any] = []
    if sport:
        sql += " WHERE s.name = ?"
        params.append(sport)
    scored: list[tuple[float, dict]] = []
    for row in conn.execute(sql, tuple(params)):
        d = dict(row)
        norm = d.pop("normalized_name") or ""
        score = _fuzzy_score(query_norm, norm)
        if score >= _FUZZY_CUTOFF:
            scored.append((score, d))
    # Best similarity first, then most cards as a tiebreak.
    scored.sort(key=lambda t: (round(t[0], 3), t[1]["card_count"]), reverse=True)
    return [d for _s, d in scored[:limit]]


# ---------- Tool implementations ----------

def search_player(name: str, sport: str | None = None, limit: int = 10) -> dict:
    """Find player(s) by name. Uses FTS for tolerance; falls back to LIKE on the
    normalized name if FTS returns nothing (e.g., single-token queries)."""
    conn = get_connection(read_only=True)
    try:
        fts_query = " ".join(f"{tok}*" for tok in name.split() if tok)
        sql = (
            "SELECT p.id, p.full_name, s.name AS sport, "
            "       (SELECT COUNT(*) FROM cards c WHERE c.player_id = p.id) AS card_count "
            "FROM players_fts f JOIN players p ON p.id = f.rowid "
            "JOIN sports s ON s.id = p.sport_id "
            "WHERE players_fts MATCH ? "
        )
        params: list[Any] = [fts_query]
        if sport:
            sql += " AND s.name = ?"
            params.append(sport)
        sql += " ORDER BY card_count DESC LIMIT ?"
        params.append(limit)
        try:
            rows = _rows(conn, sql, tuple(params))
        except sqlite3.OperationalError:
            rows = []

        if not rows:
            like = f"%{normalize_player_name(name)}%"
            sql2 = (
                "SELECT p.id, p.full_name, s.name AS sport, "
                "       (SELECT COUNT(*) FROM cards c WHERE c.player_id = p.id) AS card_count "
                "FROM players p JOIN sports s ON s.id = p.sport_id "
                "WHERE p.normalized_name LIKE ?"
            )
            params2: list[Any] = [like]
            if sport:
                sql2 += " AND s.name = ?"
                params2.append(sport)
            sql2 += " ORDER BY card_count DESC LIMIT ?"
            params2.append(limit)
            rows = _rows(conn, sql2, tuple(params2))

        # Tier 3: typo-tolerant fuzzy match. FTS is prefix-tolerant but not
        # typo-tolerant ("Mahoms" misses "Mahomes"), and LIKE needs a contiguous
        # substring. Only reached when both exact paths return nothing.
        if not rows:
            fuzzy = _fuzzy_player_search(conn, name, sport, limit)
            if fuzzy:
                return {"matches": fuzzy, "query": name, "sport": sport, "fuzzy": True}

        return {"matches": rows, "query": name, "sport": sport}
    finally:
        conn.close()


def cards_for_player(player_id: int, year: int | None = None, brand: str | None = None,
                     set_name: str | None = None, parallel_contains: str | None = None,
                     is_auto: bool | None = None, is_relic: bool | None = None,
                     is_rookie: bool | None = None, limit: int = 200) -> dict:
    conn = get_connection(read_only=True)
    try:
        sql = (
            "SELECT c.id, c.card_number, c.parallel_name, c.print_run, "
            "       c.is_auto, c.is_relic, c.is_rookie, "
            "       s.year, s.name AS set_name, b.name AS brand, sp.name AS sport, "
            "       t.name AS team "
            "FROM cards c "
            "JOIN sets s ON s.id = c.set_id "
            "JOIN brands b ON b.id = s.brand_id "
            "JOIN sports sp ON sp.id = s.sport_id "
            "LEFT JOIN teams t ON t.id = c.team_id "
            "WHERE c.player_id = ?"
        )
        params: list[Any] = [player_id]
        if year:
            sql += " AND s.year = ?"
            params.append(year)
        if brand:
            sql += " AND b.name = ?"
            params.append(brand)
        if set_name:
            sql += " AND s.name LIKE ?"
            params.append(f"%{set_name}%")
        if parallel_contains:
            sql += " AND COALESCE(c.parallel_name, '') LIKE ?"
            params.append(f"%{parallel_contains}%")
        if is_auto is not None:
            sql += " AND c.is_auto = ?"
            params.append(int(is_auto))
        if is_relic is not None:
            sql += " AND c.is_relic = ?"
            params.append(int(is_relic))
        if is_rookie is not None:
            sql += " AND c.is_rookie = ?"
            params.append(int(is_rookie))
        sql += " ORDER BY s.year DESC, b.name, s.name, c.card_number LIMIT ?"
        params.append(limit)
        rows = _rows(conn, sql, tuple(params))
        return {"player_id": player_id, "count_returned": len(rows), "cards": rows}
    finally:
        conn.close()


def sets_for_player(player_id: int, year: int | None = None) -> dict:
    conn = get_connection(read_only=True)
    try:
        sql = (
            "SELECT s.id AS set_id, s.year, s.name AS set_name, b.name AS brand, "
            "       sp.name AS sport, COUNT(c.id) AS cards_in_set "
            "FROM cards c "
            "JOIN sets s ON s.id = c.set_id "
            "JOIN brands b ON b.id = s.brand_id "
            "JOIN sports sp ON sp.id = s.sport_id "
            "WHERE c.player_id = ?"
        )
        params: list[Any] = [player_id]
        if year:
            sql += " AND s.year = ?"
            params.append(year)
        sql += " GROUP BY s.id ORDER BY s.year DESC, b.name, s.name"
        rows = _rows(conn, sql, tuple(params))
        return {"player_id": player_id, "set_count": len(rows), "sets": rows}
    finally:
        conn.close()


def count_cards(player_id: int, year: int | None = None, brand: str | None = None,
                is_auto: bool | None = None, is_relic: bool | None = None,
                is_rookie: bool | None = None) -> dict:
    conn = get_connection(read_only=True)
    try:
        where = ["c.player_id = ?"]
        params: list[Any] = [player_id]
        if year:
            where.append("s.year = ?")
            params.append(year)
        if brand:
            where.append("b.name = ?")
            params.append(brand)
        if is_auto is not None:
            where.append("c.is_auto = ?")
            params.append(int(is_auto))
        if is_relic is not None:
            where.append("c.is_relic = ?")
            params.append(int(is_relic))
        if is_rookie is not None:
            where.append("c.is_rookie = ?")
            params.append(int(is_rookie))
        where_sql = " AND ".join(where)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM cards c JOIN sets s ON s.id=c.set_id "
            f"JOIN brands b ON b.id=s.brand_id WHERE {where_sql}",
            tuple(params),
        ).fetchone()["n"]
        by_year = _rows(
            conn,
            f"SELECT s.year, COUNT(*) AS n FROM cards c JOIN sets s ON s.id=c.set_id "
            f"JOIN brands b ON b.id=s.brand_id WHERE {where_sql} GROUP BY s.year ORDER BY s.year DESC",
            tuple(params),
        )
        by_brand = _rows(
            conn,
            f"SELECT b.name AS brand, COUNT(*) AS n FROM cards c JOIN sets s ON s.id=c.set_id "
            f"JOIN brands b ON b.id=s.brand_id WHERE {where_sql} GROUP BY b.name ORDER BY n DESC",
            tuple(params),
        )
        return {"player_id": player_id, "total": total, "by_year": by_year, "by_brand": by_brand}
    finally:
        conn.close()


def search_sets(query: str | None = None, year: int | None = None, sport: str | None = None,
                brand: str | None = None, limit: int = 50) -> dict:
    conn = get_connection(read_only=True)
    try:
        where = ["1=1"]
        params: list[Any] = []
        if query:
            where.append("s.name LIKE ?")
            params.append(f"%{query}%")
        if year:
            where.append("s.year = ?")
            params.append(year)
        if sport:
            where.append("sp.name = ?")
            params.append(sport)
        if brand:
            where.append("b.name = ?")
            params.append(brand)
        where_sql = " AND ".join(where)
        sql = (
            "SELECT s.id AS set_id, s.year, s.name AS set_name, b.name AS brand, sp.name AS sport, "
            "       (SELECT COUNT(*) FROM cards c WHERE c.set_id = s.id) AS card_count "
            f"FROM sets s JOIN brands b ON b.id=s.brand_id JOIN sports sp ON sp.id=s.sport_id "
            f"WHERE {where_sql} ORDER BY s.year DESC, b.name, s.name LIMIT ?"
        )
        params.append(limit)
        rows = _rows(conn, sql, tuple(params))
        return {"count": len(rows), "sets": rows}
    finally:
        conn.close()


def set_details(set_id: int, sample_players: int = 15) -> dict:
    conn = get_connection(read_only=True)
    try:
        head = conn.execute(
            "SELECT s.id, s.year, s.name, b.name AS brand, sp.name AS sport, "
            "       s.source, s.source_url, s.ingested_at, "
            "       (SELECT COUNT(*) FROM cards c WHERE c.set_id = s.id) AS card_count, "
            "       (SELECT COUNT(DISTINCT player_id) FROM cards c WHERE c.set_id = s.id) AS player_count "
            "FROM sets s JOIN brands b ON b.id=s.brand_id JOIN sports sp ON sp.id=s.sport_id "
            "WHERE s.id = ?",
            (set_id,),
        ).fetchone()
        if not head:
            return {"error": f"set_id {set_id} not found"}
        players = _rows(
            conn,
            "SELECT DISTINCT p.id, p.full_name FROM cards c JOIN players p ON p.id=c.player_id "
            "WHERE c.set_id = ? ORDER BY p.full_name LIMIT ?",
            (set_id, sample_players),
        )
        return {"set": dict(head), "sample_players": players}
    finally:
        conn.close()


def db_stats() -> dict:
    conn = get_connection(read_only=True)
    try:
        def one(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "sports": one("SELECT COUNT(*) FROM sports"),
            "brands": one("SELECT COUNT(*) FROM brands"),
            "sets": one("SELECT COUNT(*) FROM sets"),
            "cards": one("SELECT COUNT(*) FROM cards"),
            "players": one("SELECT COUNT(*) FROM players"),
            "year_min": one("SELECT COALESCE(MIN(year), 0) FROM sets"),
            "year_max": one("SELECT COALESCE(MAX(year), 0) FROM sets"),
            "sports_present": [r[0] for r in conn.execute(
                "SELECT DISTINCT sp.name FROM sports sp JOIN sets s ON s.sport_id=sp.id "
                "ORDER BY sp.name").fetchall()],
        }
    finally:
        conn.close()


# ---------- Anthropic tool-use schemas ----------

TOOL_SCHEMAS = [
    {
        "name": "db_stats",
        "description": (
            "Return summary statistics for the database (total sports, brands, sets, cards, "
            "players, and the year range present). Call this FIRST when the user asks a "
            "question whose scope might exceed what's been ingested, so you can tell them "
            "honestly what data you have."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_player",
        "description": (
            "Find players by name. Returns the player_id you'll pass to other tools. "
            "Tolerant of case, partial names, and minor misspellings. "
            "If multiple players match, ask the user which one they meant or pick the one "
            "with the most cards. If the result has \"fuzzy\": true, the name was matched "
            "approximately (the exact spelling wasn't found) — confirm with the user "
            "(e.g. 'Did you mean Patrick Mahomes?') before relying on it. "
            "Use this BEFORE cards_for_player / sets_for_player / count_cards."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Player name (any case, partial OK)"},
                "sport": {"type": "string", "description": "Optional sport filter (football, basketball, ...)"},
                "limit": {"type": "integer", "description": "Max matches to return (default 10)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "cards_for_player",
        "description": (
            "List individual cards for one player. Supports year/brand/set/parallel and autograph/relic/rookie filters. "
            "Use this when the user wants specific cards, not aggregate counts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer"},
                "year": {"type": "integer"},
                "brand": {"type": "string"},
                "set_name": {"type": "string"},
                "parallel_contains": {"type": "string"},
                "is_auto": {"type": "boolean", "description": "Filter to autograph cards when true"},
                "is_relic": {"type": "boolean", "description": "Filter to relic/patch cards when true"},
                "is_rookie": {"type": "boolean", "description": "Filter to rookie cards when true"},
                "limit": {"type": "integer", "description": "Max cards to return (default 200)"},
            },
            "required": ["player_id"],
        },
    },
    {
        "name": "sets_for_player",
        "description": (
            "List DISTINCT sets a player appears in, with per-set card counts. "
            "Use this for 'what sets is X in' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer"},
                "year": {"type": "integer"},
            },
            "required": ["player_id"],
        },
    },
    {
        "name": "count_cards",
        "description": (
            "Return total card count for a player plus breakdowns by year and by brand. Supports autograph/relic/rookie filters. "
            "Use this for 'how many cards does X have' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer"},
                "year": {"type": "integer"},
                "brand": {"type": "string"},
                "is_auto": {"type": "boolean", "description": "Filter to autograph cards when true"},
                "is_relic": {"type": "boolean", "description": "Filter to relic/patch cards when true"},
                "is_rookie": {"type": "boolean", "description": "Filter to rookie cards when true"},
            },
            "required": ["player_id"],
        },
    },
    {
        "name": "search_sets",
        "description": "Find sets by partial name / year / sport / brand. Returns set_id you can pass to set_details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "year": {"type": "integer"},
                "sport": {"type": "string"},
                "brand": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "set_details",
        "description": "Return metadata + total card count + sample players for one set.",
        "input_schema": {
            "type": "object",
            "properties": {
                "set_id": {"type": "integer"},
                "sample_players": {"type": "integer", "description": "How many sample player names to return (default 15)"},
            },
            "required": ["set_id"],
        },
    },
]


TOOLS = {
    "db_stats": db_stats,
    "search_player": search_player,
    "cards_for_player": cards_for_player,
    "sets_for_player": sets_for_player,
    "count_cards": count_cards,
    "search_sets": search_sets,
    "set_details": set_details,
}


def dispatch(name: str, arguments: dict) -> dict:
    """Call the implementation for `name`. Returns a JSON-serializable dict."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return fn(**arguments)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# CLI for quick standalone checks: `python -m chatbot.tools search_player "Patrick Mahomes"`
def _cli() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m chatbot.tools <tool_name> [json_args]")
        print("       python -m chatbot.tools search_player '{\"name\":\"Patrick Mahomes\"}'")
        print("       python -m chatbot.tools db_stats")
        print("\nAvailable tools:", ", ".join(TOOLS))
        sys.exit(1)
    name = sys.argv[1]
    if name not in TOOLS:
        print(f"Unknown tool: {name}. Try one of {list(TOOLS)}")
        sys.exit(2)
    args: dict
    if len(sys.argv) > 2:
        raw = sys.argv[2]
        # Allow either JSON or "key=value" shorthand for the common single-arg case.
        if raw.startswith("{"):
            args = json.loads(raw)
        elif "=" in raw:
            k, v = raw.split("=", 1)
            args = {k: v}
        else:
            args = {"name": raw}  # convenient for search_player
    else:
        args = {}
    print(json.dumps(dispatch(name, args), indent=2, default=str))


if __name__ == "__main__":
    _cli()
