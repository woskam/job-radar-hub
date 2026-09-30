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
-- (an email address). status is 'pending' until the confirm link is
-- clicked (double opt-in), then 'active' -- and stays 'active' from then
-- on: confirmation is a one-time, email-level thing ("one confirm link
-- covers all your searches"), not something each saved search repeats.
-- A subscriber never becomes 'unsubscribed' -- opting out is per SEARCH
-- now (see saved_searches below), not per email.
--
-- unsubscribe_token/keywords/exclude_keywords/location_mode/category/
-- segment/company/last_sent_at below are the pre-multi-search shape,
-- kept (not dropped -- same conservative migration convention
-- db/migrations.py already follows elsewhere) but no longer written or
-- read after db/migrations.py's one-time split into saved_searches.
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

-- One row per saved search (a subscriber may have several -- up to
-- MAX_SAVED_SEARCHES, see db/queries.py). status is 'pending' only
-- briefly, between a fresh (unconfirmed) subscriber's first search and
-- their confirm click, which activates every pending search of theirs
-- at once; a search added later for an already-'active' subscriber
-- starts 'active' immediately, no confirmation needed. 'unsubscribed'
-- is the per-search opt-out -- each digest email is about exactly one
-- search, so its own unsubscribe link only ever stops that one.
-- last_sent_at is this search's own "new since when" cursor for
-- send_alerts.py -- each search accumulates matches independently.
-- Filter field meanings are unchanged from the old inline subscribers
-- columns (see the comment above): all optional, NULL/empty = no filter.
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    unsubscribe_token TEXT NOT NULL,
    keywords TEXT,
    exclude_keywords TEXT,
    location_mode TEXT,
    category TEXT,
    segment TEXT,
    company TEXT,
    last_sent_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL
);
