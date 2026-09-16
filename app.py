"""Trading Card Chatbot — Streamlit chat over the local Panini card DB.

Claude answers questions by calling SQL-backed tools defined in chatbot/tools.py.
No RAG / embeddings — every answer is grounded in deterministic SQL.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Make sibling packages importable when streamlit runs us from the project root.
sys.path.insert(0, str(Path(__file__).parent))

from chatbot import conversations
from chatbot.system_prompt import SYSTEM_PROMPT
from chatbot.tools import TOOL_SCHEMAS, dispatch, db_stats
from db.connection import DB_PATH, init_db

load_dotenv()

MODELS = {
    "Haiku 4.5 — fastest & cheapest": "claude-haiku-4-5",
    "Sonnet 5 — balanced": "claude-sonnet-5",
    "Opus 5 — most capable": "claude-opus-5",
}

MAX_TOOL_ITERATIONS = 8  # safety guard against tool-call loops

EXAMPLE_QUESTIONS = [
    "Who has more cards, Patrick Mahomes or Josh Allen?",
    "Show Brock Purdy's cards year by year",
    "Who has the most autographs in 2022 Chronicles Draft Picks?",
    "What autos does Victor Wembanyama have?",
]


# ─── Tool-result rendering (tables + charts instead of raw JSON) ───────────────
def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _table(rows: list[dict], cols: list[str] | None = None) -> None:
    df = _df(rows)
    if df.empty:
        st.caption("_no rows_")
        return
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    st.dataframe(df, hide_index=True, width="stretch")


def render_tool_result(name: str, result: dict) -> None:
    """Render a tool result as tables/charts by tool type; raw JSON stays available."""
    if not isinstance(result, dict) or result.get("error"):
        st.error(result.get("error") if isinstance(result, dict) else str(result))
        return

    if name == "player_timeline":
        st.caption(f"**{result.get('name')}** — {result.get('total', 0):,} cards total")
        tl = result.get("timeline", [])
        if tl:
            st.bar_chart(_df(tl), x="year", y="cards", height=200)
            _table(tl, ["year", "cards", "autos", "relics", "rookies", "sets"])

    elif name == "compare_players":
        comp = result.get("comparison", [])
        _table(comp, ["name", "total", "autos", "relics", "rookies", "distinct_sets"])
        if comp:
            st.bar_chart(_df(comp), x="name", y="total", height=200)

    elif name == "product_leaders":
        st.caption(f"Top players by **{result.get('metric')}** in *{result.get('product')}*")
        _table(result.get("leaders", []), ["full_name", "cards", "autos", "relics"])

    elif name == "count_cards":
        st.caption(f"Total: **{result.get('total', 0):,}** cards")
        if result.get("by_year"):
            st.bar_chart(_df(result["by_year"]), x="year", y="n", height=180)
        _table(result.get("by_brand", []), ["brand", "n"])

    elif name == "cards_for_player":
        st.caption(f"{result.get('count_returned', 0)} cards")
        _table(result.get("cards", []),
               ["year", "set_name", "card_number", "parallel_name", "print_run",
                "is_auto", "is_relic", "team"])

    elif name == "sets_for_player":
        st.caption(f"{result.get('set_count', 0)} sets")
        _table(result.get("sets", []), ["year", "set_name", "brand", "sport", "cards_in_set"])

    elif name == "search_sets":
        st.caption(f"{result.get('count', 0)} sets")
        _table(result.get("sets", []), ["set_id", "year", "set_name", "brand", "sport", "card_count"])

    elif name == "search_player":
        _table(result.get("matches", []), ["id", "full_name", "sport", "card_count"])

    elif name == "set_details":
        s = result.get("set", {})
        st.caption(f"**{s.get('name')}** — {s.get('card_count', 0)} cards, {s.get('player_count', 0)} players")
        fam = result.get("product_family")
        if fam:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Subsets", fam["subset_count"])
            c2.metric("Family cards", f"{fam['total_cards']:,}")
            c3.metric("Autos", f"{fam['total_autos']:,}")
            c4.metric("Relics", f"{fam['total_relics']:,}")
            _table(fam.get("subsets", []), ["id", "name", "card_count"])
        elif result.get("sample_players"):
            _table(result["sample_players"], ["id", "full_name"])

    else:  # db_stats and anything unrecognized
        st.json(result, expanded=False)

    with st.expander("raw JSON", expanded=False):
        st.code(json.dumps(result, default=str, indent=2)[:8000], language="json")


def conversation_to_markdown(messages: list[dict]) -> str:
    """Serialize the chat to readable Markdown: your questions, Claude's text
    answers, and a note of which tools it called. Tool-result payloads are
    omitted (they're rendered live but would bloat an export)."""
    lines = ["# Trading Card Chatbot — conversation", ""]
    for m in messages:
        content = m["content"]
        if m["role"] == "user" and isinstance(content, str):
            lines += [f"**You:** {content}", ""]
            continue
        if m["role"] == "user":
            continue  # tool_result blocks — skip
        # assistant: gather text + tool-call names
        texts, tools_used = [], []
        for block in content if isinstance(content, list) else []:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "text" and block.get("text"):
                texts.append(block["text"])
            elif btype == "tool_use":
                tools_used.append(block.get("name", ""))
        if texts:
            lines += [f"**Assistant:** {' '.join(texts)}", ""]
        if tools_used:
            lines += [f"_(tools used: {', '.join(dict.fromkeys(tools_used))})_", ""]
    return "\n".join(lines)

# ─── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Trading Card Chatbot", layout="wide")

CUSTOM_CSS = """
<style>
  /* Header banner */
  .tcc-header { margin:.2rem 0 .1rem; }
  .tcc-title  {
    font-size:2.15rem; font-weight:800; letter-spacing:-.02em; line-height:1.05;
    background:linear-gradient(92deg,#f4d488 0%,#e0a94a 45%,#c98a2e 100%);
    -webkit-background-clip:text; background-clip:text; color:transparent;
  }
  .tcc-sub { color:#9aa0b0; font-size:.92rem; margin-top:.15rem; }
  .tcc-rule { height:2px; border:0; margin:.6rem 0 1.1rem;
    background:linear-gradient(90deg,rgba(224,169,74,.55),rgba(224,169,74,0) 70%); }

  /* Buttons as chips — rounded, subtle gold border, warm hover */
  .stButton > button {
    border-radius:10px; border:1px solid rgba(224,169,74,.22);
    transition:border-color .12s ease, background-color .12s ease, transform .04s ease;
  }
  .stButton > button:hover {
    border-color:rgba(224,169,74,.7); background-color:rgba(224,169,74,.08);
  }
  .stButton > button:active { transform:translateY(1px); }

  /* Chat bubbles — soft rounded card with a thin border */
  [data-testid="stChatMessage"] {
    border:1px solid rgba(255,255,255,.06); border-radius:14px;
    padding:.35rem .65rem; margin-bottom:.35rem;
  }

  /* Sidebar section headings + metrics */
  [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { color:#e0a94a; }
  [data-testid="stMetricValue"] { font-size:1.35rem; }

  /* Slightly tighter top padding on the main pane */
  .block-container { padding-top:2.2rem; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
st.markdown(
    """
    <div class="tcc-header">
      <div>
        <div class="tcc-title">Trading Card Chatbot</div>
        <div class="tcc-sub">Ask about Panini cards, players &amp; sets — every answer grounded in a local SQLite database.</div>
      </div>
    </div>
    <hr class="tcc-rule" />
    """,
    unsafe_allow_html=True,
)

# ─── DB bootstrap ─────────────────────────────────────────────────────────────
if not DB_PATH.exists():
    st.warning(f"No database at `{DB_PATH}`. Initializing an empty one — "
               f"run the scraper + ingest to populate it (see README).")
    init_db()

# ─── Conversation state + persistence ─────────────────────────────────────────
# One active conversation lives in session_state; it's mirrored to a JSON file
# (chatbot/conversations.py) so saved chats survive reruns and server restarts.
if "conv_id" not in st.session_state:
    st.session_state.conv_id = conversations.new_id()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "conv_title" not in st.session_state:
    st.session_state.conv_title = "New chat"
if "conv_title_custom" not in st.session_state:
    st.session_state.conv_title_custom = False


def _persist() -> None:
    """Save the active conversation to disk (empty chats are not written)."""
    if not st.session_state.conv_title_custom:
        st.session_state.conv_title = conversations.title_from_messages(
            st.session_state.messages)
    conversations.save(st.session_state.conv_id,
                       st.session_state.conv_title,
                       st.session_state.messages)


def _new_chat() -> None:
    _persist()  # keep whatever the current chat already has
    st.session_state.conv_id = conversations.new_id()
    st.session_state.messages = []
    st.session_state.conv_title = "New chat"
    st.session_state.conv_title_custom = False


def _load_chat(cid: str) -> None:
    _persist()  # save current before switching away
    data = conversations.load(cid)
    if data:
        st.session_state.conv_id = data["id"]
        st.session_state.messages = data.get("messages", [])
        st.session_state.conv_title = data.get("title", "New chat")
        st.session_state.conv_title_custom = True  # respect the saved title


def _delete_chat(cid: str) -> None:
    conversations.delete(cid)
    if cid == st.session_state.conv_id:  # deleted the active one → start fresh
        st.session_state.conv_id = conversations.new_id()
        st.session_state.messages = []
        st.session_state.conv_title = "New chat"
        st.session_state.conv_title_custom = False


def _ago(ts: float) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


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
    st.subheader("Conversations")

    if st.button("➕  New chat", width="stretch"):
        _new_chat()
        st.rerun()

    # Rename the active conversation (auto-titled from the first question until
    # you type your own). The widget key includes the current title so that when
    # the title changes programmatically (auto-derive, or switching chats) the
    # box re-initialises with the new value instead of retaining a stale one — a
    # common Streamlit pitfall. Renames commit via on_change (fires only on a
    # real user edit), so the auto-derived title is never clobbered.
    _title_key = f"title_{st.session_state.conv_id}_{st.session_state.conv_title}"

    def _rename() -> None:
        v = (st.session_state.get(_title_key) or "").strip()
        if v and v != st.session_state.conv_title:
            st.session_state.conv_title = v
            st.session_state.conv_title_custom = True
            if st.session_state.messages:
                _persist()

    st.text_input(
        "Chat title",
        value=st.session_state.conv_title,
        key=_title_key,
        on_change=_rename,
        label_visibility="collapsed",
        placeholder="Chat title…",
    )

    saved = conversations.list_all()
    if saved:
        st.caption(f"{len(saved)} saved")
        for c in saved:
            active = c["id"] == st.session_state.conv_id
            col_open, col_del = st.columns([6, 1], gap="small")
            label = ("● " if active else "") + c["title"]
            if col_open.button(
                label,
                key=f"open_{c['id']}",
                width="stretch",
                disabled=active,
                help=f"{c['turns']} question(s) · {_ago(c['updated'])}",
            ):
                _load_chat(c["id"])
                st.rerun()
            if col_del.button("🗑", key=f"del_{c['id']}", help="Delete this chat"):
                _delete_chat(c["id"])
                st.rerun()
    else:
        st.caption("No saved chats yet — ask a question to start one.")

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
    _msgs = st.session_state.get("messages", [])
    st.download_button(
        "⬇︎  Export this chat",
        data=conversation_to_markdown(_msgs) if _msgs else "",
        file_name=f"card-chat-{date.today():%Y%m%d}.md",
        mime="text/markdown",
        disabled=not _msgs,
        width="stretch",
        help="Download this conversation as Markdown",
    )

# ─── Replay history ───────────────────────────────────────────────────────────
def _tool_names_by_id(messages: list[dict]) -> dict[str, str]:
    """Map each tool_use_id -> tool name, so tool_result blocks (which carry only
    the id) can be rendered by the right renderer on history replay."""
    names: dict[str, str] = {}
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names[block.get("id", "")] = block.get("name", "")
    return names


def _render_message(msg: dict, tool_names: dict[str, str]) -> None:
    """Render one message. Assistant messages may be a list of content blocks
    (text + tool_use); tool_result blocks are rendered as tables/charts."""
    role = msg["role"]
    with st.chat_message("user" if role == "user" else "assistant"):
        content = msg["content"]
        if isinstance(content, str):
            st.markdown(content)
            return
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text":
                text = block["text"] if isinstance(block, dict) else block.text
                if text:
                    st.markdown(text)
            elif btype == "tool_use":
                name = block["name"] if isinstance(block, dict) else block.name
                inp = block["input"] if isinstance(block, dict) else block.input
                with st.expander(f"🔧 `{name}`", expanded=False):
                    st.code(json.dumps(inp, indent=2), language="json")
            elif btype == "tool_result":
                tid = block.get("tool_use_id") or ""
                name = tool_names.get(tid, "")
                try:
                    result = json.loads(block.get("content", "") or "{}")
                except (ValueError, TypeError):
                    result = {"raw": str(block.get("content", ""))}
                render_tool_result(name, result)


_tool_names = _tool_names_by_id(st.session_state.messages)
for msg in st.session_state.messages:
    _render_message(msg, _tool_names)

# ─── Example prompts (only on an empty chat) ──────────────────────────────────
if not st.session_state.messages and "pending_q" not in st.session_state:
    st.markdown("**Try one of these:**")
    cols = st.columns(2)
    for i, q in enumerate(EXAMPLE_QUESTIONS):
        if cols[i % 2].button(q, width="stretch", key=f"ex_{i}"):
            st.session_state.pending_q = q
            st.rerun()

# ─── Input ────────────────────────────────────────────────────────────────────
user_input = st.chat_input("Ask about a player, a set, a year…") or st.session_state.pop("pending_q", None)
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
                # Stream the text so the answer appears as it's written; the
                # assembled final message still gives us tool_use blocks + stop.
                text_ph = st.empty()
                streamed = ""
                with client.messages.stream(
                    model=model_id,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_SCHEMAS,
                    messages=api_messages,
                ) as stream:
                    for delta in stream.text_stream:
                        streamed += delta
                        text_ph.markdown(streamed + "▌")
                    final = stream.get_final_message()
                # NB: use if/else statements, not a bare ternary expression —
                # Streamlit "magic" would auto-render a bare expression's value.
                if streamed:
                    text_ph.markdown(streamed)
                else:
                    text_ph.empty()

                # Convert content blocks to serializable dicts so we can stash
                # them in session_state and re-send next turn.
                assistant_content = []
                tool_uses = []
                for block in final.content:
                    if block.type == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        tu = {"type": "tool_use", "id": block.id,
                              "name": block.name, "input": block.input}
                        assistant_content.append(tu)
                        tool_uses.append(tu)

                st.session_state.messages.append({"role": "assistant", "content": assistant_content})
                api_messages.append({"role": "assistant", "content": assistant_content})

                # Text is already on screen from the stream; just show tool calls.
                for block in assistant_content:
                    if block["type"] == "tool_use":
                        with st.expander(f"🔧 `{block['name']}`", expanded=False):
                            st.code(json.dumps(block["input"], indent=2), language="json")

                if final.stop_reason != "tool_use" or not tool_uses:
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
                    render_tool_result(tu["name"], result)

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

    # Save the conversation (with this completed turn) to disk, and refresh the
    # sidebar list / title so a brand-new chat shows up immediately.
    _persist()
    st.rerun()
