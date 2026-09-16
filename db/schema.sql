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

-- Consumer API keys, issued by hand via manage.py -- no self-serve signup in v1.
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    owner TEXT,
    rate_limit_per_hour INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMP NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT 0
);
