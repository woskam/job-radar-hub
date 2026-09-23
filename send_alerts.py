#!/usr/bin/env python3
"""
Daily digest sender for the email-alerts feature -- run via
systemd/job-radar-hub-alerts.timer (oneshot, same shape as job-radar's own
job-radar.timer). For every active subscriber, finds listings scraped
since their last digest (or since they signed up, if never sent), sends a
digest through Resend if there's anything to report, and advances
last_sent_at. Also purges subscribers stuck in 'pending' (never confirmed)
for more than a week, to keep this hub's one personal-data table small.

One DB connection for the whole run (get_db() re-runs schema.sql +
migrations + a commit on every call -- wasteful in a per-subscriber loop).
last_sent_at is committed right after each individual send, not batched at
the end, so a mid-run crash or Resend outage only affects subscribers not
yet reached, not ones already emailed -- unreached subscribers are simply
caught by tomorrow's run.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from alerts import digest_email_html, send_email
from db.queries import get_db, query_new_listings_for_alert

HUB_PUBLIC_BASE_URL = os.environ.get("HUB_PUBLIC_BASE_URL", "http://localhost:5300")
PENDING_EXPIRY_DAYS = 7
SEND_PACING_SECONDS = 0.5  # Resend's rate limit -- avoid a burst of 429s mid-run.


def purge_stale_pending(conn) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PENDING_EXPIRY_DAYS)).isoformat()
    cur = conn.execute("DELETE FROM subscribers WHERE status = 'pending' AND created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def run() -> dict:
    conn = get_db()
    purged = purge_stale_pending(conn)

    subscribers = conn.execute("SELECT * FROM subscribers WHERE status = 'active'").fetchall()
    sent, skipped_empty, failed = 0, 0, 0

    for sub in subscribers:
        since = sub["last_sent_at"] or sub["created_at"]
        listings = query_new_listings_for_alert(
            conn,
            keywords=json.loads(sub["keywords"] or "[]"),
            exclude_keywords=json.loads(sub["exclude_keywords"] or "[]"),
            location_mode=sub["location_mode"],
            category=sub["category"],
            segment=sub["segment"],
            company=sub["company"],
            since=since,
        )

        now = datetime.now(timezone.utc).isoformat()
        if not listings:
            skipped_empty += 1
            # Still advance last_sent_at -- nothing to report, and doing so
            # keeps `since` from growing into an ever-larger lookback window.
            conn.execute("UPDATE subscribers SET last_sent_at = ? WHERE id = ?", (now, sub["id"]))
            conn.commit()
            continue

        html = digest_email_html(HUB_PUBLIC_BASE_URL, sub["unsubscribe_token"], listings)
        result = send_email(sub["email"], f"{len(listings)} new job listing(s) for you", html)
        if result == "sent":
            sent += 1
            conn.execute("UPDATE subscribers SET last_sent_at = ? WHERE id = ?", (now, sub["id"]))
            conn.commit()
        elif result == "rate_limited":
            # Stop the whole run here rather than burn through every
            # remaining subscriber into more 429s -- none of them (this one
            # included) get last_sent_at advanced, so all are simply picked
            # up again on tomorrow's run.
            print(f"[send_alerts] rate-limited by Resend, stopping run ({sent} sent so far)")
            break
        else:
            failed += 1
            # Don't advance last_sent_at on a failed send -- these listings
            # get picked up again on tomorrow's run instead of silently lost.
            print(f"[send_alerts] send failed for subscriber {sub['id']}, will retry tomorrow")

        time.sleep(SEND_PACING_SECONDS)

    conn.close()
    result = {"subscribers": len(subscribers), "sent": sent, "skipped_empty": skipped_empty, "failed": failed, "purged_pending": purged}
    print(result)
    return result


if __name__ == "__main__":
    run()
