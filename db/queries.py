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

LISTING_FIELDS = ["source", "external_id", "title", "company", "location", "url", "description", "scraped_at", "category", "segment"]
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
    category: str | None = None,
    segment: str | None = None,
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
    if category:
        clauses.append("category = ?")
        params.append(category)
    if segment:
        clauses.append("segment = ?")
        params.append(segment)

    conn = get_db()
    rows = conn.execute(
        f"SELECT {', '.join(OUTPUT_FIELDS)} FROM listings WHERE {' AND '.join(clauses)} "
        "ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Max items/length accepted for keywords/exclude_keywords -- this feeds a
# public, unauthenticated endpoint (POST /alerts/subscribe), so the caller
# (app.py) enforces these before ever building a query with them: an
# attacker-supplied huge list would otherwise become a huge WHERE clause
# re-run on every subscriber on every daily send_alerts.py run.
MAX_ALERT_KEYWORDS = 10
MAX_ALERT_KEYWORD_LENGTH = 60


def query_new_listings_for_alert(
    conn: sqlite3.Connection,
    *,
    keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    location_mode: str | None = None,
    category: str | None = None,
    segment: str | None = None,
    company: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Listings new since `since` (compared against `scraped_at`, the
    first-seen timestamp that never changes on a repeat sighting -- NOT
    `last_seen_at`, which is bumped on every ingest touch including an
    unchanged still-open listing re-sent by push_to_hub()'s full-snapshot
    push every cycle; using last_seen_at here would match the entire
    active listing set on every single run). Takes an existing connection
    (unlike query_listings) since send_alerts.py calls this once per
    subscriber in a loop and shouldn't reopen/re-migrate the DB each time.

    Keyword matching: keywords OR together (any match), each checked
    against both title and description; exclude_keywords NOT together
    (none may match). category/segment are exact matches (closed
    vocabulary from companies.yaml); company is a LIKE match (free text,
    e.g. "Nike" should match "Nike, Inc.").
    """
    clauses, params = ["active = 1"], []
    if since:
        clauses.append("scraped_at > ?")
        params.append(since)

    if keywords:
        keywords = keywords[:MAX_ALERT_KEYWORDS]
        or_parts = []
        for kw in keywords:
            kw = kw[:MAX_ALERT_KEYWORD_LENGTH]
            or_parts.append("(title LIKE ? OR description LIKE ?)")
            params += [f"%{kw}%", f"%{kw}%"]
        clauses.append(f"({' OR '.join(or_parts)})")

    if exclude_keywords:
        for kw in exclude_keywords[:MAX_ALERT_KEYWORDS]:
            kw = kw[:MAX_ALERT_KEYWORD_LENGTH]
            clauses.append("title NOT LIKE ? AND description NOT LIKE ?")
            params += [f"%{kw}%", f"%{kw}%"]

    if location_mode == "remote":
        clauses.append("location LIKE '%remote%'")
    elif location_mode:
        clauses.append("location LIKE ?")
        params.append(f"%{location_mode}%")

    if category:
        clauses.append("category = ?")
        params.append(category)
    if segment:
        clauses.append("segment = ?")
        params.append(segment)
    if company:
        clauses.append("company LIKE ?")
        params.append(f"%{company}%")

    rows = conn.execute(
        f"SELECT {', '.join(OUTPUT_FIELDS)} FROM listings WHERE {' AND '.join(clauses)} "
        "ORDER BY scraped_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]
