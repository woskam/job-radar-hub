"""
Job Radar Hub -- a public, tokengated read API for generic job-listing data,
fed by a push from a private Job Radar instance's own scrape cycle (see
README.md for the ingest contract). The listings themselves carry no
personal data: no relevance score, no application status, no letters/CVs
-- those stay on whichever instance pushes into this hub. This hub never
sends anything to an employer; it only serves listing data out to whoever
holds a valid API key. What a consumer does with that data (draft, apply,
whatever) is entirely their own responsibility.

The one exception to "no personal data" is the email-alerts feature (see
README.md) -- subscribers.email is real PII, double-opt-in and
self-service unsubscribe throughout, see alerts.py/send_alerts.py.
"""

import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from alerts import clean_keyword_list, confirm_email_html, new_token, render_subscribe_form, send_email
from db.queries import LISTING_FIELDS, get_db, lookup_api_key, query_listings

HUB_PUSH_TOKEN = os.environ.get("HUB_PUSH_TOKEN")
# The Hub's own externally-reachable base URL, for confirm/unsubscribe
# links in emails -- falls back to request.host_url, which works fine when
# job-radar-site's relay Function hits this app directly by IP:port (see
# functions/api/alerts-subscribe.js), but an explicit override avoids any
# surprise if something else ever proxies requests here differently.
HUB_PUBLIC_BASE_URL = os.environ.get("HUB_PUBLIC_BASE_URL")

app = Flask(__name__)


def _bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""


def _base_url() -> str:
    return HUB_PUBLIC_BASE_URL or request.host_url.rstrip("/")


def _alert_client_ip() -> str:
    # job-radar-site's relay Function (mixed-content workaround -- the site
    # is HTTPS, this hub is plain HTTP, see README.md) forwards the real
    # visitor's IP via this header, since otherwise every relayed request
    # would show Cloudflare's edge IP here. Best-effort only: a direct hit
    # on this endpoint (it's public, no auth) can set this header to
    # anything, so it's not a security boundary, just makes the rate limit
    # meaningful for traffic that actually came through the site.
    return request.headers.get("CF-Connecting-IP") or get_remote_address()


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
            INSERT INTO listings (source, external_id, title, company, location, url, description, scraped_at, category, segment, last_seen_at, active)
            VALUES (:source, :external_id, :title, :company, :location, :url, :description, :scraped_at, :category, :segment, :now, 1)
            ON CONFLICT (source, external_id) DO UPDATE SET
                title = excluded.title, company = excluded.company, location = excluded.location,
                url = excluded.url, description = excluded.description, scraped_at = excluded.scraped_at,
                category = excluded.category, segment = excluded.segment,
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


_GENERIC_SUBSCRIBE_RESPONSE = {"message": "If that's a valid email, check your inbox to confirm."}


