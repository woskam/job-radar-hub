"""
Shared logic for the email-alerts feature -- token generation, Resend
sending, and email templates -- used by both app.py (the subscribe/
confirm/unsubscribe endpoints) and send_alerts.py (the daily digest
sender), so the two can't drift on what a valid subscription/email looks
like.
"""
import os
import secrets
from html import escape

import requests

from db.queries import MAX_ALERT_KEYWORD_LENGTH, MAX_ALERT_KEYWORDS

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# onboarding@resend.dev works with no domain verification at all, but only
# ever delivers to the Resend account's own verified email -- fine for
# building/testing, not for real subscribers. Swap once a domain is
# verified (see README.md). Resend's `from` field accepts a plain address
# or "Display Name <address>" -- once a domain is verified, set this to
# e.g. "Alerts - 12getajob.com <alerts@12getajob.com>" so it doesn't show
# as a bare address in the recipient's inbox.
ALERTS_FROM_EMAIL = os.environ.get("ALERTS_FROM_EMAIL", "onboarding@resend.dev")

OSS_FOOTER_HTML = (
    '<p style="color:#888;font-size:12px;margin-top:24px;border-top:1px solid #e2e2e2;padding-top:12px;">'
    "12GetAJob is built on open source -- "
    '<a href="https://github.com/woskam/job-radar">job-radar</a> and '
    '<a href="https://github.com/woskam/job-radar-hub">job-radar-hub</a>. '
    "Self-host it yourself, or contribute on GitHub.</p>"
)


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


CATEGORIES = [
    "ai_ml", "b2b", "banking", "beauty", "climate", "consulting", "consumer", "crypto", "devtools",
    "ecommerce", "education", "enterprise", "fashion", "fintech", "fmcg", "government", "healthcare",
    "healthtech", "industrials", "insurance", "marketplace", "mobility", "other", "overheid", "pharma",
    "real_estate_and_construction", "resilience", "saas", "semiconductor", "sportswear", "staffing",
    "tech", "telecom",
]

