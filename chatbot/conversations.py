"""Persistent chat-conversation store.

Each conversation is a single JSON file in a local ``conversations/`` directory
(gitignored — chat logs are personal and machine-local). This lets saved chats
survive Streamlit reruns *and* full server restarts, which ``st.session_state``
alone does not.

A conversation file looks like::

    {
      "id": "a1b2c3d4e5f6",
      "title": "Who has more cards, Mahomes or Allen?",
      "created": 1726000000.0,
      "updated": 1726000123.0,
      "messages": [ ...the same message dicts app.py keeps in session_state... ]
    }

The message dicts are already JSON-serializable (app.py converts Anthropic
content blocks to plain dicts, and tool_result content is a JSON string), so we
persist them verbatim and replay them on load.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

CONV_DIR = Path(__file__).resolve().parents[1] / "conversations"
_TITLE_MAX = 60


def _ensure_dir() -> None:
    CONV_DIR.mkdir(exist_ok=True)


def _path(cid: str) -> Path:
    return CONV_DIR / f"{cid}.json"


def new_id() -> str:
    """A short, filename-safe conversation id."""
    return uuid.uuid4().hex[:12]


def title_from_messages(messages: list[dict], default: str = "New chat") -> str:
    """Derive a readable title from the first user question."""
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            t = " ".join(m["content"].split()).strip()
            if not t:
                continue
            return (t[: _TITLE_MAX - 1] + "…") if len(t) > _TITLE_MAX else t
    return default


def save(cid: str, title: str, messages: list[dict]) -> None:
    """Write (or overwrite) a conversation, preserving its ``created`` stamp.

    A conversation with no messages is not written (and any existing file for it
    is removed), so empty/aborted chats never clutter the list.
    """
    _ensure_dir()
    p = _path(cid)
    if not messages:
        p.unlink(missing_ok=True)
        return
    now = time.time()
    created = now
    if p.exists():
        try:
            created = json.loads(p.read_text("utf-8")).get("created", now)
        except (ValueError, OSError):
            created = now
    payload = {
        "id": cid,
        "title": title or "Untitled",
        "created": created,
        "updated": now,
        "messages": messages,
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(p)  # atomic-ish: never leave a half-written file at the real path


def load(cid: str) -> dict | None:
    """Return the full conversation dict, or None if missing/unreadable."""
    try:
        return json.loads(_path(cid).read_text("utf-8"))
    except (ValueError, OSError):
        return None


def delete(cid: str) -> None:
    _path(cid).unlink(missing_ok=True)


def list_all() -> list[dict]:
    """All saved conversations as light summaries, newest-updated first.

    Each item: ``{id, title, updated, created, turns}`` where ``turns`` counts
    user questions (not raw messages, so tool round-trips don't inflate it).
    """
    _ensure_dir()
    out: list[dict] = []
    for p in CONV_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        msgs = d.get("messages", [])
        turns = sum(
            1 for m in msgs
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        out.append({
            "id": d.get("id", p.stem),
            "title": d.get("title") or "Untitled",
            "updated": d.get("updated", 0),
            "created": d.get("created", 0),
            "turns": turns,
        })
    return sorted(out, key=lambda x: x["updated"], reverse=True)