def _create_pending_subscriber(payload: dict) -> str | None:
    """Shared by both /alerts/subscribe (JSON, for any client that can
    reach this hub directly) and /alerts (HTML form, for job-radar-site --
    see its own comment for why it needs a same-origin plain-HTML form
    instead of calling the JSON endpoint). Returns an error message if the
    input was invalid, else None -- honeypot/throttle hits also return
    None (same outward behavior as a real signup, deliberately not
    distinguishable by the caller/response)."""
    if payload.get("website"):
        return None  # honeypot -- a real visitor never fills a hidden field

    email = (payload.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return "a valid email is required"

    keywords = clean_keyword_list(payload.get("keywords"))
    exclude_keywords = clean_keyword_list(payload.get("exclude_keywords"))
    location_mode = (payload.get("location_mode") or "").strip()[:200] or None
    category = (payload.get("category") or "").strip()[:100] or None
    segment = (payload.get("segment") or "").strip()[:100] or None
    company = (payload.get("company") or "").strip()[:200] or None

    conn = get_db()

    # Per-target-email throttle -- the real abuse case here is emailing a
    # stranger's confirm-link over and over, which is orthogonal to
    # requester IP (the IP-based limiter on the callers only slows down one
    # abusive caller, not someone rotating IPs to spam a single victim).
    row = conn.execute("SELECT last_confirm_sent_at FROM subscribers WHERE email = ?", (email,)).fetchone()
    if row and row["last_confirm_sent_at"]:
        last_sent = datetime.fromisoformat(row["last_confirm_sent_at"])
        if datetime.now(timezone.utc) - last_sent < timedelta(hours=1):
            conn.close()
            return None

    now = datetime.now(timezone.utc).isoformat()
    confirm_token = new_token()
    unsubscribe_token = new_token()
    conn.execute(
        """
        INSERT INTO subscribers
            (email, status, confirm_token, unsubscribe_token, last_confirm_sent_at,
             keywords, exclude_keywords, location_mode, category, segment, company, created_at)
        VALUES (:email, 'pending', :confirm_token, :unsubscribe_token, :now,
                :keywords, :exclude_keywords, :location_mode, :category, :segment, :company, :now)
        ON CONFLICT (email) DO UPDATE SET
            status = 'pending', confirm_token = excluded.confirm_token,
            unsubscribe_token = excluded.unsubscribe_token, last_confirm_sent_at = excluded.last_confirm_sent_at,
            keywords = excluded.keywords, exclude_keywords = excluded.exclude_keywords,
            location_mode = excluded.location_mode, category = excluded.category,
            segment = excluded.segment, company = excluded.company
        """,
        {
            "email": email, "confirm_token": confirm_token, "unsubscribe_token": unsubscribe_token, "now": now,
            "keywords": json.dumps(keywords), "exclude_keywords": json.dumps(exclude_keywords),
            "location_mode": location_mode, "category": category, "segment": segment, "company": company,
        },
    )
    conn.commit()
    conn.close()

    send_email(email, "Confirm your Job Radar alert", confirm_email_html(_base_url(), confirm_token))
    return None


@app.route("/alerts/subscribe", methods=["POST"])
@limiter.limit("5 per hour", key_func=_alert_client_ip)
def alerts_subscribe():
    # Small, explicit cap on this one public route -- there's no app-wide
    # MAX_CONTENT_LENGTH (would risk breaking /ingest's much larger
    # payloads), but a subscribe body is a handful of short strings and
    # never needs more than a few KB.
    if request.content_length and request.content_length > 8192:
        return jsonify({"error": "payload too large"}), 413

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object"}), 400

    error = _create_pending_subscriber(payload)
    if error:
        return jsonify({"error": error}), 400
    # Same response whether the email was new, already subscribed, or the
    # send failed -- never turn this into an email-existence oracle.
    return jsonify(_GENERIC_SUBSCRIBE_RESPONSE)


@app.route("/alerts", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"], key_func=_alert_client_ip)
def alerts_form():
    # A plain, same-origin HTML form -- not JSON via fetch() -- because
    # job-radar-site is HTTPS and this hub is still plain HTTP (no domain
    # yet for a real cert). A same-page fetch()/JS POST from an HTTPS page
    # to a plain-HTTP target is blocked by the browser's mixed-content
    # rules, and routing it through a Cloudflare Pages Function relay
    # doesn't work either -- Cloudflare Workers' fetch() refuses any
    # destination that resolves to a raw IP address outright (confirmed
    # live: error 1003 "Direct IP Access Not Allowed", even via a
    # nip.io-style hostname trick, since Cloudflare inspects the resolved
    # connection target, not the hostname string). A normal top-level link
    # from job-radar-site to this page isn't subject to either
    # restriction, so the form lives here instead, same-origin start to
    # finish. Swap job-radar-site's /alerts page to embed/redirect here
    # directly, or just move the whole form back once this hub has a real
    # domain + TLS.
    if request.content_length and request.content_length > 8192:
        return ("payload too large", 413)

    message = None
    if request.method == "POST":
        payload = {
            "email": request.form.get("email", ""),
            "keywords": [s.strip() for s in request.form.get("keywords", "").split(",") if s.strip()],
            "exclude_keywords": [s.strip() for s in request.form.get("exclude_keywords", "").split(",") if s.strip()],
            "location_mode": "remote" if request.form.get("remote_only") == "on" else (request.form.get("location", "").strip() or None),
            "category": request.form.get("category") or None,
            "segment": request.form.get("segment") or None,
            "company": request.form.get("company", "").strip() or None,
            "website": request.form.get("website", ""),
        }
        error = _create_pending_subscriber(payload)
        message = error or "Check your inbox to confirm your subscription."

    return render_subscribe_form(message)


@app.route("/alerts/confirm/<token>")
def alerts_confirm(token):
    conn = get_db()
    cur = conn.execute("UPDATE subscribers SET status = 'active' WHERE confirm_token = ? AND status != 'unsubscribed'", (token,))
    conn.commit()
    conn.close()
    if cur.rowcount:
        return "Subscribed -- you'll get an email when new matching listings appear."
    return ("That confirmation link is invalid or has already been used.", 404)


@app.route("/alerts/unsubscribe/<token>")
def alerts_unsubscribe(token):
    conn = get_db()
    cur = conn.execute("UPDATE subscribers SET status = 'unsubscribed' WHERE unsubscribe_token = ?", (token,))
    conn.commit()
    conn.close()
    if cur.rowcount:
        return "Unsubscribed -- you won't get any more alert emails."
    return ("That unsubscribe link is invalid.", 404)


if __name__ == "__main__":
    # Local dev only -- the systemd unit runs this through gunicorn instead
    # (Flask's built-in server isn't meant for real traffic).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5300)))
