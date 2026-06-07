-- market-intelligence schema
-- Design principle: platform-agnostic core. A "game" is separate from where it
-- is sold ("listing"). Steam is just the first collector plugged into this core;
-- new platforms = new rows in `platforms` + a new collector, no schema changes.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Reference: platforms
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platforms (
    platform_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,   -- 'steam', 'ios', 'android', 'switch', 'epic'
    name          TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Canonical game (one row per game, regardless of how many platforms it's on)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS games (
    game_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name      TEXT NOT NULL,
    developer           TEXT,
    publisher           TEXT,
    is_indie            INTEGER,           -- 1/0/NULL
    first_release_date  TEXT,              -- ISO date 'YYYY-MM-DD'
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Genres / tags (platform-agnostic taxonomy)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS genres (
    genre_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS game_genres (
    game_id   INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(genre_id) ON DELETE CASCADE,
    source    TEXT,                        -- where this tag came from ('steam', 'igdb')
    PRIMARY KEY (game_id, genre_id)
);

-- ---------------------------------------------------------------------------
-- Listing: a game's presence on a specific platform's store
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
    listing_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id        INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    platform_id    INTEGER NOT NULL REFERENCES platforms(platform_id),
    external_id    TEXT NOT NULL,          -- steam appid, app store id, etc.
    store_url      TEXT,
    release_date   TEXT,                   -- per-platform release (may differ)
    base_price_usd REAL,                   -- list price (no discount)
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (platform_id, external_id)
);

-- ---------------------------------------------------------------------------
-- Snapshot: time-series metrics, captured each collection cycle. NEVER updated
-- in place -- every cycle inserts a fresh row so we accumulate history.
-- This is what turns "scan once" into "past -> present -> future".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_snapshots (
    snapshot_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id             INTEGER NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    captured_at            TEXT NOT NULL DEFAULT (datetime('now')),
    price_usd              REAL,
    discount_pct           INTEGER,
    review_count_total     INTEGER,
    review_count_positive  INTEGER,
    review_score_pct       REAL,           -- 0-100
    ccu                    INTEGER,        -- peak/concurrent users (Steam-specific)
    estimated_owners_min   INTEGER,        -- SteamSpy estimate (coarse, use with care)
    estimated_owners_max   INTEGER,
    extra_json             TEXT            -- platform-specific metrics as JSON
);

CREATE INDEX IF NOT EXISTS idx_snapshots_listing_time
    ON listing_snapshots (listing_id, captured_at);

-- ---------------------------------------------------------------------------
-- Reviews: individual review text for sentiment mining
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviews (
    review_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id              INTEGER NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    external_review_id      TEXT,
    captured_at             TEXT NOT NULL DEFAULT (datetime('now')),
    posted_at               TEXT,
    is_positive             INTEGER,       -- 1/0
    votes_up                INTEGER,
    votes_funny             INTEGER,
    playtime_at_review_min  INTEGER,
    language                TEXT,
    body                    TEXT,
    UNIQUE (listing_id, external_review_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_listing
    ON reviews (listing_id);

-- ---------------------------------------------------------------------------
-- Collection run log: bookkeeping for each pipeline execution
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_id   INTEGER REFERENCES platforms(platform_id),
    collector     TEXT,                    -- which script ran
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    status        TEXT,                    -- 'running', 'success', 'failed'
    items_seen    INTEGER DEFAULT 0,
    notes         TEXT
);

-- ---------------------------------------------------------------------------
-- Seed: platforms we know about (Steam active now, others reserved)
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO platforms (code, name) VALUES
    ('steam',   'Steam (PC)'),
    ('epic',    'Epic Games Store'),
    ('ios',     'Apple App Store'),
    ('android', 'Google Play'),
    ('switch',  'Nintendo Switch');
