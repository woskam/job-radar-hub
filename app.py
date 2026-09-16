"""
Job Radar Hub -- a public, tokengated read API for generic job-listing data,
fed by a push from a private Job Radar instance's own scrape cycle (see
README.md for the ingest contract). Deliberately carries no personal data:
no relevance score, no application status, no letters/CVs -- those stay on
whichever instance pushes into this hub. This hub never sends anything to
an employer; it only serves listing data out to whoever holds a valid API
key. What a consumer does with that data (draft, apply, whatever) is
entirely their own responsibility.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_limiter import Limiter

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from db.migrations import ensure_columns

DB_PATH = Path(os.environ.get("HUB_DB_PATH", ROOT / "db" / "hub.db"))
SCHEMA_PATH = ROOT / "db" / "schema.sql"
HUB_PUSH_TOKEN = os.environ.get("HUB_PUSH_TOKEN")

LISTING_FIELDS = ["source", "external_id", "title", "company", "location", "url", "description", "scraped_at"]

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    ensure_columns(conn)
    return conn


def _bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""


def _lookup_api_key(key: str) -> sqlite3.Row | None:
    if not key:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM api_keys WHERE key = ? AND revoked = 0", (key,)).fetchone()
    conn.close()
    return row


def _jobs_rate_limit() -> str:
    # Cache the lookup on g -- this callable and the view function run in
    # the same request context, so the view can reuse it via g.api_key_row
    # instead of hitting the DB twice.
    g.api_key_row = _lookup_api_key(_bearer_token())
    limit = g.api_key_row["rate_limit_per_hour"] if g.api_key_row else 10
    return f"{limit} per hour"


limiter = Limiter(key_func=_bearer_token, app=app, storage_uri="memory://")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/ingest", methods=["POST"])
def ingest():
    if not HUB_PUSH_TOKEN or _bearer_token() != HUB_PUSH_TOKEN:
        return jsonify({"error": "invalid or missing push token"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        return jsonify({"error": "expected a JSON array of listings"}), 400

    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    inserted_or_updated = 0
    for item in payload:
        if not isinstance(item, dict) or not item.get("source") or not item.get("external_id"):
            continue  # skip malformed entries rather than failing the whole batch
        values = {field: item.get(field) for field in LISTING_FIELDS}
        conn.execute(
            """
            INSERT INTO listings (source, external_id, title, company, location, url, description, scraped_at, last_seen_at, active)
            VALUES (:source, :external_id, :title, :company, :location, :url, :description, :scraped_at, :now, 1)
            ON CONFLICT (source, external_id) DO UPDATE SET
                title = excluded.title, company = excluded.company, location = excluded.location,
                url = excluded.url, description = excluded.description, scraped_at = excluded.scraped_at,
                last_seen_at = excluded.last_seen_at, active = 1
            """,
            {**values, "now": now},
        )
        inserted_or_updated += 1

    # Anything not touched by this push is no longer live -- each push is
    # authoritative for "what's currently live", same as the source scrape
    # cycle itself treats a fresh scrape as authoritative.
    cur = conn.execute("UPDATE listings SET active = 0 WHERE last_seen_at < ? AND active = 1", (now,))
    marked_stale = cur.rowcount
    conn.commit()
    conn.close()

    return jsonify({"received": len(payload), "upserted": inserted_or_updated, "marked_stale": marked_stale})


@app.route("/jobs")
@limiter.limit(_jobs_rate_limit)
def jobs():
    if not g.get("api_key_row"):
        return jsonify({"error": "invalid or missing API key"}), 401

    conn = get_db()
    clauses, params = ["active = ?"], [request.args.get("active", "1") not in ("0", "false")]
    for field, param in (("company", "company"), ("source", "source")):
        value = request.args.get(param)
        if value:
            clauses.append(f"{field} = ?")
            params.append(value)
    for field, param in (("title", "title_contains"), ("location", "location_contains")):
        value = request.args.get(param)
        if value:
            clauses.append(f"{field} LIKE ?")
            params.append(f"%{value}%")

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)
    rows = conn.execute(
        f"SELECT {', '.join(LISTING_FIELDS)} FROM listings WHERE {' AND '.join(clauses)} "
        "ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return jsonify({"count": len(rows), "results": [dict(r) for r in rows]})


if __name__ == "__main__":
    # Local dev only -- the systemd unit runs this through gunicorn instead
    # (Flask's built-in server isn't meant for real traffic).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5300)))
