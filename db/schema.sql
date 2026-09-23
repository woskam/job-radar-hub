-- Generic, non-personal job listings only -- no relevance score, no status,
-- no link to letters/CVs. That stays entirely private on whichever Job Radar
-- instance pushes into this hub. See README.md for the ingest contract.
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    company TEXT,
    location TEXT,
    url TEXT,
    description TEXT,
    scraped_at TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    UNIQUE (source, external_id)
);

-- Consumer API keys, issued by hand via manage.py -- no self-serve signup in
-- v1. `key` stores sha256(raw key), never the raw key itself -- a leaked
-- database file is then a leak of nobody's actual credential. `key_preview`
-- (a short, non-reversible prefix of the raw key, e.g. "jrh_aB3xYz...") is
-- purely so `manage.py list-keys` output is still recognizable to a human.
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    key_preview TEXT,
    owner TEXT,
    rate_limit_per_hour INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMP NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT 0
);

-- Email alert subscribers -- this hub's first real personal-data store
-- (an email address). status starts 'pending' until the confirm link is
-- clicked (double opt-in), and becomes 'unsubscribed' rather than being
-- deleted outright when the unsubscribe link is used, so a re-signup with
-- the same address (email UNIQUE) is a clean upsert, not a conflict. All
-- filter fields are optional; NULL/empty means "don't filter on this".
-- keywords/exclude_keywords are JSON arrays (OR'd / NOT'd against
-- title+description); location_mode is NULL (any), 'remote', or a
-- free-text city/country string -- deliberately one field, not a separate
-- remote-only flag alongside a location string, since a listing's
-- location realistically reads as either "Remote" or a city, rarely both,
-- and ANDing two such filters together would silently zero out matches.
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    confirm_token TEXT NOT NULL,
    unsubscribe_token TEXT NOT NULL,
    last_confirm_sent_at TIMESTAMP,
    keywords TEXT,
    exclude_keywords TEXT,
    location_mode TEXT,
    category TEXT,
    segment TEXT,
    company TEXT,
    last_sent_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL
);
