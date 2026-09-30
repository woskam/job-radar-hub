import sqlite3

# Same lightweight column-migration convention as job-radar's own
# db/migrations.py -- SQLite's ALTER TABLE ADD COLUMN has no IF NOT EXISTS,
# so check pragma table_info() ourselves before adding a column. Empty for
# now (schema.sql covers the initial shape); add future columns here rather
# than editing schema.sql once real data exists.
NEW_LISTING_COLUMNS: dict[str, str] = {
    # Sourced from companies.yaml on the producer side (see job-radar's
    # scheduler.py::push_to_hub) -- category is free-text (e.g. "fintech"),
    # segment is "startup" or NULL/absent (the established-company default).
    # Existing rows get these backfilled automatically within one ingest
    # cycle (push_to_hub resends the whole open-listings snapshot every
    # time, and /ingest is an upsert), no separate backfill script needed.
    "category": "TEXT",
    "segment": "TEXT",
}
NEW_API_KEY_COLUMNS: dict[str, str] = {
    # Existing rows on an already-deployed hub predate this column and
    # predate storing a hash in `key` -- if any exist, they're revoked here
    # rather than silently left holding a plaintext key that no longer
    # round-trips through lookup_api_key's hashing. See app.py/manage.py.
    "key_preview": "TEXT",
}


def _ensure_table_columns(conn: sqlite3.Connection, table: str, new_columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, col_type in new_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def _revoke_pre_hash_keys(conn: sqlite3.Connection) -> None:
    # One-time cleanup for a hub deployed before api_keys.key started storing
    # sha256(raw key) instead of the raw key: such a row's `key` value will
    # never again equal any real caller's hashed token (lookup_api_key hashes
    # before comparing), so it's already unusable -- this just makes that
    # explicit in `revoked` instead of leaving a row that looks active but
    # silently never authenticates. key_preview IS NULL identifies these
    # (every row created after this migration always gets one).
    conn.execute("UPDATE api_keys SET revoked = 1 WHERE key_preview IS NULL AND revoked = 0")


# Old subscribers.status -> (new subscribers.status, new saved_searches.status).
# 'unsubscribed' never actually occurs in production today (checked live,
# 2026-09-30 -- both real subscribers are 'active'), but is mapped
# correctly for completeness: they did confirm their email at some point
# (subscriber stays active), it was this one search they opted out of
# (search becomes unsubscribed), matching the new per-search opt-out model.
_STATUS_SPLIT = {
    "active": ("active", "active"),
    "pending": ("pending", "pending"),
    "unsubscribed": ("active", "unsubscribed"),
}


def _split_inline_searches_into_saved_searches(conn: sqlite3.Connection) -> None:
    # One-time: subscribers used to hold exactly one search's filters
    # inline. Each such row (still non-NULL keywords -- the "not yet
    # migrated" signal, since the app never writes there anymore once
    # migrated) becomes one saved_searches row, reusing the *same*
    # unsubscribe_token -- a fresh one would silently break any
    # unsubscribe link already sitting in a previously-sent digest email.
    rows = conn.execute(
        """
        SELECT s.id, s.status, s.unsubscribe_token, s.keywords, s.exclude_keywords,
               s.location_mode, s.category, s.segment, s.company, s.last_sent_at, s.created_at
        FROM subscribers s
        WHERE s.keywords IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM saved_searches WHERE subscriber_id = s.id)
        """
    ).fetchall()

    for row in rows:
        new_subscriber_status, new_search_status = _STATUS_SPLIT.get(row["status"], ("active", "active"))
        conn.execute(
            """
            INSERT INTO saved_searches
                (subscriber_id, status, unsubscribe_token, keywords, exclude_keywords,
                 location_mode, category, segment, company, last_sent_at, created_at)
            VALUES (:subscriber_id, :status, :unsubscribe_token, :keywords, :exclude_keywords,
                    :location_mode, :category, :segment, :company, :last_sent_at, :created_at)
            """,
            {
                "subscriber_id": row["id"], "status": new_search_status, "unsubscribe_token": row["unsubscribe_token"],
                "keywords": row["keywords"], "exclude_keywords": row["exclude_keywords"],
                "location_mode": row["location_mode"], "category": row["category"], "segment": row["segment"],
                "company": row["company"], "last_sent_at": row["last_sent_at"], "created_at": row["created_at"],
            },
        )
        conn.execute(
            "UPDATE subscribers SET status = ?, keywords = NULL, exclude_keywords = NULL, "
            "location_mode = NULL, category = NULL, segment = NULL, company = NULL, last_sent_at = NULL "
            "WHERE id = ?",
            (new_subscriber_status, row["id"]),
        )


def ensure_columns(conn: sqlite3.Connection) -> None:
    # WAL instead of the default rollback journal: the ingest writer and
    # read-API requests can happen concurrently, a writer shouldn't block readers.
    conn.execute("PRAGMA journal_mode=WAL")
    # Needed for saved_searches' ON DELETE CASCADE (schema.sql) to actually
    # fire -- SQLite silently ignores ON DELETE CASCADE unless this is on.
    conn.execute("PRAGMA foreign_keys = ON")

    _ensure_table_columns(conn, "listings", NEW_LISTING_COLUMNS)
    _ensure_table_columns(conn, "api_keys", NEW_API_KEY_COLUMNS)
    _revoke_pre_hash_keys(conn)
    _split_inline_searches_into_saved_searches(conn)
    conn.commit()
