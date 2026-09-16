import sqlite3

# Same lightweight column-migration convention as job-radar's own
# db/migrations.py -- SQLite's ALTER TABLE ADD COLUMN has no IF NOT EXISTS,
# so check pragma table_info() ourselves before adding a column. Empty for
# now (schema.sql covers the initial shape); add future columns here rather
# than editing schema.sql once real data exists.
NEW_LISTING_COLUMNS: dict[str, str] = {}
NEW_API_KEY_COLUMNS: dict[str, str] = {}


def _ensure_table_columns(conn: sqlite3.Connection, table: str, new_columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, col_type in new_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def ensure_columns(conn: sqlite3.Connection) -> None:
    # WAL instead of the default rollback journal: the ingest writer and
    # read-API requests can happen concurrently, a writer shouldn't block readers.
    conn.execute("PRAGMA journal_mode=WAL")

    _ensure_table_columns(conn, "listings", NEW_LISTING_COLUMNS)
    _ensure_table_columns(conn, "api_keys", NEW_API_KEY_COLUMNS)
    conn.commit()
