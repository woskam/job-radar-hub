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

import hmac
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_limiter import Limiter

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from db.queries import LISTING_FIELDS, get_db, lookup_api_key, query_listings

HUB_PUSH_TOKEN = os.environ.get("HUB_PUSH_TOKEN")

app = Flask(__name__)


def _bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""


def _jobs_rate_limit() -> str:
    # Cache the lookup on g -- this callable and the view function run in
    # the same request context, so the view can reuse it via g.api_key_row
    # instead of hitting the DB twice.
    g.api_key_row = lookup_api_key(_bearer_token())
    limit = g.api_key_row["rate_limit_per_hour"] if g.api_key_row else 10
    return f"{limit} per hour"


limiter = Limiter(key_func=_bearer_token, app=app, storage_uri="memory://")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/ingest", methods=["POST"])
def ingest():
    if not HUB_PUSH_TOKEN or not hmac.compare_digest(_bearer_token(), HUB_PUSH_TOKEN):
        return jsonify({"error": "invalid or missing push token"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object with scraped_ok and listings"}), 400

    # scraped_ok: which (source, company) pairs this push is authoritative
    # for -- previously a push carried no such scope, so "not in this
    # payload" was the only staleness signal, and a producer always sent
    # its entire table (every job it had ever seen), so nothing was ever
    # NOT in the payload and nothing ever went stale. Scoping deactivation
    # to this list means a push only speaks for what it actually covers --
    # a company whose scrape failed upstream is simply absent here, and its
    # existing listings are left untouched, not wiped.
    scraped_ok = payload.get("scraped_ok")
    if not isinstance(scraped_ok, list) or not scraped_ok:
        return jsonify({"error": "scraped_ok must be a non-empty list; refusing to change liveness for no coverage"}), 400
    coverage = [
        (pair.get("source"), pair.get("company"))
        for pair in scraped_ok
        if isinstance(pair, dict) and pair.get("source") and pair.get("company")
    ]
    if not coverage:
        return jsonify({"error": "scraped_ok contained no valid {source, company} pairs"}), 400

    listings = payload.get("listings")
    if not isinstance(listings, list):
        return jsonify({"error": "listings must be a list (may be empty)"}), 400

    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    upserted = 0
    for item in listings:
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
        upserted += 1

    # Deactivate only within what this push actually covers -- not "anything
    # not in this exact payload" (that was the bug: a producer sending its
    # whole table every time meant this condition could never be true).
    marked_stale = 0
    for source, company in coverage:
        cur = conn.execute(
            "UPDATE listings SET active = 0 WHERE source = ? AND company = ? AND last_seen_at < ? AND active = 1",
            (source, company, now),
        )
        marked_stale += cur.rowcount
    conn.commit()
    conn.close()

    return jsonify({"received": len(listings), "upserted": upserted, "marked_stale": marked_stale})


@app.route("/jobs")
@limiter.limit(_jobs_rate_limit)
def jobs():
    if not g.get("api_key_row"):
        return jsonify({"error": "invalid or missing API key"}), 401

    results = query_listings(
        company=request.args.get("company"),
        source=request.args.get("source"),
        title_contains=request.args.get("title_contains"),
        location_contains=request.args.get("location_contains"),
        active=request.args.get("active", "1") not in ("0", "false"),
        limit=int(request.args.get("limit", 50)),
        offset=int(request.args.get("offset", 0)),
    )
    return jsonify({"count": len(results), "results": results})


if __name__ == "__main__":
    # Local dev only -- the systemd unit runs this through gunicorn instead
    # (Flask's built-in server isn't meant for real traffic).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5300)))
