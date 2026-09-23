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


def ensure_columns(conn: sqlite3.Connection) -> None:
    # WAL instead of the default rollback journal: the ingest writer and
    # read-API requests can happen concurrently, a writer shouldn't block readers.
    conn.execute("PRAGMA journal_mode=WAL")

    _ensure_table_columns(conn, "listings", NEW_LISTING_COLUMNS)
    _ensure_table_columns(conn, "api_keys", NEW_API_KEY_COLUMNS)
    _revoke_pre_hash_keys(conn)
    conn.commit()