_FORM_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>12GetAJob Alerts</title>
<meta name="robots" content="noindex">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 560px; margin: 48px auto; padding: 0 20px; color: #1a1a1a; }}
h1 {{ font-size: 28px; }}
p.tagline {{ color: #5a5a5a; }}
input, select, button {{ font: inherit; font-size: 14px; padding: 8px 10px; border: 1px solid #d0d0d0; border-radius: 6px; }}
input, select {{ width: 100%; box-sizing: border-box; margin-bottom: 10px; }}
label.checkbox {{ display: flex; align-items: center; gap: 6px; font-size: 14px; margin-bottom: 10px; }}
label.checkbox input {{ width: auto; margin: 0; }}
button {{ background: #1e5a8a; color: #fff; border: none; cursor: pointer; font-weight: 600; }}
.row {{ display: flex; gap: 8px; }}
.row > * {{ flex: 1; }}
.message {{ padding: 10px 14px; border-radius: 6px; background: #f4f4f4; margin-bottom: 16px; }}
.honeypot {{ position: absolute; left: -9999px; }}
a {{ color: #1e5a8a; }}
</style>
</head>
<body>
<p><a href="https://12getajob.com/">&larr; 12GetAJob</a></p>
<h1>Email alerts</h1>
<p class="tagline">Save a search, get a daily email when new matching listings appear. No account, no password -- just an email and an unsubscribe link in every message. Already subscribed? Submit another search with the same email -- no need to confirm again.</p>
{message_html}
<form method="POST">
  <input type="email" name="email" placeholder="you@example.com" required>
  <input type="text" name="keywords" placeholder="Keywords, comma-separated (e.g. backend, platform engineer)">
  <input type="text" name="exclude_keywords" placeholder="Exclude keywords, comma-separated (e.g. senior, intern)">
  <div class="row">
    <input type="text" name="location" placeholder="Location (e.g. Amsterdam)">
    <label class="checkbox"><input type="checkbox" name="remote_only"> Remote only</label>
  </div>
  <div class="row">
    <select name="category"><option value="">Any category</option>{category_options}</select>
    <select name="segment"><option value="">Any segment</option><option value="startup">Startups &amp; scale-ups only</option></select>
    <input type="text" name="company" placeholder="Company (optional)">
  </div>
  <input class="honeypot" type="text" name="website" tabindex="-1" autocomplete="off">
  <button type="submit">Subscribe</button>
</form>
<p style="font-size:12px;color:#888;margin-top:24px;"><a href="https://12getajob.com/privacy">Privacy</a></p>
</body>
</html>
"""


def render_subscribe_form(message: str | None = None) -> str:
    category_options = "".join(f'<option value="{c}">{c}</option>' for c in CATEGORIES)
    message_html = f'<p class="message">{message}</p>' if message else ""
    return _FORM_TEMPLATE.format(category_options=category_options, message_html=message_html)


def confirm_email_html(base_url: str, token: str) -> str:
    link = f"{base_url}/alerts/confirm/{token}"
    return (
        "<p>Confirm your 12GetAJob alert subscription:</p>"
        f'<p><a href="{link}">{link}</a></p>'
        "<p>If you didn't request this, ignore this email -- nothing further will be sent.</p>"
        + OSS_FOOTER_HTML
    )


def _format_location(location: str | None) -> str:
    """"Amsterdam, NL; Berlin, DE; Hannover, DE; ..." -> "Amsterdam, NL
    +2 more locations" -- a multi-location listing would otherwise dump
    every variant onto one line (a real one has shown up with 10 cities
    across 8 countries), making the digest unreadable. Only the first
    location is a link target's actual destination anyway (job.url points
    at one ATS posting), so showing the rest as a plain count loses
    nothing a reader could act on."""
    if not location:
        return ""
    parts = [p.strip() for p in location.split(";") if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return escape(parts[0])
    extra = len(parts) - 1
    noun = "location" if extra == 1 else "locations"
    return f'{escape(parts[0])} <span style="color:#9b99a5;">+{extra} more {noun}</span>'


def digest_email_html(base_url: str, unsubscribe_token: str, listings: list[dict]) -> str:
    cards = "".join(
        '<div style="border:1px solid #efeef3;border-radius:10px;padding:14px 16px;margin:0 0 10px;">'
        f'<a href="{escape(l["url"])}" style="display:block;font-size:15px;font-weight:600;'
        f'color:#0e0e12;text-decoration:none;line-height:1.4;">{escape(l["title"])}</a>'
        f'<div style="margin-top:4px;font-size:13px;color:#6e6c78;">{escape(l["company"])}'
        f'{" &middot; " + _format_location(l.get("location")) if l.get("location") else ""}</div>'
        "</div>"
        for l in listings
    )
    unsub_link = f"{base_url}/alerts/unsubscribe/{unsubscribe_token}"
    count = len(listings)
    return (
        # Table-based, every style inline -- same reasoning as the plain
        # /alerts form page (see _FORM_TEMPLATE): this has to render
        # correctly in Gmail/Outlook/Apple Mail, none of which reliably
        # support a <style> block, flexbox, or grid.
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f3f7;padding:24px 0;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;background:#ffffff;border:1px solid #efeef3;border-radius:16px;'
        'overflow:hidden;">'
        '<tr><td style="padding:26px 28px 20px;border-bottom:1px solid #efeef3;">'
        '<div style="font-weight:700;font-size:19px;color:#0e0e12;letter-spacing:-0.01em;">12GetAJob</div>'
        f'<div style="margin-top:6px;font-size:13.5px;color:#6e6c78;">'
        f'{count} new listing{"" if count == 1 else "s"} match your saved search</div>'
        "</td></tr>"
        f'<tr><td style="padding:16px 20px 4px;">{cards}</td></tr>'
        '<tr><td style="padding:18px 28px 28px;">'
        '<a href="https://12getajob.com/jobs" style="display:inline-block;background:#0e0e12;'
        'color:#ffffff;font-size:13.5px;font-weight:600;padding:10px 22px;border-radius:999px;'
        'text-decoration:none;">View all matches &rarr;</a>'
        '<div style="margin-top:22px;padding-top:16px;border-top:1px solid #efeef3;font-size:12px;'
        'color:#9b99a5;">'
        f'<a href="{unsub_link}" style="color:#9b99a5;">Unsubscribe</a> from these alerts.</div>'
        # OSS_FOOTER_HTML nested inside this footer cell, not appended
        # after the table closes -- keeps it inside the white card on the
        # gray page background, matching the approved mockup, instead of
        # floating on the outer <table>'s own background.
        + OSS_FOOTER_HTML +
        "</td></tr>"
        "</table></td></tr></table>"
    )
