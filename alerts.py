"""
Shared logic for the email-alerts feature -- token generation, Resend
sending, and email templates -- used by both app.py (the subscribe/
confirm/unsubscribe endpoints) and send_alerts.py (the daily digest
sender), so the two can't drift on what a valid subscription/email looks
like.
"""
import os
import secrets

import requests

from db.queries import MAX_ALERT_KEYWORD_LENGTH, MAX_ALERT_KEYWORDS

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# onboarding@resend.dev works with no domain verification at all, but only
# ever delivers to the Resend account's own verified email -- fine for
# building/testing, not for real subscribers. Swap once a domain is
# verified (see README.md).
ALERTS_FROM_EMAIL = os.environ.get("ALERTS_FROM_EMAIL", "onboarding@resend.dev")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def clean_keyword_list(raw) -> list[str]:
    """Validates+caps an incoming JSON value into a plain list[str] --
    called on untrusted input from the public POST /alerts/subscribe
    endpoint, so this is the one place list size/item length get enforced
    before anything downstream (the DB, later the WHERE clause in
    db/queries.py::query_new_listings_for_alert) ever sees it."""
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw[:MAX_ALERT_KEYWORDS]:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip()[:MAX_ALERT_KEYWORD_LENGTH])
    return cleaned


def send_email(to: str, subject: str, html: str) -> str:
    """Best-effort -- a Resend outage/misconfiguration must never crash the
    caller (same spirit as job-radar's own scheduler.py::_safe_fetch).
    Returns "sent", "failed", or "rate_limited" -- send_alerts.py treats
    the last one specially (stop the whole run rather than burn through
    every remaining subscriber into more 429s); app.py's confirm-email
    send doesn't care about the distinction and can ignore the return."""
    if not RESEND_API_KEY:
        print(f"[alerts] RESEND_API_KEY not set, skipping send to {to}")
        return "failed"
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": ALERTS_FROM_EMAIL, "to": to, "subject": subject, "html": html},
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        print(f"[alerts] Resend unreachable sending to {to}: {exc}")
        return "failed"
    if resp.status_code == 429:
        print(f"[alerts] Resend rate-limited (429) sending to {to}")
        return "rate_limited"
    if resp.status_code >= 400:
        print(f"[alerts] Resend error {resp.status_code} sending to {to}: {resp.text[:300]}")
        return "failed"
    return "sent"


def confirm_email_html(base_url: str, token: str) -> str:
    link = f"{base_url}/alerts/confirm/{token}"
    return (
        "<p>Confirm your Job Radar alert subscription:</p>"
        f'<p><a href="{link}">{link}</a></p>'
        "<p>If you didn't request this, ignore this email -- nothing further will be sent.</p>"
    )


def digest_email_html(base_url: str, unsubscribe_token: str, listings: list[dict]) -> str:
    items = "".join(
        f'<li><a href="{l["url"]}">{l["title"]}</a> &mdash; {l["company"]}'
        f'{" (" + l["location"] + ")" if l.get("location") else ""}</li>'
        for l in listings
    )
    unsub_link = f"{base_url}/alerts/unsubscribe/{unsubscribe_token}"
    return (
        f"<p>{len(listings)} new listing(s) matching your saved search:</p>"
        f"<ul>{items}</ul>"
        '<p style="color:#888;font-size:12px">'
        f'<a href="{unsub_link}">Unsubscribe</a> from these alerts.</p>'
    )
