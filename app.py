"""Trading Card Chatbot — Streamlit chat over the local Panini card DB.

Claude answers questions by calling SQL-backed tools defined in chatbot/tools.py.
No RAG / embeddings — every answer is grounded in deterministic SQL.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic
import streamlit as st
from dotenv import load_dotenv

# Make sibling packages importable when streamlit runs us from the project root.
sys.path.insert(0, str(Path(__file__).parent))

from chatbot.system_prompt import SYSTEM_PROMPT
from chatbot.tools import TOOL_SCHEMAS, dispatch, db_stats
from db.connection import DB_PATH, init_db

load_dotenv()

MODELS = {
    "Haiku 4.5 — fastest & cheapest": "claude-haiku-4-5",
    "Sonnet 4.6 — balanced": "claude-sonnet-4-6",
    "Opus 4.7 — most capable": "claude-opus-4-7",
}

MAX_TOOL_ITERATIONS = 8  # safety guard against tool-call loops

# ─── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Trading Card Chatbot", page_icon="🃏", layout="wide")
st.title("🃏 Trading Card Chatbot")
st.caption("Ask about Panini cards, players, and sets. Answers come from a local SQLite database.")

# ─── DB bootstrap ─────────────────────────────────────────────────────────────
if not DB_PATH.exists():
    st.warning(f"No database at `{DB_PATH}`. Initializing an empty one — "
               f"run the scraper + ingest to populate it (see README).")
    init_db()

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")

    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        st.success("API key loaded from .env")
        api_key = env_key
    else:
        api_key = st.text_input(
            "Anthropic API key",
            type="password",
            value=st.session_state.get("api_key", ""),
            help="Get one at console.anthropic.com",
        )
        if api_key:
            st.session_state["api_key"] = api_key

    model_label = st.selectbox("Model", list(MODELS), index=0)
    model_id = MODELS[model_label]

    st.divider()
    st.subheader("Database")
    try:
        stats = db_stats()
        c1, c2 = st.columns(2)
        c1.metric("Cards", f"{stats['cards']:,}")
        c2.metric("Players", f"{stats['players']:,}")
        c1.metric("Sets", f"{stats['sets']:,}")
        c2.metric("Sports", f"{stats['sports']:,}")
        if stats["year_max"]:
            st.caption(f"Years loaded: {stats['year_min']}–{stats['year_max']}")
        if stats["sports_present"]:
            st.caption("Sports: " + ", ".join(stats["sports_present"]))
    except Exception as e:
        st.error(f"DB stats unavailable: {e}")

    st.divider()
    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()

# ─── Chat state ───────────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []  # list[ {"role": "user"|"assistant", "content": str|list} ]

# ─── Replay history ───────────────────────────────────────────────────────────
def _render_message(msg: dict) -> None:
    """Render one message. Assistant messages may be a list of content blocks
    (text + tool_use); we show the text and put tool calls in an expander."""
    role = msg["role"]
    with st.chat_message("user" if role == "user" else "assistant"):
        content = msg["content"]
        if isinstance(content, str):
            st.markdown(content)
            return
        # List of content blocks (Anthropic format)
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text":
                text = block["text"] if isinstance(block, dict) else block.text
                if text:
                    st.markdown(text)
            elif btype == "tool_use":
                name = block["name"] if isinstance(block, dict) else block.name
                inp = block["input"] if isinstance(block, dict) else block.input
                with st.expander(f"🔧 Tool call: `{name}`", expanded=False):
                    st.code(json.dumps(inp, indent=2), language="json")
            elif btype == "tool_result":
                # Tool results are shown in the user-role message that carries them;
                # included here only for completeness.
                tid = block.get("tool_use_id") or ""
                with st.expander(f"📦 Tool result ({tid[:8]}…)", expanded=False):
                    st.code(str(block.get("content", ""))[:5000], language="json")


for msg in st.session_state.messages:
    _render_message(msg)

# ─── Input ────────────────────────────────────────────────────────────────────
user_input = st.chat_input("Ask about a player, a set, a year…")
if user_input:
    if not api_key:
        st.error("Add your Anthropic API key in the sidebar first.")
        st.stop()

    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    client = anthropic.Anthropic(api_key=api_key)

    # Build the API conversation history. We carry assistant messages as their
    # full content-block list so Claude sees its prior tool_use blocks.
    def _to_api(msg: dict) -> dict:
        return {"role": msg["role"], "content": msg["content"]}

    api_messages = [_to_api(m) for m in st.session_state.messages]

    with st.chat_message("assistant"):
        status = st.status("Thinking…", expanded=False)
        try:
            for iteration in range(MAX_TOOL_ITERATIONS):
                status.update(label=f"Calling Claude (iteration {iteration + 1})…")
                resp = client.messages.create(
                    model=model_id,
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_SCHEMAS,
                    messages=api_messages,
                )

                # Convert content blocks to serializable dicts so we can stash
                # them in session_state and re-send next turn.
                assistant_content = []
                tool_uses = []
                for block in resp.content:
                    if block.type == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        tu = {"type": "tool_use", "id": block.id,
                              "name": block.name, "input": block.input}
                        assistant_content.append(tu)
                        tool_uses.append(tu)

                st.session_state.messages.append({"role": "assistant", "content": assistant_content})
                api_messages.append({"role": "assistant", "content": assistant_content})

                # Render whatever text + tool_use blocks just landed
                for block in assistant_content:
                    if block["type"] == "text" and block["text"]:
                        st.markdown(block["text"])
                    elif block["type"] == "tool_use":
                        with st.expander(f"🔧 Tool call: `{block['name']}`", expanded=False):
                            st.code(json.dumps(block["input"], indent=2), language="json")

                if resp.stop_reason != "tool_use" or not tool_uses:
                    status.update(label="Done", state="complete")
                    break

                # Execute tools and feed results back in a user-role message.
                status.update(label=f"Running {len(tool_uses)} tool(s)…")
                tool_results = []
                for tu in tool_uses:
                    result = dispatch(tu["name"], tu["input"] or {})
                    serialized = json.dumps(result, default=str)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": serialized,
                    })
                    with st.expander(f"📦 Result: `{tu['name']}`", expanded=False):
                        st.code(serialized[:8000], language="json")

                user_followup = {"role": "user", "content": tool_results}
                st.session_state.messages.append(user_followup)
                api_messages.append(user_followup)
            else:
                status.update(label="Hit max tool iterations", state="error")
                st.warning(f"Stopped after {MAX_TOOL_ITERATIONS} tool iterations. "
                           "Try rephrasing or break the question down.")
        except anthropic.APIError as e:
            status.update(label="API error", state="error")
            st.error(f"Anthropic API error: {e}")
        except Exception as e:
            status.update(label="Error", state="error")
            st.error(f"{type(e).__name__}: {e}")
