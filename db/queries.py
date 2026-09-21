"""
Shared DB access + the /jobs filter logic -- imported by both app.py (the
REST API) and mcp_server.py (the MCP server), so the two surfaces can never
drift apart on what a filter means or which fields get returned.
"""

import hashlib
import os
import sqlite3
from pathlib import Path

from db.migrations import ensure_columns

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "db" / "schema.sql"

LISTING_FIELDS = ["source", "external_id", "title", "company", "location", "url", "description", "scraped_at"]
# What /jobs and the MCP tool return, in addition to LISTING_FIELDS --
# last_seen_at is this Hub's own receive-time, updated on every ingest that
# touches a listing, so it's a genuine freshness signal a consumer can rely
# on (unlike scraped_at, which is fixed at first-seen time and never moves).
OUTPUT_FIELDS = LISTING_FIELDS + ["last_seen_at"]


def db_path() -> Path:
    # Read lazily (not at import time) so tests/tools can set HUB_DB_PATH
    # after import, same as app.py already relied on.
    return Path(os.environ.get("HUB_DB_PATH", ROOT / "db" / "hub.db"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    ensure_columns(conn)
    return conn


def lookup_api_key(key: str) -> sqlite3.Row | None:
    # api_keys.key stores sha256(raw key), never the raw key -- see manage.py.
    if not key:
        return None
    conn = get_db()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    row = conn.execute("SELECT * FROM api_keys WHERE key = ? AND revoked = 0", (key_hash,)).fetchone()
    conn.close()
    return row


def query_listings(
    *,
    company: str | None = None,
    source: str | None = None,
    title_contains: str | None = None,
    location_contains: str | None = None,
    active: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)

    clauses, params = ["active = ?"], [active]
    if company:
        clauses.append("company = ?")
        params.append(company)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if title_contains:
        clauses.append("title LIKE ?")
        params.append(f"%{title_contains}%")
    if location_contains:
        clauses.append("location LIKE ?")
        params.append(f"%{location_contains}%")

    conn = get_db()
    rows = conn.execute(
        f"SELECT {', '.join(OUTPUT_FIELDS)} FROM listings WHERE {' AND '.join(clauses)} "
        "ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
