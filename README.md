# Trading Card Chatbot

A local SQLite database of Panini trading card checklists plus a Streamlit chat interface that lets you ask natural-language questions like:

- *"What sets is Patrick Mahomes in?"*
- *"How many cards does LeBron James have?"*
- *"Show me Aaron Judge's 2024 cards."*
- *"What players are in 2024 Donruss Football?"*

Built so you can stop scrolling through card sites every time you need to look something up at work.

---

## How it works

1. **Scrape** Panini checklists from TCDB (primary) with Panini.com / manual files as fallback. TCDB serves checklist HTML to an ordinary HTTP client, so the scraper uses a plain `urllib` request (no browser needed).
2. **Parse** scraped HTML/PDF/XLSX into normalized card rows.
3. **Load** into a local SQLite database with FTS5 player search.
4. **Chat** via a Streamlit app where Claude uses tool calls to query the database — structured answers with exact counts, no RAG hallucination.

---

## Setup

### 1. Create a virtual environment

**PowerShell:**
```powershell
cd "C:\Users\Alex\Documents\Alex\Code\apps\Trading Card Chatbot"
python -m venv .venv
.venv\Scripts\Activate.ps1
```

> If PowerShell blocks activation: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Set your Anthropic API key

```powershell
copy .env.example .env
```

Open `.env` and replace the placeholder with your real key.

### 4. Get the database

`db/cards.db` is **not** committed to git — it grew toward GitHub's 100 MB
single-file limit and bloated history. It's published as a **GitHub Release
asset** (`cards.db` on the rolling `db-latest` release) and pulled on demand.

Fetch the current build (public repo, no auth needed):

```bash
bash scripts/fetch_db.sh          # set FORCE=1 to overwrite an existing copy
```

Or start an empty schema instead: `python -m db.connection init`.

In **Codespaces** this fetch runs automatically on container create (see
`.devcontainer/devcontainer.json`), so the app comes up with data.

**After a data update**, republish the DB so deploys pick it up:

```bash
.venv/Scripts/python.exe scripts/publish_db.py
```

(Uploads `db/cards.db` to the `db-latest` release. Auth comes from
`$GITHUB_TOKEN`, `gh auth token`, or your existing git credential.)

---

## Building the card database

The ingestion pipeline is split into three stages: **discover** → **download** → **ingest**. Each stage is restartable and writes a status log so you can see what worked.

### Phase 0 — one real set end-to-end

Pulls a single TCDB set through the whole pipeline. Use this first to confirm everything works before scaling up. The example below is **2024 Donruss Football** (`sid 462124`).

```powershell
python -m scraper.tcdb_download --sid 462124 --sport football --year 2024 --set-name "2024 Donruss"
python -m ingest.load --sid 462124
sqlite3 db/cards.db "SELECT COUNT(*) FROM cards; SELECT COUNT(*) FROM players;"
```

You should see ~400 cards / ~400 players.

> **Finding a set's `sid`:** open the set on TCDB and read it from the URL —
> `tcdb.com/ViewSet.cfm/sid/`**`462124`**`/2024-Donruss`. The downloader
> resolves the URL slug automatically from the sid, pages through the checklist
> (`?PageIndex=1..N`, 100 cards/page), and saves one `set_<sid>_p<n>.html` per
> page. A bundled offline smoke test (`sid 999999`, a synthetic fixture already
> on disk — no network) exercises the parser's header-fallback path; run it with
> `python -m ingest.load --sid 999999`.

### Phase 1+ — bulk scrape

```powershell
# 1. Walk TCDB to enumerate all Panini sets for the given filters
python -m scraper.tcdb_discover --sport football --year 2025

# 2. Download HTML for every discovered set (rate-limited, polite)
python -m scraper.tcdb_download --from-index

# 3. Ingest everything that downloaded successfully
python -m ingest.load --from-raw
```

### Manual fallback (Panini-only releases TCDB doesn't have yet)

1. Visit the set on paniniamerica.net in your browser, download the XLSX/PDF.
2. Save it under `data/manual/{sport}/{year}/`.
3. Create a sidecar file next to it, same basename, ending `.meta.json`:
   ```json
   {"brand": "Panini", "sport": "football", "year": 2025, "set_name": "2025 Prizm Football"}
   ```
4. Run: `python -m ingest.manual`

---

## Running the chatbot

```powershell
streamlit run app.py
```

The app opens at `http://localhost:8501`. Pick a model in the sidebar (default Haiku 4.5), ask questions in the chat box.

The bot will tell you what's *not* in the database — if you ask about 2019 cards but you've only loaded 2024, it'll say so honestly using the `db_stats` tool instead of guessing.

---

## File structure

```
Trading Card Chatbot/
├── app.py                       # Streamlit chat UI
├── requirements.txt
├── .env.example / .env          # API key
├── .gitignore
├── README.md
├── db/
│   ├── schema.sql               # SQLite schema
│   ├── connection.py            # DB init + connection helpers
│   └── cards.db                 # SQLite file (created on init)
├── scraper/
│   ├── tcdb_discover.py         # Walk TCDB, list set URLs
│   ├── tcdb_download.py         # Fetch each set's HTML
│   └── panini_fallback.py       # Panini direct fallback for new releases
├── parsers/
│   ├── normalize.py             # Player names, parallels, teams
│   ├── tcdb_html.py             # Parse one TCDB checklist
│   ├── panini_xlsx.py
│   └── panini_pdf.py
├── ingest/
│   ├── load.py                  # Parsed rows → DB upserts
│   └── manual.py                # data/manual/ → DB
├── chatbot/
│   ├── tools.py                 # SQL-backed tools Claude calls
│   └── system_prompt.py
└── data/
    ├── raw/{tcdb,panini}/{sport}/{year}/   # downloaded HTML, set_<sid>_p<n>.html per page
    ├── manual/                              # user-supplied files
    ├── scrape_index.json                    # discovered sets (sid, slug, name)
    └── scrape_status.json                   # per-set status log
```

---

## Troubleshooting

**Download logs `failed: could not resolve checklist slug`** — the `sid` is wrong or the set has no checklist on TCDB. Confirm the sid by opening `tcdb.com/ViewSet.cfm/sid/<sid>` in a browser.

**Download logs `empty` / `checklist 404`** — the set page exists but has no card rows, or TCDB changed its URL scheme. Re-check the sid; the bare `/Checklist.cfm/sid/<sid>` (no slug) always 404s, which the downloader handles by resolving the slug for you.

**Only the base set loads, not parallels/inserts** — expected. On TCDB each insert/parallel subset is its *own* sid (listed on the set's ViewSet page). Discover picks those up too; add them to the index to ingest them.

**Bot says "I don't have that data"** — That's working as intended. Check `data/scrape_status.json` to see what's been loaded.

**Chatbot gives wrong counts** — Run `sqlite3 db/cards.db "SELECT COUNT(*) FROM cards;"` directly and compare. If the DB is right but the bot is wrong, that's a tool-use bug in `chatbot/tools.py`.
