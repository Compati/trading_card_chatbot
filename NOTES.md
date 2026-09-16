# Project Notes / Dev Log

A running log of significant work, the current data state, and candidate next
steps. Newest session first.

---

## Current state (as of 2026-09-15)

- **Repo:** github.com/Compati/trading_card_chatbot — `origin/main` = `9ffc3b3`
  (this session's UI work is committed on top; see the 2026-09-15 entry).
- **Database** (`db/cards.db`, ~57 MB — no longer git-tracked; served as a
  GitHub Release asset on the rolling `db-latest` release, fetched by
  `scripts/fetch_db.sh` and published with `scripts/publish_db.py`):
  - **19,443 sets / 706,410 cards / 17,740 players**
  - **8 sports:** football (324,709 cards), basketball (174,755), baseball
    (96,021), soccer (57,678), racing (26,708 NASCAR), UFC/MMA (12,625),
    WWE (12,609), golf (1,305 LIV). Years ~2016–2026.
  - Sourced from TCDB (19,421 sets) + Panini-America "Download Full Checklist"
    CSVs (14 sets — premium 2025-26 bball flagships TCDB left empty, plus
    WC-2026 soccer). Parent/subset hierarchy built (`parent_set_id`).
  - Integrity clean: 0 rival-maker sets, 0 dup groups, `players_fts` in sync.
- **Chatbot:** Streamlit chat, live Claude tool-use loop (Haiku 4.5 default;
  Sonnet 5 / Opus 5 selectable). `.env` holds a real `ANTHROPIC_API_KEY`.
  10 SQL-backed tools; tool results render as tables/charts. **Now themed
  (gold-on-charcoal) with saved/named conversations** (see 2026-09-15).
- **Known data caveat:** ~1.3k cards of contamination were identified but left
  in per an earlier decision (Finest Overtime Elite, sticker albums, indie
  one-offs). Purge available on request — use an explicit set-id list.

---

## Session 2026-09-15 — chatbot UI: theme + conversation management

Data was already current and republished (DB reconciled: the `db-latest` asset
matches the local file). Two shipped items this session:

### 1. Fixed the "Panini Panini" doubled-brand set name (commit `9ffc3b3`)
`ingest/panini_csv._set_name` prepended the manufacturer even when the CSV
PROGRAM already led with it, yielding "2026 Panini **Panini** Intl France". Added
a guard (skip the prefix when the program already starts with the brand word) +
renamed the 3 existing rows to "2026 Panini Intl France/Germany/Mexico".

### 2. Visual theme & polish
- **`.streamlit/config.toml`** — a "premium card" dark palette: charcoal-navy
  ground (`#0e1016`) with a warm gold accent (`#e0a94a`). *Requires a server
  restart to take effect — config.toml is read at startup, not on rerun.*
- **`app.py`** injects `CUSTOM_CSS`: a gold gradient title header (replacing
  `st.title`), an accent rule, chip-styled buttons (rounded, gold hover),
  rounded chat bubbles, gold sidebar section headings. CSS targets stable
  selectors only (`[data-testid=…]`, `.stButton>button`), no hashed classes.
- Removed the `🃏` emoji from both the header and the browser tab (`page_icon`)
  — it rendered as a colorful jester glyph. The tab now falls back to
  Streamlit's default favicon.

### 3. Conversation management (persistent, survives restarts)
- **`chatbot/conversations.py`** (new) — one JSON file per chat in a gitignored
  `conversations/` dir. API: `new_id / title_from_messages / save (skips empty,
  atomic write, preserves created) / load / delete / list_all`. Unlike
  `session_state`, these survive reruns, server restarts, and new sessions.
- **`app.py`** sidebar gained a **Conversations** panel: ➕ New chat, inline
  rename, saved-chat list (● active marker, per-row 🗑 delete), auto-title from
  the first question, "N saved" count. Redundant Clear-chat button removed;
  Export kept. Each completed turn (and new/switch/rename/delete) persists.
- **Fixed a classic Streamlit trap:** an always-on rename `text_input` retained
  a stale value across reruns and clobbered the auto-derived title back to
  "New chat". Fix: the widget key includes the current title (so a programmatic
  change re-inits the box) and renames commit via an `on_change` callback
  (fires only on a real edit), never a diff-check.

**Verified live** in the browser: theme renders; auto-title, save, persistence
across a full reload, switch-and-replay (charts/tables rebuild from stored tool
results), rename (Enter/blur), and delete (disk-confirmed) all work.

*Dev-server note:* `preview_start` resolves the parent `apps/.claude` launch
config (pickem), not this project's — launch Streamlit by hand:
`.venv/Scripts/python.exe -m streamlit run app.py --server.port 8501 --server.headless true`.

### Candidate next steps (from this session)
- **Favicon:** the tab now shows Streamlit's default. If a custom-but-clean icon
  is wanted, inject a `<link rel="icon">` (data-URI SVG) or drop a `favicon.png`
  in `.streamlit/static/` — Streamlit's `page_icon` can't render a blank.
- **Follow-up suggestion chips:** after each answer, offer 2–3 related questions
  (was offered in the UI-focus menu; not chosen this round).
- **Richer result rendering:** auto/relic as badges, `print_run` as `/99`,
  clickable player/set drill-downs.
- **Export all conversations** / bulk management (currently per-chat Export).
- **Contamination purge** (~1.3k cards) if the earlier hold is lifted.
- **Refresh 2025-26 premium bball** in a few weeks as TCDB catalogues more
  inserts; ingest **WC-2026 soccer** updates as those pre-release checklists firm
  up. Reclaim ~2 GB `data/raw/` if disk is needed (DB stands alone).

---

## Session 2026-08-31

### 1. 2020–2022 curated-subset backfill (download + ingest)
Downloaded the previously-discovered 2020–2022 subset sids (basketball 2020/21,
football 2020/21/22) that were sitting in `data/scrape_index.json` un-downloaded.
- ~7,847 target sids → **6,924 ok / 610 empty / 2 benign failures** (a rewards-
  points non-set + a magnet set whose page-1 ingested).
- `verify_trim` scanned 7,236 target-year sets → **0 junk pages** (the hardened
  download guard held; no bulk-load junk-loops).
- Full `python -m ingest.load --from-raw`, then orphan cleanup + FTS rebuild.
- Year growth: 2020 23k→~97k, 2021 25k→~93k, 2022 31k→~80k. 2023/2024 unchanged.
- Commit `742e1d0`.

### 2. Fixed a dead tool: `search_sets`
It referenced `is_auto`/`is_relic`/`is_rookie` (never its parameters) plus a
non-existent `cards` alias, so **every call raised `NameError`** — the "find sets
by name/year/sport/brand" tool had been silently broken. Removed the stray block.

### 3. Typo-tolerant player search
FTS5 is prefix-tolerant but not typo-tolerant ("Mahoms" returned nothing). Added a
tier-3 `difflib` fuzzy fallback in `search_player` (cutoff 0.72) that fires only
when both FTS and LIKE miss; scores the query against each player's normalized
name and its tokens, so last-name typos resolve ("Mahoms"→Mahomes,
"Wembanyma"→Wembanyama). Matches are flagged `"fuzzy": true` so the model confirms.
(2 + 3 committed together in `b00a1b1`.)

### 4. Parser: capture parallel labels
TCDB packs the parallel/variation descriptor into the **player cell**, trailing
the anchor name (`<a>Arch Manning</a> SN49 Burnt Orange`; number and team are
separate linked cells). The link-based parser was discarding it. Now
`_parallel_from_cell` extracts the trailing text into `parallel_name` (guarded to
only strip a clean name prefix), and `extract_print_run` also understands TCDB's
`SN99` serial form (previously only `/99`). No card-count change — 8 formerly
identical "Arch Manning PT-AM" rows now carry distinct labels.

### 5. Brand filter: exclude non-Panini rival sets
Rival products slipped into the DB by sharing a Panini sub-brand word ("Topps
Pristine", "Leaf Trinity Pristine", "Topps Triple Threads"). Added
`is_non_panini` / `_is_rival_set`, which reject a set only when a rival maker
**leads** the name (after an optional year) — so ambiguous words appearing
mid-name in real Panini subsets ("… Wild Card Round", "… Prime Onyx") are kept.
Applied at both discovery (`tcdb_discover._is_panini_set`) and ingest
(`ingest.load._ingest_tcdb_set` skips them). 44 rival sets removed.
(4 + 5 + regenerated DB committed in `4687938`.)

> **Gotcha (cost a re-ingest cycle):** do NOT make `detect_brand` word-boundary
> (`\bbrand\b`). Panini subset names pluralize/possessive brand words ("Canton
> Absolute**s**", "Draft Picks **Prizms**"), so `\babsolute\b` misses "absolutes",
> flips the brand Absolute→Panini, and because `_load_set` keys on
> `(brand,sport,year,name)` a re-ingest then creates **duplicate** set rows
> (183 of them). Kept substring matching. General rule: any `detect_brand` change
> plus an idempotent re-ingest risks dup sets, because the match key includes
> brand and stale old-brand rows aren't cleaned up.

---

## Candidate next steps

### Expand to other sports (the main growth lever)
The scrape pipeline is sport-name driven
(`/ViewAll.cfm/sp/{FullSportName}/year/{YYYY}`), so a new sport is mostly a matter
of running discover → download → ingest for it. Panini-licensed candidates worth
adding, roughly by value:
- **Soccer** (strongest): Panini is huge here — Prizm Premier League, Donruss
  Soccer, Select Soccer, Obsidian Soccer, World Cup products. TCDB sport name is
  `Soccer`, so it should drop straight into the existing pipeline. Big, high-
  interest catalog.
- **UFC / MMA:** Panini holds the UFC license (Prizm UFC, Select UFC, Chronicles
  UFC). Check TCDB's sport bucket (may be under a combat-sports / non-sport
  heading rather than a clean `MMA`), then confirm the `ViewAll` URL form.
- **WNBA:** Panini-licensed; smaller but clean fit alongside basketball.
- **College-specific products:** partly covered already via draft-pick sets;
  could be deepened if there's interest.
- **Skip:** MLB/baseball depth (no Panini license), and sports Panini doesn't hold
  (F1, most current hockey).
  Reuse the curated-subset flow: `tcdb_discover` (top-level) →
  `tcdb_discover_subsets` (curated inserts/autos/relics) → `tcdb_download
  --from-index` → `ingest.load --from-raw`. Budget: football/basketball ran
  ~800–2,000 curated subsets per sport-year; soccer is likely comparable or larger.

### Data / coverage
- **Auto/relic completeness is capped by TCDB.** The ultra-premium 1/1
  auto/patch/logoman sets are exactly the ones TCDB catalogues as *empty*, so
  "how many autos" answers undercount for stars (e.g. Wembanyama's premium RC
  autos). This is a source-data limitation, not a bug — worth surfacing in the
  chatbot's wording, or supplementing from another source if it matters.
- **Reclaim disk:** `data/raw/` (~1.5–1.8 GB) can be deleted now that the parser
  work is done; the DB stands alone. (Left in place for now by choice.)

### Code / UX polish (all low priority)
- **Parser:** parallel labels are now captured verbatim ("SN49 Burnt Orange",
  "AU, SN1 Black") but not normalized into a color/finish/auto taxonomy — a future
  step if structured parallel querying is wanted.
- **Fuzzy search perf:** the typo fallback does a full-table scan (~300–400 ms) on
  a miss. Fine as a fallback; could prune candidates (e.g. by first letter or a
  trigram index) if it ever matters.
- **Model config:** `app.py` passes no `thinking` param and `max_tokens=2048`;
  fine for Haiku/Sonnet, but watch for truncation if Opus-5 (thinking-on) is used
  for long answers.
