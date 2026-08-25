-- Trading Card Chatbot schema
-- SQLite — file-based, no server. FTS5 for tolerant player name search.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sports (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS brands (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS teams (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sport_id INTEGER NOT NULL REFERENCES sports(id),
    name     TEXT NOT NULL,
    UNIQUE(sport_id, name)
);

CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sport_id        INTEGER NOT NULL REFERENCES sports(id),
    full_name       TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    UNIQUE(sport_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS sets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id     INTEGER NOT NULL REFERENCES brands(id),
    sport_id     INTEGER NOT NULL REFERENCES sports(id),
    year         INTEGER NOT NULL,
    name         TEXT NOT NULL,
    release_date TEXT,
    source       TEXT,           -- 'tcdb' | 'panini' | 'manual'
    source_url   TEXT,
    source_file  TEXT,
    tcdb_sid     INTEGER,
    ingested_at  TEXT,
    UNIQUE(brand_id, sport_id, year, name)
);

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id        INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
    card_number   TEXT,
    player_id     INTEGER REFERENCES players(id),
    team_id       INTEGER REFERENCES teams(id),
    parallel_name TEXT,
    print_run     INTEGER,
    is_auto       INTEGER NOT NULL DEFAULT 0,
    is_relic      INTEGER NOT NULL DEFAULT 0,
    is_rookie     INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_cards_player ON cards(player_id);
CREATE INDEX IF NOT EXISTS idx_cards_set    ON cards(set_id);
CREATE INDEX IF NOT EXISTS idx_cards_team   ON cards(team_id);
CREATE INDEX IF NOT EXISTS idx_sets_year    ON sets(year);
CREATE INDEX IF NOT EXISTS idx_players_norm ON players(normalized_name);

-- FTS5 virtual table over player full_name for tolerant search
CREATE VIRTUAL TABLE IF NOT EXISTS players_fts USING fts5(
    full_name,
    content='players',
    content_rowid='id'
);

-- Keep FTS in sync with players table
CREATE TRIGGER IF NOT EXISTS players_ai AFTER INSERT ON players BEGIN
    INSERT INTO players_fts(rowid, full_name) VALUES (new.id, new.full_name);
END;

CREATE TRIGGER IF NOT EXISTS players_ad AFTER DELETE ON players BEGIN
    INSERT INTO players_fts(players_fts, rowid, full_name) VALUES('delete', old.id, old.full_name);
END;

CREATE TRIGGER IF NOT EXISTS players_au AFTER UPDATE ON players BEGIN
    INSERT INTO players_fts(players_fts, rowid, full_name) VALUES('delete', old.id, old.full_name);
    INSERT INTO players_fts(rowid, full_name) VALUES (new.id, new.full_name);
END;
